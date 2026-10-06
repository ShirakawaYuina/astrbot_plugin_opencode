# AstrBot OpenCode 适配插件 (`astrbot_plugin_opencode`)

本插件专门用于解决在 **AstrBot** 中调用 **OpenCode** 大模型（特别是 `muse-spark-1.3-contributor-free` 等免费层模型）时遇到的 `403 FreeTierError` 权限问题。

---

## 核心特性

1. **解决 403 FreeTierError 限制**：
   - 自动生成符合官方 CLI 规范的有序消息标识（`x-opencode-session`、`x-opencode-request`）。
   - 自动模拟官方客户端 User-Agent 及项目上下文环境标识。
   - 自动补齐 OpenCode 免费层强校验所需的 11 项内置工具声明（`bash`, `edit`, `glob`, `grep`, `read`, `skill`, `task`, `todowrite`, `webfetch`, `websearch`, `write`）。
2. **多模型智能协议双向路由**：
   - OpenCode 后端不同模型所支持的协议不同：
     - **Muse 系列模型**（如 `muse-spark-1.3-contributor-free`）：原生要求 **Responses 协议**（`/responses`）。
     - **其他开源/免费模型**（如 `space-bunny-free`, `mimo-v2.6-flash-free`, `fledge-alpha-free`, `mimo-v2.5-free` 等）：原生要求 **ChatCompletions 协议**（`/chat/completions`）。
   - 本插件内置**协议智能路由器**：无论你在 AstrBot 中选用 `openai_responses` 还是 `openai_chat_completion` 适配器，插件都会根据当前调用的具体模型自动转译为目标正确的上游接口，并在流式与非流式之间无缝转换，无需用户手动区分协议。
3. **零侵入透明代理**：
   - 即插即用，无需更改现有任何已有供应商配置。
   - 也支持在仪表盘中直接选择新注册的 `opencode_responses` 提供商适配器。
4. **内置管理与连通性测试指令**：
   - `/opencode status`：查看运行状态。
   - `/opencode test [模型名]`：在线测试任意 OpenCode 模型的连通性与回包（如 `/opencode test space-bunny-free`）。

