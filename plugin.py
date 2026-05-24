"""
主动发言与回复插件 (人格与上下文感知版 + 重试与超时控制)
- 自动从数据库读取所有活跃会话，无需手动配置 target_hashes
- 使用 model="replyer" 调用聊天模型（修复 bge-m3 问题）
- 发言前获取 Bot 人格和最近聊天记录，让话题更自然
- 支持自定义超时时间和自动重试
"""

import asyncio
import random
from typing import Any, List, Dict

from maibot_sdk import Command, Field, MaiBotPlugin, PluginConfigBase

# 尝试导入麦麦的数据库模块（兼容不同版本路径）
try:
    from src.common.database import get_db
    from src.common.database.database_model import ChatStream
    DB_AVAILABLE = True
except ImportError:
    DB_AVAILABLE = False
    get_db = None
    ChatStream = None

# 尝试导入消息记录模型（不同版本表名可能不同）
try:
    # 常见版本使用 ChatLog 或 Message
    from src.common.database.database_model import ChatLog
    CHATLOG_AVAILABLE = True
except ImportError:
    CHATLOG_AVAILABLE = False
    ChatLog = None


class PluginSectionConfig(PluginConfigBase):
    enabled: bool = Field(default=True, description="是否启用插件")
    config_version: str = Field(default="1.0.0", description="配置文件版本")
    interval_minutes: int = Field(default=5, description="主动发言间隔（分钟）")
    probability: float = Field(default=0.3, description="每次检查时的发言概率")
    # 可选：手动指定目标会话哈希，留空则自动扫描数据库中的所有会话
    target_hashes: List[str] = Field(
        default_factory=list,
        description="目标会话哈希列表（留空则自动扫描所有活跃会话）",
        examples=["0b1b50f28b40e4315010ecaad09a29af"]
    )
    # 上下文感知相关配置
    context_messages: int = Field(default=3, description="发言前参考的最近消息数量（0表示不参考）")
    use_persona: bool = Field(default=True, description="是否使用 Bot 人格设定")
    # 新增：超时与重试配置
    llm_timeout: int = Field(default=180, description="LLM 生成请求超时时间（秒）")
    retry_times: int = Field(default=2, description="LLM 请求失败后的重试次数")
    retry_delay: float = Field(default=2.0, description="重试间隔（秒）")


class InitiativeTalkConfig(PluginConfigBase):
    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)


class InitiativeTalkPlugin(MaiBotPlugin):
    config_model = InitiativeTalkConfig

    # ------------------------------------------------------------------
    # 辅助方法：获取 Bot 人格
    # ------------------------------------------------------------------
    def _get_bot_persona(self) -> str:
        """获取 Bot 的人格设定（优先使用配置，其次尝试从 ctx 获取）"""
        # 方法1：从麦麦全局配置中读取 persona
        try:
            # 常见路径：self.ctx.config.get("persona", "")
            persona = self.ctx.config.get("persona", "")
            if persona:
                return persona
        except Exception:
            pass

        # 方法2：尝试从 ctx.get_persona() 获取（如果 SDK 提供了）
        if hasattr(self.ctx, "get_persona"):
            try:
                persona = self.ctx.get_persona()
                if persona:
                    return persona
            except Exception:
                pass

        # 默认人格
        return "你是一个热心的群友，性格开朗，喜欢和大家聊天"

    # ------------------------------------------------------------------
    # 辅助方法：获取近期聊天记录
    # ------------------------------------------------------------------
    async def _get_recent_messages(self, stream_id: str, limit: int = 3) -> List[Dict[str, str]]:
        """
        从数据库获取某个会话最近的消息记录（按时间正序）
        返回格式: [{"sender": "昵称/ID", "content": "消息内容"}, ...]
        """
        if not CHATLOG_AVAILABLE or not DB_AVAILABLE:
            return []

        try:
            with get_db().atomic():
                # 根据不同的表结构尝试查询（兼容 ChatLog 和 Message）
                if ChatLog is not None:
                    logs = (ChatLog
                            .select()
                            .where(ChatLog.stream_id == stream_id)
                            .order_by(ChatLog.created_at.desc())
                            .limit(limit))
                else:
                    # 尝试其他可能的表名（如 Message）
                    from src.common.database.database_model import Message
                    logs = (Message
                            .select()
                            .where(Message.stream_id == stream_id)
                            .order_by(Message.created_at.desc())
                            .limit(limit))

            # 按时间正序排列（旧 -> 新）
            messages = []
            for log in reversed(list(logs)):
                # 获取发送者名称（根据字段不同）
                sender = getattr(log, "sender_name", None)
                if not sender:
                    sender = getattr(log, "sender_id", "未知")
                content = getattr(log, "content", "") or getattr(log, "text", "")
                if content:
                    messages.append({"sender": sender, "content": content})
            return messages
        except Exception as e:
            self.ctx.logger.debug(f"获取会话历史失败 (stream={stream_id[:8]}...): {e}")
            return []

    # ------------------------------------------------------------------
    # 核心：发起一次主动发言（带人格、上下文、重试和超时控制）
    # ------------------------------------------------------------------
    async def _initiate_chat(self, stream_id: str):
        """向指定会话发起一次主动发言（感知人格和上下文，支持重试和自定义超时）"""
        # 1. 获取 Bot 人格
        persona = self._get_bot_persona() if self.config.plugin.use_persona else ""
        persona_part = f"你是{persona}。" if persona else ""

        # 2. 获取近期聊天记录（如果配置数量 > 0）
        context_part = ""
        context_limit = self.config.plugin.context_messages
        if context_limit > 0:
            recent = await self._get_recent_messages(stream_id, limit=context_limit)
            if recent:
                context_lines = ["近期聊天记录："]
                for msg in recent:
                    context_lines.append(f"{msg['sender']}: {msg['content']}")
                context_part = "\n".join(context_lines) + "\n"
            else:
                context_part = "（暂无近期聊天记录）\n"

        # 3. 构造 prompt
        prompt = f"""{persona_part}
{context_part}
请根据当前对话氛围，自然地发起一个新话题，或者延续大家正在聊的内容。
只说一句话，不要加任何解释或前缀。"""

        # 4. 重试调用 LLM
        timeout_seconds = self.config.plugin.llm_timeout
        retries = self.config.plugin.retry_times
        retry_delay = self.config.plugin.retry_delay

        last_error = None
        for attempt in range(retries + 1):  # 总尝试次数 = 重试次数 + 1
            try:
                result = await self.ctx.llm.generate(
                    prompt=prompt,
                    model="replyer",           # 使用聊天模型
                    temperature=0.8,
                    max_tokens=60,
                    timeout=timeout_seconds * 1000,  # SDK 可能要求毫秒，这里转换
                )
                success = result.get("success", False)
                content = result.get("response", "").strip()

                if success and content:
                    await self.ctx.send.text(content, stream_id)
                    self.ctx.logger.info(f"主动发言成功 (会话 {stream_id[:8]}...): {content}")
                    return  # 成功，直接返回
                else:
                    # 虽然 success=False，但不算网络错误，不重试
                    error_msg = result.get("error") or result.get("message") or "未知错误"
                    self.ctx.logger.error(f"LLM生成失败 (尝试 {attempt+1}/{retries+1}): {error_msg}")
                    # 如果还有重试机会，继续；否则跳出循环发送 fallback
                    if attempt == retries:
                        break
                    await asyncio.sleep(retry_delay)
                    continue

            except Exception as e:
                last_error = e
                self.ctx.logger.warning(f"LLM请求异常 (尝试 {attempt+1}/{retries+1}): {e}")
                if attempt == retries:
                    break
                await asyncio.sleep(retry_delay)
                continue

        # 所有重试都失败，发送 fallback 消息
        fallback = "唔… 暂时想不到什么话题，大家聊吧~"
        self.ctx.logger.error(f"主动发言最终失败，使用 fallback: {fallback} (最后错误: {last_error})")
        await self.ctx.send.text(fallback, stream_id)

    # ------------------------------------------------------------------
    # 核心：自动获取有效的会话哈希列表
    # ------------------------------------------------------------------
    def _get_streams_from_database(self) -> List[str]:
        """从麦麦数据库的 ChatStream 表中读取所有 stream_id"""
        if not DB_AVAILABLE:
            return []
        try:
            with get_db().atomic():
                streams = list(ChatStream.select(ChatStream.stream_id).dicts())
            return [s["stream_id"] for s in streams]
        except Exception as e:
            self.ctx.logger.error(f"读取数据库会话流失败: {e}")
            return []

    async def _get_target_streams(self) -> List[str]:
        """
        获取本次应该发言的目标会话列表。
        优先使用配置的 target_hashes，如果为空则自动从数据库扫描所有活跃会话。
        """
        # 1. 优先使用手动配置
        if self.config.plugin.target_hashes:
            return self.config.plugin.target_hashes

        # 2. 自动扫描数据库
        db_streams = self._get_streams_from_database()
        if db_streams:
            self.ctx.logger.debug(f"自动扫描到 {len(db_streams)} 个活跃会话")
            return db_streams

        # 3. 没有找到任何会话，返回空列表（调度器会稍后重试）
        self.ctx.logger.info("当前没有任何活跃会话，等待后续扫描...")
        return []

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def on_load(self) -> None:
        self.ctx.logger.info("主动发言插件已加载（人格与上下文感知版 + 重试/超时控制）")
        asyncio.create_task(self._schedule_chat())

    async def on_unload(self) -> None:
        self.ctx.logger.info("主动发言插件已卸载")

    async def on_config_update(self, new_config: dict) -> None:
        self.ctx.logger.debug(f"配置已更新: {new_config}")

    # ------------------------------------------------------------------
    # 定时发言调度器
    # ------------------------------------------------------------------
    async def _schedule_chat(self):
        """主循环：周期性检查并发言"""
        while True:
            if not self.config.plugin.enabled:
                await asyncio.sleep(60)
                continue

            target_streams = await self._get_target_streams()

            if not target_streams:
                self.ctx.logger.info("未找到任何目标会话，30秒后重试...")
                await asyncio.sleep(30)
                continue

            for stream_id in target_streams:
                if random.random() < self.config.plugin.probability:
                    await self._initiate_chat(stream_id)
                    await asyncio.sleep(2)

            await asyncio.sleep(self.config.plugin.interval_minutes * 60)

    # ------------------------------------------------------------------
    # 手动命令
    # ------------------------------------------------------------------
    @Command("主动发言", description="手动触发一次主动发言（在当前会话中）", pattern=r"^/chat$")
    async def manual_chat(self, stream_id: str = "", **kwargs: Any) -> tuple[bool, str, int]:
        """
        手动发言命令：在当前会话中触发一次主动发言。
        stream_id 由框架自动传入当前会话的哈希。
        """
        if not stream_id:
            streams = await self._get_target_streams()
            if streams:
                stream_id = streams[0]
                self.ctx.logger.warning("手动命令未获取到当前会话，使用第一个活跃会话")
            else:
                return False, "没有可用的会话，无法发言", 2

        await self._initiate_chat(stream_id)
        return True, "触发主动发言", 2


def create_plugin():
    return InitiativeTalkPlugin()
