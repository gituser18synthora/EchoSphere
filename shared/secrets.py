"""Secret-reference resolution shared by the API and the voice runtime.

Tenant-configured rows (api_connections etc.) store only masked
``secret://NAME`` references; the raw credential lives in the process
environment as NAME (upper-cased, non-alphanumerics folded to "_"). Resolution
happens server-side at the moment of use — resolved values are never stored,
serialized, logged, traced, or exposed to the LLM.
"""

import re

from shared.config import get_settings


def resolve_secret(reference: str | None) -> str:
    """Resolve ``secret://NAME`` (or ``env:NAME``) to its value, else ""."""
    if not reference:
        return ""
    if reference.startswith("secret://"):
        env_key = re.sub(
            r"[^A-Za-z0-9]", "_", reference.removeprefix("secret://")
        ).upper()
        return get_settings().resolve_secret(f"env:{env_key}")
    if reference.startswith("env:"):
        return get_settings().resolve_secret(reference)
    # Anything else is not a reference; refuse to treat it as a credential.
    return ""
