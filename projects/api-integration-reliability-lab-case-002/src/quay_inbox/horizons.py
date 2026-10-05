"""Retention, body cap, and the Standard Webhooks retry table.

The five-minute freshness tolerance is not the inbox lifetime. The table
below ends at 75:35:05, Stripe's automatic retries run for three days, and
the list/replay horizon used here is 30 days.
"""

from __future__ import annotations

TOLERANCE_SECONDS = 300
BODY_CAP_BYTES = 20 * 1024

# Delay before attempt N, matching the published table.
SCHEDULE_SECONDS = (
    0,
    5,
    5 * 60,
    30 * 60,
    2 * 3600,
    5 * 3600,
    10 * 3600,
    14 * 3600,
    20 * 3600,
    24 * 3600,
)

SPEC_TABLE_HORIZON = 75 * 3600 + 35 * 60 + 5
STRIPE_AUTOMATIC_HORIZON = 3 * 24 * 3600
LIST_RETENTION_SECONDS = 30 * 24 * 3600
INBOX_RETENTION_SECONDS = LIST_RETENTION_SECONDS
# Longer than both the retry table and the 30-day manual replay horizon.
IDEMPOTENCY_TTL_SECONDS = 31 * 24 * 3600

# 429/502/504 are the spec's throttle codes. 503 is included because a
# shared overload response is the case full jitter is for.
THROTTLE_STATUSES = frozenset({429, 502, 503, 504})

SELECTION_REQUIRED = frozenset({"release.audit_export"})
KNOWN_TYPES = frozenset(
    {
        "release.granted",
        "release.held",
        "release.opened",
        "release.settled",
        "release.container.moved",
        "release.audit_export",
    }
)

# Process default. Receivers must not use this to choose a payload schema.
CURRENT_API_VERSION = "2026-01-01"
API_VERSIONS = ("2024-09-01", "2026-01-01")
