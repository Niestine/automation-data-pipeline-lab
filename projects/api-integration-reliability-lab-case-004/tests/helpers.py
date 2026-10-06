"""Shared in-process lab for the cold-lot grant tests."""

from __future__ import annotations

import base64
import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
REPO = Path(__file__).resolve().parents[3]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lotcycle.httputil import Request  # noqa: E402
from lotcycle.schema import canonical_json  # noqa: E402
from lotcycle.world import install_feed, install_grants, load_examples, open_lab  # noqa: E402


def build():
    lab = open_lab()
    examples = load_examples()
    install_grants(lab, examples["grants"])
    install_feed(lab, examples["excursions"])
    return lab


def basic(client_id: str, secret: str) -> str:
    raw = f"{client_id}:{secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def refresh_request(
    client,
    *,
    refresh: str | None = None,
    secret: str | None = None,
    scope: str | None = None,
    grant: str = "refresh_token",
    basic_auth: bool = True,
    include_refresh: bool = True,
    extra: dict | None = None,
    repeated: bool = False,
) -> Request:
    """Form the token endpoint would see from this client, with overrides."""
    form: dict[str, str] = {"grant_type": grant}
    if include_refresh and grant == "refresh_token":
        form["refresh_token"] = client.refresh_token if refresh is None else refresh
    if scope is not None:
        form["scope"] = scope
    headers = {"content-type": "application/x-www-form-urlencoded"}
    use_basic = basic_auth and client.client_type == "confidential"
    if use_basic:
        headers["authorization"] = basic(client.client_id, client.secret if secret is None else secret)
    else:
        form["client_id"] = client.client_id
    if extra:
        form.update(extra)
    body = urlencode(form).encode("utf-8")
    if repeated:
        body += b"&grant_type=refresh_token"
    return Request("POST", "/token", headers, body)


def order_request(token: str, body, key, audience: str = "lot-ledger", quoted: bool = True) -> Request:
    raw = canonical_json(body) if isinstance(body, dict) else body
    headers = {
        "authorization": "Bearer " + token,
        "content-type": "application/json",
    }
    if key is not None:
        headers["idempotency-key"] = ('"' + key + '"') if quoted else key
    return Request("POST", f"/{audience}/orders", headers, raw)


def token_posts(lab) -> int:
    return sum(1 for path in lab.transport.sent if path == "/token")


def logs_text(store) -> str:
    return json.dumps(store.logs)


def refresh_row_count(lab) -> int:
    row = lab.store.con.execute("SELECT COUNT(*) AS n FROM refresh_tokens").fetchone()
    return int(row["n"])


class LabCase(unittest.TestCase):
    def setUp(self) -> None:
        self.lab = build()

    def tearDown(self) -> None:
        self.lab.close()

    def north(self):
        return self.lab.clients["handheld-north"]

    def family(self, client_id: str = "handheld-north"):
        return self.lab.store.get_family("fam-" + client_id)

    def assert_nostore(self, response) -> None:
        self.assertEqual(response.headers.get("cache-control"), "no-store")
        self.assertEqual(response.headers.get("pragma"), "no-cache")
