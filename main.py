import time
from typing import Any
import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .transport import patch_httpx_for_opencode, unpatch_httpx_for_opencode

# 导入适配器注册逻辑，确保在 AstrBot Provider 加载时提供选项
try:
    from . import adapter
except Exception as exc:
    logger.warning("[OpenCode] 导入 provider 适配器模块失败: %s", exc)

PLUGIN_NAME = "astrbot_plugin_opencode"


@register(
    PLUGIN_NAME,
    "Antigravity",
    "专为 OpenCode 免费及全系模型定制的 AstrBot 适配插件",
    "1.0.0",
)
class OpenCodePlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.config = dict(config or {})

    async def initialize(self) -> None:
        """插件初始化：自动挂载全局底层 OpenCode 请求拦截协议"""
        patch_httpx_for_opencode()
        logger.info("[OpenCode] 插件已就绪。已自动适配所有指向 opencode.ai 的模型调用。")

    async def terminate(self) -> None:
        """插件停用：卸载协议拦截"""
        unpatch_httpx_for_opencode()
        logger.info("[OpenCode] 插件已停用，已还原请求拦截器。")

    @filter.command("opencode")
    async def opencode_command(self, event: AstrMessageEvent, action: str = "status", *args: str) -> None:
        """OpenCode 调试与诊断指令: /opencode [status|test]"""
        action = action.lower().strip()
        if action == "test":
            target_model = args[0] if args else "muse-spark-1.3-contributor-free"
            yield event.plain_result(f"⏳ 正在测试 OpenCode 模型 [{target_model}]，请稍候...")
            
            # 从 AstrBot 配置中获取 opencode 提供商的 proxy 和 key
            api_key = ""
            proxy = ""
            try:
                cfg_mgr = getattr(self.context, "astrbot_config_mgr", None)
                if cfg_mgr:
                    cfg = cfg_mgr.get_config()
                    sources = cfg.get("provider_sources", [])
                    for s in sources:
                        if "opencode" in str(s.get("id", "")).lower() or "opencode.ai" in str(s.get("api_base", "")):
                            keys = s.get("key", [])
                            if keys and isinstance(keys, list):
                                api_key = keys[0]
                            proxy = s.get("proxy", "")
                            break
            except Exception as e:
                logger.debug(f"[OpenCode] 获取配置详情异常: {e}")

            headers = {
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            }
            payload = {
                "model": target_model,
                "input": [{"role": "user", "content": "你好，请用一句话证明你正常工作"}],
                "stream": True,
            }

            start_t = time.time()
            try:
                client_kwargs = {"timeout": 30.0}
                if proxy:
                    client_kwargs["proxy"] = proxy

                async with httpx.AsyncClient(**client_kwargs) as client:
                    resp = await client.post("https://opencode.ai/zen/v1/responses", headers=headers, json=payload)
                    cost = round(time.time() - start_t, 2)
                    if resp.status_code == 200:
                        yield event.plain_result(
                            f"✅ OpenCode 连通性测试成功！\n"
                            f"• 模型: {target_model}\n"
                            f"• 耗时: {cost}s\n"
                            f"• 使用代理: {proxy or '直连'}\n"
                            f"• 状态码: 200 OK\n"
                            f"已成功绕过 403 限制，模型通信正常！"
                        )
                    else:
                        yield event.plain_result(
                            f"❌ OpenCode 测试失败 (HTTP {resp.status_code}):\n{resp.text[:300]}"
                        )
            except Exception as exc:
                yield event.plain_result(f"❌ 请求发生异常: {exc}")

        else:
            yield event.plain_result(
                "🤖 【OpenCode 适配插件】运行中\n"
                "• 核心特性: 绕过 403 FreeTier 校验、补全 11 项内置工具、自动协议降级/升级\n"
                "• 可用指令:\n"
                "  /opencode status - 查看运行状态\n"
                "  /opencode test [模型名] - 快速测试模型连通性（默认 muse-spark-1.3-contributor-free）"
            )
