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
    PronunciationDictionary,
    SipTrunk,
    SupportedLanguage,
    VoiceBot,
    VoiceBotReadiness,
    VoiceBotSetting,
    VoiceCloneAudio,
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
from shared.models.billing_models import (
    Currency,
    ExchangeRate,
    ProviderPricing,
    UsageEvent,
)
from shared.models.guardrail_models import (
    GuardrailProfile,
    GuardrailProfileRule,
    GuardrailTrigger,
)
from shared.models.compliance_models import CompliancePolicy, ComplianceWording
from shared.models.collections_models import CustomerCollectionContext
from shared.models.context_models import RuntimeContextRecord, RuntimeContextSchema
from shared.models.memory_models import ConversationMemory

__all__ = [
    "Base",
    "Role", "Permission", "RolePermission", "User",
    "Tenant", "Plan", "Subscription", "Invoice", "SystemSetting", "TenantSetting",
    "VoiceBot", "VoiceBotReadiness", "VoiceProfile", "VoiceCloneAudio",
    "PronunciationDictionary",
    "SupportedLanguage", "BotLanguage",
    "VoiceBotSetting", "ChannelConfig", "PhoneNumber", "SipTrunk",
    "KnowledgeSource", "KnowledgeGap", "Prompt", "PromptVersion", "Intent", "EntityDef",
    "ApiConnection", "Workflow", "TestScenario", "Release", "PlatformTemplate",
    "ConversationSession", "PlatformAlert", "AuditLog", "Integration", "TenantIntegration",
    "ApprovedModel", "Guardrail", "UsageRecord", "HealthMetric",
    "GuardrailProfile", "GuardrailProfileRule", "GuardrailTrigger",
    "CompliancePolicy", "ComplianceWording",
    "Industry", "Country", "DataRegion", "AiConfigProfile", "ProviderDef", "ProviderModel",
    "Currency", "ExchangeRate", "ProviderPricing", "UsageEvent",
    "CustomerCollectionContext",
    "RuntimeContextSchema", "RuntimeContextRecord",
    "ConversationMemory",
]
