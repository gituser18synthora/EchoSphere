"""Back-compat re-export — the SSRF-guarded HTTP layer moved to shared.

The voice runtime's tool executor and the API's connection tester share one
outbound policy; it lives in shared.safe_http so shared code never imports
from backend.
"""

from shared.safe_http import (  # noqa: F401
    SafeResponse,
    UnsafeUrlError,
    fetch_json,
    safe_request,
    validate_outbound_url,
)
