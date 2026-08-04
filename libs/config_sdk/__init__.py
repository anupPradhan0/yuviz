"""
Config SDK — the only component allowed to know where configuration comes
from (Redis vs. Config Service's REST API vs. Postgres). Conversation
Service, and any future service (Admin Worker, Analytics, CRM Integration,
Workflow Engine, Notification Service, ...), should depend only on
IConfigProvider and the models below — never on services.config directly,
never on redis-py/httpx directly for configuration reads.

Typical construction (see services/conversation/__main__.py):

    redis_repo = RedisConfigRepository(os.environ["REDIS_URL"])
    http_repo = HttpConfigRepository(
        base_url=os.environ["CONFIG_SERVICE_URL"],
        service_email=os.environ["CONFIG_SERVICE_EMAIL"],
        service_password=os.environ["CONFIG_SERVICE_PASSWORD"],
    )
    config: IConfigProvider = CacheAsideConfigProvider(redis_repo, http_repo)

    runtime_config = await config.get_runtime_config(tenant_slug, agent_slug)
"""

from .interfaces import IConfigProvider, IConfigRepository
from .models import (
    TRANSFER_TIMEOUT_DEFAULT_MS,
    TRANSFER_TIMEOUT_MAX_MS,
    TRANSFER_TIMEOUT_MIN_MS,
    Agent,
    ConversationInfo,
    MediaInfo,
    Policies,
    Prompt,
    ProviderConfig,
    ProviderConfigs,
    RuntimeConfig,
    Tenant,
    ToolSpec,
    validate_transfer_timeout_ms,
)
from .providers import CacheAsideConfigProvider, MockConfigProvider
from .repositories import HttpConfigRepository, RedisConfigRepository

__all__ = [
    "IConfigProvider",
    "IConfigRepository",
    "Tenant",
    "Agent",
    "ProviderConfig",
    "ProviderConfigs",
    "ConversationInfo",
    "MediaInfo",
    "Policies",
    "TRANSFER_TIMEOUT_MIN_MS",
    "TRANSFER_TIMEOUT_DEFAULT_MS",
    "TRANSFER_TIMEOUT_MAX_MS",
    "validate_transfer_timeout_ms",
    "Prompt",
    "ToolSpec",
    "RuntimeConfig",
    "CacheAsideConfigProvider",
    "MockConfigProvider",
    "RedisConfigRepository",
    "HttpConfigRepository",
]
