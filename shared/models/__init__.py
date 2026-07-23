"""Import every model so Base.metadata sees all tables (Alembic autogenerate)."""

from shared.models.base import Base
from shared.models.auth_models import Permission, Role, RolePermission, User
from shared.models.tenancy_models import (
    Invoice,
    Plan,
    Subscription,
    SystemSetting,
    Tenant,
    TenantSetting,
)
from shared.models.bot_models import (
    BotLanguage,
    ChannelConfig,
    PhoneNumber,
    SipTrunk,
    SupportedLanguage,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    VoiceProfile,
)
from shared.models.content_models import (
    ApiConnection,
    EntityDef,
    Intent,
    KnowledgeGap,
    KnowledgeSource,
    PlatformTemplate,
    Prompt,
    PromptVersion,
    Release,
    TestScenario,
    Workflow,
)
from shared.models.master_models import (
    AiConfigProfile,
    Country,
    DataRegion,
    Industry,
    ProviderDef,
    ProviderModel,
)
from shared.models.ops_models import (
    ApprovedModel,
    AuditLog,
    ConversationSession,
    Guardrail,
    HealthMetric,
    Integration,
    PlatformAlert,
    TenantIntegration,
    UsageRecord,
)

__all__ = [
    "Base",
    "Role", "Permission", "RolePermission", "User",
    "Tenant", "Plan", "Subscription", "Invoice", "SystemSetting", "TenantSetting",
    "VoiceBot", "VoiceBotReadiness", "VoiceProfile", "SupportedLanguage", "BotLanguage",
    "VoiceBotSetting", "ChannelConfig", "PhoneNumber", "SipTrunk",
    "KnowledgeSource", "KnowledgeGap", "Prompt", "PromptVersion", "Intent", "EntityDef",
    "ApiConnection", "Workflow", "TestScenario", "Release", "PlatformTemplate",
    "ConversationSession", "PlatformAlert", "AuditLog", "Integration", "TenantIntegration",
    "ApprovedModel", "Guardrail", "UsageRecord", "HealthMetric",
    "Industry", "Country", "DataRegion", "AiConfigProfile", "ProviderDef", "ProviderModel",
]
