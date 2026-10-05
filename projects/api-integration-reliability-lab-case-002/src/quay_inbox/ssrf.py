"""Pin https callbacks to the address that passed the non-public deny list.

The publisher connects to ``pinned_address`` and does not resolve again.
Host and SNI keep the registrant's name. Redirects are not followed.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from urllib.parse import urlsplit

from .errors import SsrfError

METADATA_NAMES = frozenset(
    {
        "metadata",
        "metadata.google.internal",
        "instance-data",
        "localhost",
    }
)

_V4_DENY = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_V6_DENY = (
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


class ScriptedResolver:
    """Test double. ``mapping`` values are address strings, not live DNS."""

    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self.mapping = {key.lower(): list(value) for key, value in mapping.items()}
        self.calls: list[str] = []

    def resolve(self, host: str) -> list[str]:
        self.calls.append(host.lower())
        return list(self.mapping.get(host.lower(), []))


@dataclass(frozen=True)
class PinnedTarget:
    address: str
    host: str
    sni: str
    path: str
    url: str


def is_denied_address(text: str) -> bool:
    """Deny loopback, link-local, private, unique-local, and metadata ranges.

    TEST-NET addresses such as 203.0.113.10 are not in that set. The lab
    uses them as synthetic public answers from the scripted resolver.
    """

    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return True
    if ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved:
        return True
    if ip.version == 4:
        return any(ip in network for network in _V4_DENY)
    if any(ip in network for network in _V6_DENY) or ip.is_private:
        return True
    return False


def pin_https(url: str, resolver: ScriptedResolver) -> PinnedTarget:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise SsrfError("https required")
    host = parts.hostname
    if not host:
        raise SsrfError("missing host")
    host_key = host.lower().rstrip(".")
    if host_key in METADATA_NAMES or host_key.endswith(".internal") or host_key == "169.254.169.254":
        raise SsrfError("metadata")
    literal = _literal_ip(host_key)
    if literal is not None:
        addresses = [literal]
    else:
        addresses = resolver.resolve(host_key)
    if not addresses:
        raise SsrfError("unresolved")
    pinned = addresses[0]
    if is_denied_address(pinned):
        raise SsrfError("non-public")
    path = parts.path or "/"
    if parts.query:
        raise SsrfError("query is not part of the pinned profile")
    return PinnedTarget(pinned, host_key, host_key, path, url)


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None
