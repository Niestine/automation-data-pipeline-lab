"""Differential witness runner.

The same bytes go to legacy_parse and hardened_parse. A crash, a timeout, or a
difference in acceptance or canonical records is a witness. Agreement is a
compatibility fact, not a conformance oracle. Field text is not passed through
a float. Separator and quote-style spelling are the only normalizations, and
they are logged rather than used to erase a disagreement.
"""

from __future__ import annotations

import json
import random
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from .decisions import DECISIONS
from .logsetup import LOG
from .model import AdmitError, ParseFailure, ParseSuccess
from .recognize import hardened_parse, legacy_parse

# bucket_component_condition in lower-case ASCII. The one-letter bucket prefix
# means a stem can never be a bare Windows device name such as nul or con.
_CASE_NAME = re.compile(r"[ynicb](_[a-z0-9]+)+")

@dataclass(frozen=True)
class Witness:
    kind: str
    decision_id: str | None
    bucket: str | None
    normalizations: tuple[str, ...]
    legacy_code: str | None
    hardened_code: str | None
    data: bytes


@dataclass(frozen=True)
class Campaign:
    witnesses: tuple[Witness, ...]
    agreements: int
    unclassified: int
    files_written: int
    classes: tuple[str, ...]
    unclassified_samples: tuple[bytes, ...] = ()


def fold_bytes(data: bytes) -> tuple[bytes, tuple[str, ...]]:
    """Rewrite separator and quote spelling. Field text stays character for character."""

    text = data.decode("utf-8", errors="surrogateescape")
    separated, sep_changed = _fold_separators(text)
    quoted, quote_changed = _fold_quotes(separated)
    notes: list[str] = []
    if sep_changed:
        notes.append("separator")
    if quote_changed:
        notes.append("quote-style")
    return quoted.encode("utf-8", errors="surrogateescape"), tuple(notes)


def spelling_notes(left: bytes, right: bytes) -> tuple[str, ...]:
    folded_left, notes_left = fold_bytes(left)
    folded_right, notes_right = fold_bytes(right)
    if folded_left != folded_right:
        return ()
    notes: list[str] = []
    for note in notes_left + notes_right:
        if note not in notes:
            notes.append(note)
    _log_normalizations("pair", tuple(notes))
    return tuple(notes)


def _log_normalizations(context: str, notes: tuple[str, ...]) -> None:
    # Note names only. The bytes being compared stay out of the log line.
    for note in notes:
        LOG.info("normalization %s context=%s", note, context)


def examine(
    data: bytes,
    *,
    header: str = "absent",
    field_limit: int = 4096,
    mutant: str | None = None,
    timeout: float | None = 1.0,
    legacy_runner=None,
    hardened_runner=None,
) -> Witness:
    legacy_fn = legacy_runner or (lambda: legacy_parse(data, header=header, field_limit=field_limit))
    hardened_fn = hardened_runner or (
        lambda: hardened_parse(data, header=header, field_limit=field_limit, mutant=mutant)
    )
    legacy_state, legacy_value = _run_bounded(legacy_fn, timeout)
    hardened_state, hardened_value = _run_bounded(hardened_fn, timeout)
    _, notes = fold_bytes(data)

    if legacy_state == "timeout" or hardened_state == "timeout":
        return Witness("timeout", None, None, notes, None, None, data)
    if legacy_state == "crash" or hardened_state == "crash":
        return Witness("crash", None, None, notes, None, None, data)

    legacy = legacy_value
    hardened = hardened_value
    legacy_code = legacy.code if isinstance(legacy, ParseFailure) else None
    hardened_code = hardened.code if isinstance(hardened, ParseFailure) else None
    if _equivalent(legacy, hardened):
        return Witness("agree", None, None, (), legacy_code, hardened_code, data)

    decision_id = _decision_of(legacy, hardened)
    bucket = "b" if decision_id in DECISIONS else None
    _log_normalizations("witness", notes)
    return Witness("disagree", decision_id, bucket, notes, legacy_code, hardened_code, data)


def minimize(data: bytes, signature) -> bytes:
    """Delete a byte, a field, or a record while the signature stays the same.

    Stops when a full cycle of deletions changes nothing. Every accepted trial
    is strictly shorter, so the loop ends after at most len(data) cycles.
    """

    target = signature(data)
    current = data
    while True:
        changed = False
        for trial in _deletions(current):
            if trial and signature(trial) == target:
                current = trial
                changed = True
                break
        if not changed:
            break
    return current


def campaign(
    *,
    seed: int,
    budget: int,
    field_limit: int = 8,
    mutant: str | None = None,
    timeout: float | None = 1.0,
) -> Campaign:
    found: dict[tuple, Witness] = {}
    agreements = 0
    unclassified = 0
    samples: list[bytes] = []
    for data in generate(seed, budget, field_limit):
        witness = examine(data, field_limit=field_limit, mutant=mutant, timeout=timeout)
        if witness.kind == "agree":
            agreements += 1
            continue
        if witness.decision_id not in DECISIONS:
            unclassified += 1
            if len(samples) < 5:
                samples.append(data)
            continue

        def signature(candidate: bytes, _limit=field_limit, _mutant=mutant, _timeout=timeout) -> tuple:
            item = examine(candidate, field_limit=_limit, mutant=_mutant, timeout=_timeout)
            return (item.kind, item.decision_id, item.bucket, item.legacy_code, item.hardened_code)

        shrunk = minimize(data, signature)
        key = (shrunk, witness.decision_id, witness.kind)
        if key not in found:
            found[key] = Witness(
                witness.kind,
                witness.decision_id,
                witness.bucket,
                witness.normalizations,
                witness.legacy_code,
                witness.hardened_code,
                shrunk,
            )
    classes = tuple(sorted({item.decision_id or item.kind for item in found.values()}))
    return Campaign(tuple(found.values()), agreements, unclassified, 0, classes, tuple(samples))


def generate(seed: int, budget: int, field_limit: int) -> list[bytes]:
    """Four layers: raw bytes, delimiter lexemes, strict-grammar files, one-byte edits."""

    rng = random.Random(seed)
    items = [
        b"a" * field_limit + b"\r\n",
        b"a" * (field_limit + 1) + b"\r\n",
        b"# c\r\na,b\r\n",
        b"a,b\nc,d\r\n",
        b"\xff,",
        b"\xef\xbb\xbfa,b\r\n",
        b"k1,k2\r\nv1,v2\r\n",
    ]
    lexemes = [b",", b'"', b"'", b"\r\n", b"\n", b"#", b";", b"a", b"\\"]
    while len(items) < budget:
        kind = rng.randrange(4)
        if kind == 0:
            items.append(bytes(rng.randrange(256) for _ in range(rng.randint(0, 6))))
        elif kind == 1:
            items.append(b"".join(rng.choice(lexemes) for _ in range(rng.randint(1, 5))))
        elif kind == 2:
            items.append(_grammar(rng, field_limit))
        else:
            items.append(_edit_one(rng, _grammar(rng, field_limit)))
    return items[:budget]


def admit(directory: Path, name: str, data: bytes, sidecar: dict) -> None:
    """Write a corpus pair. Refuses a witness that has no decision id or bucket.

    The name must be bucket_component_condition in lower-case ASCII, start with
    the sidecar's bucket, and not already exist: a sidecar is never overwritten
    with whichever parser happened to run.
    """

    decision_id = sidecar.get("decision_id")
    bucket = sidecar.get("bucket")
    if not decision_id or not bucket:
        raise AdmitError("refusing unclassified witness")
    if decision_id not in DECISIONS:
        raise AdmitError("unknown decision")
    if bucket not in {"y", "n", "i", "c", "b"}:
        raise AdmitError("unknown bucket")
    stem = name[: -len(".bin")] if name.endswith(".bin") else name
    if not _CASE_NAME.fullmatch(stem) or not stem.startswith(bucket + "_"):
        raise AdmitError("name must be bucket_component_condition for the sidecar bucket")
    blob = directory / (stem + ".bin")
    side = directory / (stem + ".json")
    if blob.exists() or side.exists():
        raise AdmitError("refusing to overwrite an existing corpus case")
    directory.mkdir(parents=True, exist_ok=True)
    blob.write_bytes(data)
    side.write_text(
        json.dumps(sidecar, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _equivalent(legacy: ParseSuccess | ParseFailure, hardened: ParseSuccess | ParseFailure) -> bool:
    if isinstance(legacy, ParseFailure) and isinstance(hardened, ParseFailure):
        return legacy.code == hardened.code and legacy.decision_id == hardened.decision_id
    if isinstance(legacy, ParseSuccess) and isinstance(hardened, ParseSuccess):
        return legacy.header == hardened.header and legacy.records == hardened.records
    return False


def _decision_of(legacy: ParseSuccess | ParseFailure, hardened: ParseSuccess | ParseFailure) -> str | None:
    if isinstance(hardened, ParseFailure):
        return hardened.decision_id
    if isinstance(legacy, ParseSuccess) and legacy.bom_stripped and isinstance(hardened, ParseSuccess):
        if not hardened.bom_stripped:
            return "D-bom"
    if isinstance(legacy, ParseSuccess) and legacy.events:
        return legacy.events[0].decision_id
    if isinstance(legacy, ParseFailure):
        return legacy.decision_id
    return None


def _run_bounded(fn, timeout: float | None):
    if timeout is None:
        try:
            return "ok", fn()
        except Exception:  # noqa: BLE001 - a crash is a witness, not a suite error
            return "crash", None
    box: dict = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001 - recorded as a crash witness
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        return "timeout", None
    if "error" in box:
        return "crash", None
    return "ok", box.get("value")


def _fold_separators(text: str) -> tuple[str, bool]:
    out: list[str] = []
    changed = False
    in_quotes = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == '"':
            in_quotes = not in_quotes
            out.append(ch)
            i += 1
            continue
        if not in_quotes and ch == "\n" and (not out or out[-1] != "\r"):
            out.append("\r")
            out.append("\n")
            changed = True
            i += 1
            continue
        if not in_quotes and ch == "\r" and not (i + 1 < len(text) and text[i + 1] == "\n"):
            out.append("\r")
            out.append("\n")
            changed = True
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out), changed


def _fold_quotes(text: str) -> tuple[str, bool]:
    """Turn a fully single-quoted field into a double-quoted field. Interior text is kept."""

    out: list[str] = []
    changed = False
    i = 0
    n = len(text)
    while i < n:
        if text[i] == '"':
            end = _consume_double(text, i)
            out.append(text[i:end])
            i = end
            continue
        start = i
        while i < n and text[i] not in ",\r\n":
            i += 1
        field = text[start:i]
        if len(field) >= 2 and field[0] == "'" and field[-1] == "'" and "'" not in field[1:-1]:
            inner = field[1:-1]
            out.append('"' + inner.replace('"', '""') + '"')
            changed = True
        else:
            out.append(field)
        if i < n and text[i] in ",\r\n":
            if text[i] == "\r" and i + 1 < n and text[i + 1] == "\n":
                out.append("\r\n")
                i += 2
            else:
                out.append(text[i])
                i += 1
    return "".join(out), changed


def _consume_double(text: str, start: int) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == '"':
            if i + 1 < len(text) and text[i + 1] == '"':
                i += 2
                continue
            return i + 1
        i += 1
    return len(text)


def _grammar(rng: random.Random, field_limit: int) -> bytes:
    rows: list[str] = []
    for _ in range(rng.randint(1, 3)):
        cells: list[str] = []
        for _ in range(rng.randint(1, 3)):
            length = rng.choice([1, 2, 3, min(4, field_limit)])
            body = "".join(rng.choice("abc12 ") for _ in range(length))
            if rng.randrange(5) == 0:
                body = body + "," + body[:1]
            cells.append(_csv_quote(body) if any(ch in body for ch in ",\"\r\n") else body)
        rows.append(",".join(cells))
    trailing = "\r\n" if rng.randrange(2) == 0 else ""
    return ("\r\n".join(rows) + trailing).encode("utf-8")


def _csv_quote(body: str) -> str:
    return '"' + body.replace('"', '""') + '"'


def _edit_one(rng: random.Random, data: bytes) -> bytes:
    if not data:
        return bytes([rng.randrange(256)])
    if rng.randrange(2) == 0:
        at = rng.randrange(len(data) + 1)
        extra = bytes([rng.choice(b",\"\r\n#;")])
        return data[:at] + extra + data[at:]
    at = rng.randrange(len(data))
    return data[:at] + data[at + 1 :]


def _deletions(data: bytes):
    for index in range(len(data)):
        yield data[:index] + data[index + 1 :]
    for record in _record_spans(data):
        trial = data[: record[0]] + data[record[1] :]
        if trial != data:
            yield trial
    for field in _field_spans(data):
        trial = data[: field[0]] + data[field[1] :]
        if trial != data:
            yield trial


def _record_spans(data: bytes):
    parts = []
    start = 0
    i = 0
    while i < len(data):
        if data[i : i + 2] == b"\r\n":
            parts.append((start, i + 2))
            i += 2
            start = i
            continue
        if data[i] in {10, 13}:
            parts.append((start, i + 1))
            i += 1
            start = i
            continue
        i += 1
    if start < len(data):
        parts.append((start, len(data)))
    return parts


def _field_spans(data: bytes):
    spans = []
    start = 0
    for index, byte in enumerate(data):
        if byte == 44:
            spans.append((start, index + 1))
            start = index + 1
    return spans
