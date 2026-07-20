"""Import every model so Base.metadata sees all tables (Alembic autogenerate)."""

from backend.models.base import Base
from backend.models.auth_models import Permission, Role, RolePermission, User
from backend.models.tenancy_models import (
    Invoice,
    Plan,
    Subscription,
    SystemSetting,
    Tenant,
    TenantSetting,
)
from backend.models.bot_models import (
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
from backend.models.content_models import (
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
from backend.models.master_models import (
    AiConfigProfile,
    DataRegion,
    Industry,
    ProviderDef,
)
from backend.models.ops_models import (
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
    "Industry", "DataRegion", "AiConfigProfile", "ProviderDef",
]
