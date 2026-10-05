"""Per-route secrets. Standard Webhooks keys are decoded whsec_ bytes.

The lab floor is 32 bytes, the HMAC-SHA256 output length. The spec's 24-byte
minimum is rejected on purpose. These constants are placeholder material for
the in-process mock, not credentials for any real endpoint.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass

from .horizons import TOLERANCE_SECONDS

ROUTE_A = b"quay-lab-route-secret-032bytes!!"
ROUTE_B = b"quay-lab-other-route-secret-032b"
OLD_SECRET = b"quay-lab-old-secret-032-bytes!!!"
NEW_SECRET = b"quay-lab-new-secret-032-bytes!!!"

# Stripe's manual procedure uses the endpoint secret string itself as the
# HMAC key. A dashboard secret and a CLI listen secret are different keys.
DASHBOARD_SECRET = "whsec_dashboard_endpoint_secret_032"
CLI_SECRET = "whsec_cli_listen_secret_value_0032"


def encode_whsec(raw: bytes) -> str:
    if not isinstance(raw, bytes) or not 32 <= len(raw) <= 64:
        raise ValueError("HMAC key must be 32 to 64 bytes")
    return "whsec_" + base64.b64encode(raw).decode("ascii")


def decode_whsec(text: str) -> bytes:
    if not isinstance(text, str) or not text.startswith("whsec_"):
        raise ValueError("secret must start with whsec_")
    try:
        raw = base64.b64decode(text[len("whsec_") :], validate=True)
    except Exception as exc:
        raise ValueError("secret payload is not base64") from exc
    if not 32 <= len(raw) <= 64:
        raise ValueError("decoded secret must be 32 to 64 bytes")
    return raw


@dataclass(frozen=True)
class Route:
    route_id: str
    path: str
    profile: str
    secrets: tuple[bytes, ...] = ()
    stripe_secrets: tuple[str, ...] = ()
    tolerance: int = TOLERANCE_SECONDS
    keyid: str = "quay-coverage"

    def __post_init__(self) -> None:
        if self.tolerance <= 0:
            raise ValueError(
                "tolerance must be a positive number of seconds; 0 disables the freshness check"
            )
        if self.profile not in ("standard", "stripe", "coverage"):
            raise ValueError("unknown signature profile")
        if self.profile == "stripe":
            if not self.stripe_secrets:
                raise ValueError("stripe route requires endpoint secret strings")
        elif not self.secrets:
            raise ValueError("route requires at least one decoded secret")
        else:
            for secret in self.secrets:
                if not 32 <= len(secret) <= 64:
                    raise ValueError("HMAC key must be 32 to 64 bytes")
