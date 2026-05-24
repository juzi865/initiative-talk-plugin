# 🗣️ 麦麦主动发言插件

让麦麦在群聊或私聊里**主动找你聊天**，不再只是被动回复。

支持**定时发言**和**手动触发**，话题由 AI 自动生成，并且会参考最近聊天记录和 Bot 人格设定，就像有个热心的群友在合适的时候冒个泡～

---

## ✨ 功能亮点

- 💬 **主动发起话题**：按你设定的时间间隔，在指定群或私聊中抛出自然的话题。
- 🧠 **智能感知上下文**：发言前会读取最近几条聊天记录，让话题不突兀。
- 🎭 **人格设定**：支持读取麦麦配置的 `persona`，让发言风格更统一。
- ⏱️ **长思考支持**：模型响应慢时可以等待最长 5 分钟（可调），不会超时失败。
- 🔁 **自动重试**：模型临时故障时自动重试 2 次，提高成功率。
- 🎯 **超简单配置**：**无需手动填写任何 ID**，插件会自动扫描数据库中的所有活跃会话！
- 🔧 **两种触发方式**：定时自动发言 + 手动命令 `/chat`。

---

## 📦 安装方法

1. 将插件文件夹放入麦麦的 `plugins/` 目录下。
2. 重启麦麦（或在 WebUI 中重载插件）。
3. **无需额外配置**即可使用自动扫描功能。  
   如果你想限制只在某些会话中发言，可以按下方“手动指定目标”一节填写哈希值。
4. 确保你的麦麦已经配置好了 **replyer 模型任务**（一般默认都有），并且有可用的对话模型（如 `gpt-oss-20b`、`deepseek-v4-flash` 等）。

> 如果不会配置模型，请参考麦麦主程序的 `config/model_config.toml` 文件，确保 `[model_task_config.replyer]` 里的 `models` 列表有至少一个可用的生成模型（**不是 bge-m3 这种嵌入模型**）。

---

## ⚙️ 配置说明（WebUI 中可改）

| 配置项 | 作用 | 默认值 |
|--------|------|--------|
| `enabled` | 插件总开关 | `true` |
| `interval_minutes` | 每隔几分钟检查一次（定时发言周期） | `5` 分钟 |
| `probability` | 每次检查时发言的概率（0~1） | `0.3`（30% 概率） |
| `context_messages` | 发言前参考最近几条消息（0 表示不参考） | `3` |
| `use_persona` | 是否使用 Bot 人格设定 | `true` |
| `llm_timeout` | 生成话题时最多等多少秒（模型慢时可调大） | `180` 秒（3 分钟） |
| `retry_times` | 请求失败后重试次数 | `2` |
| `retry_delay` | 每次重试等待秒数 | `2` 秒 |
| `target_hashes` | **可选：手动指定会话哈希列表** | `[]`（留空则自动扫描） |

### 🎯 如何配置 `target_hashes`？

#### 推荐方式：留空，让插件自动扫描
插件会从麦麦数据库的 `chat_streams` 表中读取所有已有的会话（群聊或私聊），**自动对每个会话按概率发言**。  
这是最简单、最省心的方式，你**完全不需要关心任何 ID 或哈希值**。

> 注意：自动扫描要求数据库中已经有会话记录。如果你从未和某群/某人说过话，可以先手动发一句“你好”让麦麦回复，或者让群里有消息产生，数据库就会自动创建该会话的记录。

#### 高级方式：手动填写哈希值
如果你只想让麦麦在**特定几个会话**中发言，可以手动填入 `stream_id` 的 MD5 哈希值。  
哈希值获取方法如下（任选其一）：

1. **从 WebUI 获取**  
   打开麦麦的 Web 管理界面，找到“聊天流管理”或“会话列表”，复制目标会话的 `stream_id` 字段。

2. **从数据库获取**  
   用 SQLite 浏览器打开麦麦的 `data/mai.db` 文件，查看 `chat_streams` 表，复制 `stream_id` 列的值。

3. **从日志中获取**  
   执行手动命令 `/chat` 后，日志中会打印类似 `主动发言成功 (会话 0b1b50f2...)` 的信息，前面 8 位可以对照，但完整哈希建议用前两种方法。

**哈希值是否会变化？**  
**不会！** 哈希值由 `平台ID_群号`（群聊）或 `平台ID_QQ号_private`（私聊）通过 MD5 生成，只要群号或 QQ 号不变，哈希值就永远不变。重启麦麦、重装系统都不会影响。

配置示例（在 WebUI 的 `target_hashes` 列表中填写）：
```json
["0b1b50f28b40e4315010ecaad09a29af", "e6a2c8d9f4b12a7c5e3d8f1a9b4c6d7e"]
```

> 如果你既填写了哈希值，又希望自动扫描其他会话，插件**只会使用手动填写的列表**，不会合并。如需对所有会话发言，请保持 `target_hashes` 为空。

---

## 🚀 使用方法

### 1. 手动触发

在任何私聊或群聊中发送：
/chat

麦麦就会在当前对话中根据最近聊天记录主动说一句话。

### 2. 定时自动发言

按上述配置填好 `interval_minutes` 和 `probability`，并**确保 `target_hashes` 为空**（或填写了你想要的特定会话），插件就会按周期自动检查并发言。

例如：  
- `interval_minutes = 10`，`probability = 0.5`  
  表示每 10 分钟，对每个目标会话有 50% 的概率说一句话。

---

## ❓ 常见问题

### 麦麦不说话 / 没反应？

- 检查插件是否启用（`enabled = true`）。
- 检查是否使用了自动扫描但数据库中没有会话记录。解决方法：在目标群里发一条消息，让麦麦产生回复，数据库就会自动记录该会话。
- 如果手动填写了哈希值，请确认哈希值是否正确（可以从 WebUI 或数据库中复制）。
- 检查麦麦的 **replyer 模型**是否可用（可以在日志里看有没有“模型不存在”“参数不正确”等错误）。
- 尝试手动发送 `/chat` 命令。如果手动能成功但定时不行，说明 `target_hashes` 配置可能无效。

### 生成的话题太短 / 太奇怪？

- 这是由你使用的模型决定的。可以换一个更聪明的模型（比如 `gpt-oss-20b` 或 `deepseek-v4-flash`）。
- 你也可以修改插件里的 `prompt`（在 `plugin.py` 中搜索“请根据当前对话氛围”，调整一下描述）。

### 总是提示“请求超时”？

- 说明你的模型响应太慢了，可以适当增大 `llm_timeout`，例如调到 `240` 或 `300`（秒）。
- 或者换一个更快的模型。

### 我可以让麦麦只在我指定的几个群里发言吗？

- 可以。有两种方法：
  1. 留空 `target_hashes`，但通过麦麦的全局黑名单/白名单控制（如果主程序支持）。
  2. 手动填写那几个群的哈希值（获取方法见上文）。

### 支持其他平台（如微信、Discord）吗？

- 理论上支持，只要麦麦接入了该平台。哈希值的生成规则与平台相关，你需要从数据库或 WebUI 中获取对应平台的 `stream_id`。自动扫描功能同样适用。

---

## 📄 许可证

MIT License

Copyright (c) 2026 juzi865

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

---

## 🙏 感谢

本插件参考了麦麦社区“智能分段插件”的优秀设计，采用固定调度器绕过了 RPC 超时限制，让主动发言更稳定。  
感谢社区大佬对 **模型路由、人格感知、自动重试** 等功能的指点。  
灵感来自于 `mai_only_you`，也吸收了多位贡献者的建议。

**Enjoy your chat with MaiBot!** 🎉

---

**Enjoy your chat with MaiBot!** 🎉
