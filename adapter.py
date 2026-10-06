from typing import Any

from astrbot.api import logger
from astrbot.core.provider.entities import ProviderType
from astrbot.core.provider.register import register_provider_adapter
from astrbot.core.provider.sources.openai_responses_source import ProviderOpenAIResponses
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial
from astrbot.core.provider.sources.request_retry import retry_provider_request

DEFAULT_OPENCODE_BASE = "https://opencode.ai/zen/v1"

OPENCODE_CONFIG_TMPL = {
    "provider": "openai",
    "provider_type": "chat_completion",
    "key": [],
    "api_base": DEFAULT_OPENCODE_BASE,
    "timeout": 120,
    "proxy": "",
    "custom_headers": {},
    "enable": True,
}


@register_provider_adapter(
    "opencode_responses",
    "OpenCode Responses 协议适配器（推荐免费模型使用）",
    ProviderType.CHAT_COMPLETION,
    default_config_tmpl={**OPENCODE_CONFIG_TMPL, "type": "opencode_responses", "id": "opencode_responses"},
    provider_display_name="OpenCode Responses",
)
class ProviderOpenCodeResponses(ProviderOpenAIResponses):
    """OpenCode 官方 Responses 协议模型适配器"""

    def __init__(self, provider_config: dict[str, Any], provider_settings: dict[str, Any]) -> None:
        if not provider_config.get("api_base"):
            provider_config["api_base"] = DEFAULT_OPENCODE_BASE
        super().__init__(provider_config, provider_settings)

    async def get_models(self) -> list[str]:
        try:
            models = await retry_provider_request(
                "OpenCode",
                lambda: self.client.models.list(),
            )
            return sorted([m.id for m in models.data])
        except Exception as e:
            logger.warning("[OpenCode] 获取动态模型列表失败，使用预设常用模型列表: %s", e)
            return [
                "muse-spark-1.3-contributor-free",
                "space-bunny-free",
                "mimo-v2.6-flash-free",
                "fledge-alpha-free",
                "mimo-v2.5-free",
                "ling-3.1-flash-free",
                "nemotron-3.5-lightning-free",
                "longcat-2.5-preview-free",
                "big-pickle",
            ]


@register_provider_adapter(
    "opencode_chat_completion",
    "OpenCode ChatCompletion 协议兼容适配器",
    ProviderType.CHAT_COMPLETION,
    default_config_tmpl={**OPENCODE_CONFIG_TMPL, "type": "opencode_chat_completion", "id": "opencode_chat"},
    provider_display_name="OpenCode ChatCompletion",
)
class ProviderOpenCodeChat(ProviderOpenAIOfficial):
    """OpenCode 经典 ChatCompletion 兼容适配器"""

    def __init__(self, provider_config: dict[str, Any], provider_settings: dict[str, Any]) -> None:
        if not provider_config.get("api_base"):
            provider_config["api_base"] = DEFAULT_OPENCODE_BASE
        super().__init__(provider_config, provider_settings)

    async def get_models(self) -> list[str]:
        try:
            models = await retry_provider_request(
                "OpenCode",
                lambda: self.client.models.list(),
            )
            return sorted([m.id for m in models.data])
        except Exception as e:
            logger.warning("[OpenCode] 获取动态模型列表失败，使用预设常用模型列表: %s", e)
            return [
                "muse-spark-1.3-contributor-free",
                "space-bunny-free",
                "mimo-v2.6-flash-free",
                "fledge-alpha-free",
                "mimo-v2.5-free",
                "ling-3.1-flash-free",
                "nemotron-3.5-lightning-free",
                "longcat-2.5-preview-free",
                "big-pickle",
            ]
