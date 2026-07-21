"""Code shared by the platform API (``backend``) and the realtime voice worker
(``voice_runtime``).

Only genuinely shared building blocks live here — settings, database clients,
SQLAlchemy models, provider adapters, the knowledge plane, conversation
orchestration, audio utilities, the Redis voice-session store and trusted
bot-config resolution. Import direction is strictly one-way:

    backend  ──►  shared  ◄──  voice_runtime

``shared`` must never import from ``backend`` or ``voice_runtime``.
"""
