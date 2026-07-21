"""Provider layer: STT / TTS / LLM / embeddings / telephony adapters.

Providers are selectable per tenant/bot through typed configuration; secrets
are referenced (`env:VAR`) and resolved at construction — never stored raw.
"""
