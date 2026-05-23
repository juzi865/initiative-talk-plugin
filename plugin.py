"""
主动发言与回复插件

功能：
- 在指定群聊或私聊中定时/手动发起话题，让机器人主动与用户互动。
- 自动复用宿主模型任务（如 utils 任务）的模型列表，支持超时、自动切换模型。
- 目标会话支持简写格式：群:123456 或 私:987654，也支持原始 stream_id（qq_123456 或 qq_987654_private）。

配置方式：
- 在 MaiBot WebUI 中打开插件配置页面，填写插件专属的 config.toml。
- 关键配置项说明见下方 PluginSectionConfig。
"""

import asyncio
import os
import random
import re
import tomllib
from typing import Any, List

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

# ==================== 尝试导入宿主内部 LLM 调度器 ====================
# 如果导入成功，插件可以使用固定任务调度器，绕过 RPC 层的 30 秒超时，
# 并利用宿主的多模型故障转移能力。
try:
    from src.llm_models.utils_model import LLMOrchestrator
    from src.config.model_configs import TaskConfig
    _HAS_INTERNAL_ORCHESTRATOR = True
except ImportError:
    _HAS_INTERNAL_ORCHESTRATOR = False


# ==================== 固定任务调度器（借鉴智能分段插件） ====================
class _PinnedTaskLLMOrchestrator(LLMOrchestrator):
    """
    固定模型列表的调度器，不依赖宿主 model_task_config 中的动态更新，
    而是直接使用调用方传入的 TaskConfig 对象。
    这样可以确保插件使用的模型列表、超时策略完全由插件配置控制。
    """

    def __init__(self, task_config: TaskConfig, request_type: str = "") -> None:
        self._pinned_task_config = task_config
        super().__init__(task_name="planner", request_type=request_type)  # task_name 仅作占位

    def _get_task_config_or_raise(self) -> TaskConfig:
        """重写：返回插件自己固定的任务配置，而不是去宿主查找。"""
        return self._pinned_task_config

    def _refresh_task_config(self) -> TaskConfig:
        """重写：确保模型列表变更时能刷新内部统计，但配置源仍为固定对象。"""
        latest = self._pinned_task_config
        if latest is not self.model_for_task:
            self.model_for_task = latest
        if list(self.model_usage.keys()) != latest.model_list:
            self.model_usage = {model: self.model_usage.get(model, (0, 0, 0)) for model in latest.model_list}
        return self.model_for_task


# ==================== 插件配置定义 ====================
class PluginSectionConfig(PluginConfigBase):
    """
    插件主配置段，所有可调整的运行时参数均在此定义。
    WebUI 会根据这些 Field 自动生成可视化配置表单。
    """

    # 插件总开关
    enabled: bool = Field(default=True, description="是否启用插件")

    # 配置文件版本，用于升级时兼容，一般无需修改
    config_version: str = Field(default="1.0.0", description="配置文件版本")

    # 定时发言间隔（分钟）
    interval_minutes: int = Field(default=5, description="主动发言间隔（分钟）")

    # 每次检查时的发言概率（0~1），仅在定时发言中生效
    probability: float = Field(default=0.3, description="每次检查时的发言概率")

    # 使用的宿主模型任务名，如 utils / replyer / planner
    # 插件会读取宿主 model_config.toml 中对应任务的模型列表，并顺序尝试。
    model_task: str = Field(default="utils", description="使用的宿主模型任务名，如 utils, replyer, planner")

    # 单次生成话题的超时时间（秒），建议 120~300。
    # 因为生成话题可能耗时较长（尤其模型负载高时），此值需大于实际模型响应时间。
    timeout_seconds: int = Field(default=180, description="单次生成超时时间（秒），建议 120-300")

    # 目标会话列表：支持两种格式
    # 1. 原始 stream_id：qq_123456（群聊） 或 qq_987654_private（私聊）
    # 2. 简写格式：群:123456 或 私:987654（中英文冒号均可，不区分大小写）
    # 留空则只响应手动 /chat 命令，不自动发言。
    target_streams: List[str] = Field(
        default_factory=list,
        description="目标会话。支持格式：原始stream_id（如 qq_123456），或简写“群:群号”“私:QQ号”（英文冒号或中文冒号均可）",
        examples=["群:123456", "私:987654", "qq_123456"]
    )


class InitiativeTalkConfig(PluginConfigBase):
    """总配置模型，包含插件段。"""
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


# ==================== 主插件类 ====================
class InitiativeTalkPlugin(MaiBotPlugin):
    """
    主动发言插件主类，实现定时/手动发起话题的全部逻辑。
    """

    config_model = InitiativeTalkConfig

    # ------------------------------------------------------------------
    # 内部辅助方法：将用户友好的会话标识转换为内部 stream_id
    # ------------------------------------------------------------------
    def _parse_stream_id(self, user_input: str) -> str | None:
        """
        将用户友好的输入转换为内部 stream_id。

        支持格式：
        - 原始格式: qq_123456 或 qq_987654_private
        - 简写: 群:123456 或 group:123456
        - 简写: 私:987654 或 private:987654

        返回转换后的 stream_id，若无法识别则返回 None。
        """
        s = user_input.strip()
        if not s:
            return None

        # 1. 已经是原始格式（qq_数字 或 qq_数字_private）→ 直接使用
        if re.match(r'^qq_\d+(_private)?$', s):
            return s

        # 2. 解析群聊简写：群:数字 或 group:数字（不区分大小写）
        match = re.match(r'^(群|group)[：:]\s*(\d+)$', s, re.IGNORECASE)
        if match:
            group_id = match.group(2)
            return f"qq_{group_id}"

        # 3. 解析私聊简写：私:数字 或 private:数字
        match = re.match(r'^(私|private)[：:]\s*(\d+)$', s, re.IGNORECASE)
        if match:
            user_id = match.group(2)
            return f"qq_{user_id}_private"

        # 无法识别：记录警告并忽略该条目
        self.ctx.logger.warning(
            f"无法解析的目标会话格式: {s}，请使用 '群:123456' 或 '私:789012' 或原始 stream_id"
        )
        return None

    def _resolve_target_streams(self) -> List[str]:
        """
        从配置中读取 target_streams 列表，将每个条目转换为有效的 stream_id，
        过滤无效条目后返回。
        """
        raw_list = self.config.plugin.target_streams
        if not raw_list:
            return []
        resolved = []
        for item in raw_list:
            sid = self._parse_stream_id(item)
            if sid:
                resolved.append(sid)
            else:
                self.ctx.logger.error(f"忽略无效的目标会话配置: {item}")
        return resolved

    # ------------------------------------------------------------------
    # 插件生命周期
    # ------------------------------------------------------------------
    async def on_load(self) -> None:
        """插件加载时执行：解析目标会话，启动定时任务（如果配置了有效会话）。"""
        # 解析并存储有效的 stream_id 列表，供定时任务使用
        self._resolved_streams = self._resolve_target_streams()
        self.ctx.logger.info(f"主动发言插件已加载，有效目标会话: {self._resolved_streams}")

        if not _HAS_INTERNAL_ORCHESTRATOR:
            self.ctx.logger.warning(
                "无法导入宿主 LLMOrchestrator，将回退到 llm.generate RPC 方式（可能超时）"
            )

        if self._resolved_streams:
            asyncio.create_task(self._schedule_chat())
        else:
            self.ctx.logger.info("未配置有效目标会话，定时主动发言已禁用，仅支持手动触发 /chat")

    async def on_unload(self) -> None:
        """插件卸载时清理日志。"""
        self.ctx.logger.info("主动发言插件已卸载")

    async def on_config_update(self, new_config: dict) -> None:
        """
        配置热更新时重新解析目标会话。
        MaiBot 会在 WebUI 保存配置后调用此方法。
        """
        # 注意：此处 new_config 参数包含完整的新配置，但我们只重新解析 target_streams
        self._resolved_streams = self._resolve_target_streams()
        self.ctx.logger.info(f"配置已更新，有效目标会话: {self._resolved_streams}")

    # ------------------------------------------------------------------
    # 定时调度任务
    # ------------------------------------------------------------------
    async def _schedule_chat(self):
        """
        定时发言循环：按 interval_minutes 间隔遍历所有目标会话，
        按 probability 概率决定是否发言。
        """
        while True:
            # 检查插件总开关
            if not self.config.plugin.enabled:
                await asyncio.sleep(60)
                continue

            streams = self._resolved_streams
            if not streams:
                await asyncio.sleep(self.config.plugin.interval_minutes * 60)
                continue

            for stream_id in streams:
                # 根据概率随机决定是否本次发言
                if random.random() < self.config.plugin.probability:
                    await self._initiate_chat(stream_id)
                    # 避免短时间内多个会话同时发言，稍作延迟
                    await asyncio.sleep(2)

            # 等待下一个周期
            await asyncio.sleep(self.config.plugin.interval_minutes * 60)

    # ------------------------------------------------------------------
    # 核心生成逻辑：使用固定调度器或回退 RPC
    # ------------------------------------------------------------------
    async def _generate_using_pinned_orchestrator(
        self, prompt: str, task_name: str, timeout_sec: int
    ) -> tuple[bool, str]:
        """
        使用固定任务调度器生成回复（推荐方式）。
        优点：
        - 绕过 RPC 层的 30 秒硬超时，支持长超时（最长可设置数分钟）。
        - 自动按顺序尝试任务配置中的多个模型，某个模型失败/超时后会切换到下一个。
        - 超时由 asyncio.wait_for + hard_timeout 双重控制。
        """
        try:
            # 1. 定位宿主 model_config.toml 文件（通常位于 MaiBot 根目录的 config/ 下）
            config_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "config", "model_config.toml"
            )
            if not os.path.isfile(config_path):
                # 尝试另一种常见路径（当插件目录结构不同时）
                config_path = os.path.join(
                    os.path.dirname(__file__), "..", "..", "..", "config", "model_config.toml"
                )
            with open(config_path, "rb") as f:
                full_config = tomllib.load(f)

            # 2. 获取指定任务（如 utils）的配置
            task_config_dict = full_config.get("model_task_config", {}).get(task_name)
            if not task_config_dict:
                raise ValueError(f"未找到任务 {task_name} 的配置")

            model_list = task_config_dict.get("model_list", [])
            if not model_list:
                raise ValueError(f"任务 {task_name} 的 model_list 为空")

            # 3. 构造 TaskConfig 对象，使用用户配置的超时时间和顺序切换策略
            task_cfg = TaskConfig(
                model_list=model_list,
                max_tokens=120,               # 话题较短，120 token 足够
                temperature=0.7,              # 稍高温度让话题更自然
                slow_threshold=timeout_sec,   # 慢请求警告阈值
                selection_strategy="sequential",  # 按配置顺序尝试模型，失败则切换
                hard_timeout=timeout_sec,     # 每个模型的硬超时（秒）
            )

            # 4. 创建固定调度器并执行生成
            orchestrator = _PinnedTaskLLMOrchestrator(task_cfg, request_type="plugin.initiative_talk")
            result = await asyncio.wait_for(
                orchestrator.generate_response_async(prompt=prompt, max_tokens=120, temperature=0.7),
                timeout=timeout_sec + 10   # 额外给一点缓冲时间
            )

            if result and result.response:
                return True, result.response.strip()
            else:
                return False, ""
        except Exception as e:
            self.ctx.logger.error(f"固定调度器生成失败: {e}", exc_info=True)
            return False, ""

    async def _initiate_chat(self, stream_id: str):
        """
        主动发言的主要入口：生成话题并发送。
        stream_id 可以是自动解析后的 qq_123456 或 qq_xxx_private。
        """
        prompt = (
            "你是一个热心的群友，想在群里找个话题和大家聊聊。"
            "请随意想到一个话题，用一句日常的话说出来。"
        )

        try:
            model_task = self.config.plugin.model_task
            timeout = self.config.plugin.timeout_seconds
            self.ctx.logger.debug(f"主动发言使用模型任务: {model_task}, 超时: {timeout}秒")

            # 优先使用内部调度器（支持长超时+自动切换模型）
            if _HAS_INTERNAL_ORCHESTRATOR:
                success, content = await self._generate_using_pinned_orchestrator(
                    prompt, model_task, timeout
                )
            else:
                # 回退方案：使用普通 RPC 调用（可能受 30 秒超时限制）
                success, content, *rest = await asyncio.wait_for(
                    self.ctx.llm.generate(prompt=prompt, model=model_task, max_tokens=120),
                    timeout=timeout
                )
                content = content if success else ""

            if success and content and content.strip():
                await self.ctx.send.text(content, stream_id)
                self.ctx.logger.info(f"主动发言成功 (会话 {stream_id}): {content}")
            else:
                fallback = "唔… 暂时想不到什么话题，稍后再试试吧~"
                await self.ctx.send.text(fallback, stream_id)
                self.ctx.logger.error(f"主动发言失败 (会话 {stream_id})，原始内容: {content}")
        except asyncio.TimeoutError:
            self.ctx.logger.error(f"主动发言超时 (>{timeout}秒) 会话 {stream_id}")
            await self.ctx.send.text("模型思考时间太长，稍后再试试吧~", stream_id)
        except Exception as e:
            self.ctx.logger.error(f"主动发言异常 (会话 {stream_id}): {e}", exc_info=True)
            await self.ctx.send.text("生成话题时出错了，请检查模型配置或余额。", stream_id)

    # ------------------------------------------------------------------
    # 手动命令
    # ------------------------------------------------------------------
    @Command("主动发言", description="手动触发一次主动发言", pattern=r"^/chat$")
    async def manual_chat(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """
        处理 /chat 命令：立即在当前会话（私聊或群聊）中主动发起一次话题。
        stream_id 由框架自动注入，无需用户提供。
        """
        await self._initiate_chat(stream_id)
        return True, "触发主动发言", 2   # 第二个返回值会作为提示输出，第三个为拦截等级


# ==================== 工厂函数 ====================
def create_plugin():
    """MaiBot 要求的工厂函数，用于实例化插件。"""
    return InitiativeTalkPlugin()