"""Environment-driven settings. All credentials come from .env — never hardcode."""

import logging
import os
from functools import lru_cache
from urllib.parse import quote_plus

from dotenv import load_dotenv
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# Secret *references* (env:VAR) resolve against os.environ, so the root .env
# must be loaded into the process environment too — not only into the pydantic
# Settings fields. Real environment variables always win (override=False).
load_dotenv(os.path.join(_PROJECT_ROOT, ".env"), override=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(_PROJECT_ROOT, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = "development"
    app_name: str = "EchoSphere"
    api_host: str = "0.0.0.0"
    api_port: int = 9001
    frontend_port: int = 5199
    cors_origins: str = "http://localhost:5199"

    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 720

    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_database: str = "voice_bot"
    mysql_user: str = ""
    mysql_password: str = ""

    mongodb_uri: str = "mongodb://localhost:27017"
    mongodb_database: str = "voice_bot"

    redis_url: str = "redis://localhost:6379"

    allow_hard_delete: bool = False
    auto_run_migrations: bool = False
    enable_database_seed: bool = True

    superadmin_email: str = "admin@aurexion.com"
    superadmin_name: str = "Platform Admin"
    superadmin_password: str = ""

    # ── PostgreSQL + pgvector (knowledge plane) ──────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_database: str = "echosphere_knowledge"
    postgres_user: str = ""
    postgres_password: str = ""
    postgres_pool_size: int = 10
    postgres_max_overflow: int = 20

    pgvector_distance_metric: str = "cosine"
    pgvector_hnsw_m: int = 16
    pgvector_hnsw_ef_construction: int = 64
    pgvector_hnsw_ef_search: int = 100

    # ── Knowledge / RAG ──────────────────────────────────────────
    embedding_provider: str = "openai"
    embedding_api_key_reference: str = "env:OPENAI_API_KEY"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimension: int = 1536
    embedding_batch_size: int = 64

    knowledge_upload_dir: str = "storage/knowledge"
    knowledge_max_file_mb: int = 25
    retrieval_top_k: int = 6
    retrieval_candidate_k: int = 24
    retrieval_rerank_k: int = 12
    retrieval_min_score: float = 0.35
    # Score fusion: "weighted" = normalized weighted-sum of semantic + BM25
    # scores; "rrf" = weighted reciprocal-rank fusion (rank-only, use when the
    # two score distributions cannot be compared reliably).
    retrieval_fusion_method: str = "weighted"
    retrieval_semantic_weight: float = 0.65
    retrieval_bm25_weight: float = 0.35
    # ts_rank_cd is unbounded — saturate to (0,1) via rank/(rank+k) before fusing.
    retrieval_bm25_saturation: float = 1.0
    # A keyword hit counts as relevant on its own once its raw ts_rank_cd
    # clears this floor (protects exact terms/codes/names from the vector gate).
    retrieval_min_keyword_rank: float = 0.02
    # Fused-score bonus when a chunk contains the whole query as a phrase.
    retrieval_phrase_boost: float = 0.1
    # RRF-mode weights (legacy names kept so existing .env files keep working).
    retrieval_hybrid_vector_weight: float = 0.6
    retrieval_hybrid_keyword_weight: float = 0.4
    retrieval_use_reranker: bool = False
    retrieval_ts_config: str = "english"
    enable_ocr_fallback: bool = True
    ocr_min_page_chars: int = 120
    ingestion_worker_poll_seconds: float = 2.0
    ingestion_max_attempts: int = 3
    # Run the ingestion worker inside the API process (dev/single-node default).
    # Disable when running dedicated `python -m backend.workers.ingestion` workers.
    ingestion_worker_embedded: bool = True

    # ── Providers (platform defaults; tenant/bot overrides in DB) ─
    # Defaults must stay inside the governed provider matrix:
    # STT=Sarvam, TTS=Sarvam/ElevenLabs, LLM=OpenAI, Embedding=OpenAI.
    stt_provider: str = "sarvam"
    stt_api_key_reference: str = "env:SARVAM_API_KEY"
    stt_model: str = "saaras:v3"
    tts_provider: str = "sarvam"
    tts_api_key_reference: str = "env:SARVAM_API_KEY"
    tts_model: str = "bulbul:v3"
    tts_voice: str = "shubh"
    llm_provider: str = "openai"
    llm_api_key_reference: str = "env:OPENAI_API_KEY"
    llm_model: str = "gpt-4o-mini"

    # ── Outbound API connections (SSRF guard) ───────────────────
    # Private/loopback targets are blocked unless explicitly allowed (dev/test).
    api_connect_allow_private: bool = False
    # Comma-separated host allowlist; empty = any public host.
    api_connect_allowed_hosts: str = ""
    api_connect_max_response_kb: int = 64

    # ── MCP server ───────────────────────────────────────────────
    mcp_enabled: bool = True
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 9003

    # ── Voice runtime ────────────────────────────────────────────
    voice_worker_host: str = "0.0.0.0"
    voice_worker_port: int = 9002
    voice_worker_concurrency: int = 20
    voice_session_timeout: int = 900
    max_call_duration: int = 3600
    default_silence_timeout: int = 12

    # ── Telephony ────────────────────────────────────────────────
    freeswitch_host: str = "127.0.0.1"
    freeswitch_port: int = 9004
    freeswitch_password_reference: str = "env:FREESWITCH_PASSWORD"
    telephony_webhook_secret_reference: str = "env:TELEPHONY_WEBHOOK_SECRET"
    # Public base URL (ws:// or wss://) telephony providers use to reach the
    # VOICE WORKER's media WebSocket. Set it whenever the worker is not
    # reachable on the same host:port that served the webhook (separate
    # process/port, or a different public hostname). Empty = derive from the
    # webhook request's own base URL (single-host proxy deployments).
    telephony_public_ws_base: str = ""
    # The telephony gateway (`python -m voice_runtime.gateway`) is a second
    # voice-worker instance bound to the public dialer port so external
    # dialers reach ONE host:port for both the signed webhook and the media
    # WebSocket. TELEPHONY_PUBLIC_WS_BASE should point at this instance.
    telephony_gateway_host: str = "0.0.0.0"
    telephony_gateway_port: int = 9011

    @property
    def postgres_url(self) -> str:
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password)
        return (
            f"postgresql+asyncpg://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def postgres_sync_url(self) -> str:
        """psycopg-free sync URL for Alembic (uses pg8000-style asyncpg run_sync instead)."""
        return self.postgres_url

    def resolve_secret(self, reference: str) -> str:
        """Resolve a `env:VAR_NAME` secret reference. Raw values are never stored in DB rows."""
        if not reference:
            return ""
        if reference.startswith("env:"):
            return os.environ.get(reference[4:], "")
        return reference

    @property
    def mysql_url(self) -> str:
        return (
            f"mysql+pymysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_database}?charset=utf8mb4"
        )

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


class ConfigurationError(RuntimeError):
    """Raised at startup when mandatory environment variables are missing."""


def validate_settings(service: str) -> None:
    """Fail fast with a clear error when mandatory settings are missing.

    ``service`` is one of ``"api"``, ``"voice-runtime"`` or
    ``"ingestion-worker"``. Infrastructure settings every process needs
    (MySQL, MongoDB, Redis) are always checked; service-specific ones only
    for the service that uses them. Optional provider API keys never block
    startup unless that provider is actually selected — and even then a
    missing key is only fatal in production (development logs a warning so
    mock-provider setups keep working).
    """
    settings = get_settings()
    missing: list[str] = []
    warnings: list[str] = []

    def require(value: str, name: str, hint: str) -> None:
        if not value:
            missing.append(f"{name} — {hint}")

    # Every service talks to these backing stores.
    require(settings.mysql_user, "MYSQL_USER", "MySQL account for the control-plane database")
    require(settings.mysql_password, "MYSQL_PASSWORD", "password for MYSQL_USER")
    require(settings.mongodb_uri, "MONGODB_URI", "MongoDB connection string (transcripts/events)")
    require(settings.redis_url, "REDIS_URL", "Redis connection string (voice sessions, caches)")

    if service == "api":
        require(settings.jwt_secret, "JWT_SECRET",
                'generate with: python -c "import secrets; print(secrets.token_urlsafe(48))"')
        if settings.enable_database_seed:
            require(settings.superadmin_password, "SUPERADMIN_PASSWORD",
                    "initial super-admin password (required while ENABLE_DATABASE_SEED=true)")

    if service in ("api", "ingestion-worker"):
        # The knowledge plane (pgvector) backs knowledge APIs and ingestion.
        require(settings.postgres_user, "POSTGRES_USER", "PostgreSQL account (knowledge plane)")
        require(settings.postgres_password, "POSTGRES_PASSWORD", "password for POSTGRES_USER")
        if settings.embedding_provider != "mock":
            key = settings.resolve_secret(settings.embedding_api_key_reference)
            if not key:
                warnings.append(
                    f"EMBEDDING_PROVIDER={settings.embedding_provider} but "
                    f"{settings.embedding_api_key_reference!r} resolves empty — "
                    "document ingestion and retrieval will fail"
                )

    if service == "voice-runtime":
        for kind, provider, reference in (
            ("STT", settings.stt_provider, settings.stt_api_key_reference),
            ("TTS", settings.tts_provider, settings.tts_api_key_reference),
            ("LLM", settings.llm_provider, settings.llm_api_key_reference),
        ):
            if provider != "mock" and not settings.resolve_secret(reference):
                warnings.append(
                    f"default {kind} provider '{provider}' has no API key "
                    f"({reference!r} resolves empty) — calls using the platform "
                    "default will fail; per-bot provider overrides still work"
                )

    logger = logging.getLogger(f"{service}.config")
    if missing:
        detail = "\n  - ".join(missing)
        message = (
            f"{service}: missing mandatory environment variables "
            f"(set them in the project root .env):\n  - {detail}"
        )
        raise ConfigurationError(message)
    for warning in warnings:
        if settings.app_env == "production":
            raise ConfigurationError(f"{service}: {warning}")
        logger.warning("%s", warning)
