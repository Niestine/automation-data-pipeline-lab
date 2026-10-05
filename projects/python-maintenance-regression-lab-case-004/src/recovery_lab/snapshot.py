"""Checksummed ledger snapshot and in-memory application of logical images."""

from __future__ import annotations

import copy
import hashlib
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path

from recovery_lab.format import MAGIC, SNAP_NAME, canonical_json, crc32

INITIAL_STATES = {"email_index": "public", "status": "absent"}


@dataclass
class Ledger:
    accounts: dict[int, dict] = field(default_factory=dict)
    email_index: dict[str, int] = field(default_factory=dict)
    schema_version: int = 1
    element_states: dict[str, str] = field(default_factory=lambda: dict(INITIAL_STATES))
    applied_lsn: int = 0
    backfill_complete: bool = False
    cleanup_complete: bool = False

    def copy(self) -> Ledger:
        return Ledger(
            accounts=copy.deepcopy(self.accounts),
            email_index=dict(self.email_index),
            schema_version=self.schema_version,
            element_states=dict(self.element_states),
            applied_lsn=self.applied_lsn,
            backfill_complete=self.backfill_complete,
            cleanup_complete=self.cleanup_complete,
        )


def empty_ledger() -> Ledger:
    return Ledger()


def fingerprint(ledger: Ledger) -> str:
    payload = {
        "accounts": [ledger.accounts[key] for key in sorted(ledger.accounts)],
        "backfill_complete": ledger.backfill_complete,
        "cleanup_complete": ledger.cleanup_complete,
        "element_states": ledger.element_states,
        "email_index": {key: ledger.email_index[key] for key in sorted(ledger.email_index)},
        "schema_version": ledger.schema_version,
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def encode_snapshot(ledger: Ledger) -> bytes:
    states = canonical_json(ledger.element_states)
    body_obj = {
        "accounts": [ledger.accounts[key] for key in sorted(ledger.accounts)],
        "backfill_complete": ledger.backfill_complete,
        "cleanup_complete": ledger.cleanup_complete,
        "email_index": {key: ledger.email_index[key] for key in sorted(ledger.email_index)},
    }
    body = canonical_json(body_obj)
    out = bytearray()
    out += MAGIC
    out += struct.pack("<IQ", ledger.schema_version, ledger.applied_lsn)
    out += struct.pack("<I", len(states))
    out += states
    out += struct.pack("<I", crc32(states))
    out += struct.pack("<I", len(body))
    out += body
    out += struct.pack("<I", crc32(body))
    return bytes(out)


def decode_snapshot(blob: bytes) -> Ledger | None:
    """Return the ledger, or None when the bytes are not a valid snapshot."""
    if len(blob) < len(MAGIC) + 4 + 8 + 4:
        return None
    if not blob.startswith(MAGIC):
        return None
    offset = len(MAGIC)
    schema_version, applied_lsn = struct.unpack_from("<IQ", blob, offset)
    offset += 12
    if offset + 4 > len(blob):
        return None
    (states_len,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    if offset + states_len + 4 > len(blob):
        return None
    states_raw = blob[offset : offset + states_len]
    offset += states_len
    (states_crc,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    if crc32(states_raw) != states_crc:
        return None
    if offset + 4 > len(blob):
        return None
    (body_len,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    if offset + body_len + 4 > len(blob):
        return None
    body_raw = blob[offset : offset + body_len]
    offset += body_len
    (body_crc,) = struct.unpack_from("<I", blob, offset)
    offset += 4
    if offset != len(blob) or crc32(body_raw) != body_crc:
        return None
    try:
        states = json.loads(states_raw.decode("utf-8"))
        body = json.loads(body_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(states, dict) or not isinstance(body, dict):
        return None
    accounts: dict[int, dict] = {}
    for row in body.get("accounts") or []:
        if not isinstance(row, dict) or "account_id" not in row:
            return None
        accounts[int(row["account_id"])] = {
            "account_id": int(row["account_id"]),
            "balance": int(row["balance"]),
            "email": str(row["email"]),
            "status": row.get("status"),
        }
    index: dict[str, int] = {}
    for email, account_id in (body.get("email_index") or {}).items():
        index[str(email)] = int(account_id)
    element_states = {
        "email_index": str(states.get("email_index", "public")),
        "status": str(states.get("status", "absent")),
    }
    return Ledger(
        accounts=accounts,
        email_index=index,
        schema_version=int(schema_version),
        element_states=element_states,
        applied_lsn=int(applied_lsn),
        backfill_complete=bool(body.get("backfill_complete")),
        cleanup_complete=bool(body.get("cleanup_complete")),
    )


def read_snapshot(directory: Path) -> tuple[Ledger | None, bytes | None]:
    path = Path(directory) / SNAP_NAME
    if not path.exists():
        return None, None
    blob = path.read_bytes()
    return decode_snapshot(blob), blob


def file_hash(path: Path) -> str:
    if not path.exists():
        return "missing"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_image(ledger: Ledger, image: dict | None) -> None:
    """Apply one absolute logical image. A second apply is a no-op."""
    if not image:
        return
    account_id = image.get("account_id")
    if account_id is not None:
        account = image.get("account")
        if account is None:
            ledger.accounts.pop(int(account_id), None)
        else:
            ledger.accounts[int(account_id)] = {
                "account_id": int(account["account_id"]),
                "balance": int(account["balance"]),
                "email": str(account["email"]),
                "status": account.get("status"),
            }
        for email in image.get("index_clear") or []:
            if ledger.email_index.get(str(email)) == int(account_id):
                del ledger.email_index[str(email)]
        for email, pointed in (image.get("index_set") or {}).items():
            ledger.email_index[str(email)] = int(pointed)
    if image.get("schema_version") is not None:
        ledger.schema_version = int(image["schema_version"])
    states = image.get("element_states")
    if isinstance(states, dict) and states:
        ledger.element_states = {
            "email_index": str(states.get("email_index", ledger.element_states.get("email_index"))),
            "status": str(states.get("status", ledger.element_states.get("status"))),
        }
    if image.get("backfill_complete") is True or image.get("marker") == "backfill_complete":
        ledger.backfill_complete = True
    if image.get("cleanup_complete") is True or image.get("marker") == "cleanup_complete":
        ledger.cleanup_complete = True


def apply_forward(ledger: Ledger, record) -> bool:
    """Redo one record. Checkpoint records are not applied. Returns True for a row image."""
    if record.kind in ("update", "compensation"):
        apply_image(ledger, record.after)
        ledger.applied_lsn = record.lsn
        return True
    if record.kind == "commit":
        apply_image(ledger, record.after)
        ledger.applied_lsn = record.lsn
        return False
    if record.kind in ("abort", "checkpoint") or record.is_unknown:
        return False
    return False
