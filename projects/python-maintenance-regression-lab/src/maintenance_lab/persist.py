"""Atomic JSON file writes used by durable stores."""

from __future__ import annotations

from typing import Any
import hashlib
import json
import os
from pathlib import Path


def dump_json(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(dump_json(data))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_stem(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_fields(
    sku: str,
    title: str,
    price_cents: int,
    currency: str,
    stock: int,
    active: bool,
    image_url: str | None,
) -> str:
    payload = json.dumps(
        {
            "active": active,
            "currency": currency,
            "image_url": image_url,
            "price_cents": price_cents,
            "sku": sku,
            "stock": stock,
            "title": title,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
