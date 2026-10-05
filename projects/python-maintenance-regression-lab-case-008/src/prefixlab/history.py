"""Adya dependency checker over append-only lists.

Version order is prefix order of the lists clients observed. It is not commit time.
Unacknowledged transactions are neither aborted reads nor committed installs.
"""

from __future__ import annotations

from collections import defaultdict

STATUSES = ("committed", "aborted", "unknown")
EDGE_WRITE = "write-depends"
EDGE_READ = "read-depends"
EDGE_ANTI = "anti-depends"

# Search the smallest edge set first so a G0 cycle is not reported as G2 just
# because an anti-depends edge also connects the same transactions.
CYCLE_STAGES = (
    ("G0", (EDGE_WRITE,)),
    ("G1c", (EDGE_WRITE, EDGE_READ)),
    ("G2", (EDGE_WRITE, EDGE_READ, EDGE_ANTI)),
)


def parse_history(doc: dict) -> dict:
    if not isinstance(doc, dict):
        raise ValueError("history must be an object")
    raw_txns = doc.get("transactions")
    if not isinstance(raw_txns, list):
        raise ValueError("transactions must be a list")
    seen = set()
    transactions = []
    for index, raw in enumerate(raw_txns):
        transactions.append(_parse_txn(raw, index, seen))
    surviving = doc.get("surviving", {})
    if not isinstance(surviving, dict):
        raise ValueError("surviving must be an object")
    clean_surviving = {}
    for key, value in surviving.items():
        if not isinstance(key, str) or not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError("surviving values must be lists of strings")
        clean_surviving[key] = list(value)
    return {"transactions": transactions, "surviving": clean_surviving}


def _parse_txn(raw: dict, index: int, seen: set[str]) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"transaction {index} must be an object")
    txn_id = raw.get("id")
    status = raw.get("status")
    if not isinstance(txn_id, str) or not txn_id:
        raise ValueError(f"transaction {index} requires an id")
    if txn_id in seen:
        raise ValueError(f"duplicate transaction id {txn_id}")
    seen.add(txn_id)
    if status not in STATUSES:
        raise ValueError(f"transaction {txn_id} has status {status!r}")
    ops_raw = raw.get("ops")
    if not isinstance(ops_raw, list):
        raise ValueError(f"transaction {txn_id} ops must be a list")
    ops = [_parse_op(op, txn_id, op_index) for op_index, op in enumerate(ops_raw)]
    return {"id": txn_id, "status": status, "ops": ops}


def _parse_op(raw: dict, txn_id: str, index: int) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{txn_id} op {index} must be an object")
    kind = raw.get("op")
    key = raw.get("key")
    value = raw.get("value")
    if kind not in ("read", "append") or not isinstance(key, str) or not key:
        raise ValueError(f"{txn_id} op {index} is malformed")
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{txn_id} op {index} value must be a list of strings")
    if kind == "read":
        return {"op": "read", "key": key, "value": list(value)}
    tokens = raw.get("tokens")
    if not isinstance(tokens, list) or not tokens or not all(isinstance(item, str) for item in tokens):
        raise ValueError(f"{txn_id} op {index} append requires tokens")
    return {"op": "append", "key": key, "tokens": list(tokens), "value": list(value)}


def check_history(doc: dict) -> dict:
    """Return anomaly, edges, transactions, and missing committed tokens."""

    history = parse_history(doc)
    transactions = history["transactions"]
    g1a = _g1a(transactions)
    if g1a is not None:
        return _result("G1a", g1a["edges"], g1a["transactions"], [])
    g1b = _g1b(transactions)
    if g1b is not None:
        return _result("G1b", g1b["edges"], g1b["transactions"], [])
    edges = _dependency_edges(transactions)
    missing = _missing(transactions, history["surviving"])
    for anomaly, kinds in CYCLE_STAGES:
        cycle = _cycle([edge for edge in edges if edge["kind"] in kinds])
        if cycle is not None:
            return _result(anomaly, cycle["edges"], cycle["transactions"], missing)
    if missing:
        writers = _missing_writers(transactions, missing)
        return _result("lost_append", [], writers, missing)
    return _result(None, [], [], [])


def _result(anomaly, edges, transactions, missing) -> dict:
    return {
        "anomaly": anomaly,
        "edges": edges,
        "transactions": list(transactions),
        "missing": list(missing),
    }


def _g1a(transactions: list[dict]) -> dict | None:
    writers: dict[str, tuple[str, str]] = {}
    for txn in transactions:
        for op in txn["ops"]:
            if op["op"] != "append":
                continue
            for token in op["tokens"]:
                writers.setdefault(token, (txn["id"], txn["status"]))
    for txn in transactions:
        if txn["status"] != "committed":
            continue
        for op in txn["ops"]:
            if op["op"] != "read":
                continue
            for token in op["value"]:
                found = writers.get(token)
                if found is None or found[1] != "aborted":
                    continue
                return {
                    "edges": [
                        {
                            "kind": "aborted-read",
                            "from": found[0],
                            "to": txn["id"],
                            "key": op["key"],
                        }
                    ],
                    "transactions": _ids(found[0], txn["id"]),
                }
    return None


def _g1b(transactions: list[dict]) -> dict | None:
    finals: dict[tuple[str, str], list[str]] = {}
    written: dict[tuple[str, str], set[str]] = defaultdict(set)
    for txn in transactions:
        if txn["status"] != "committed":
            continue
        for op in txn["ops"]:
            if op["op"] != "append":
                continue
            finals[(txn["id"], op["key"])] = list(op["value"])
            written[(txn["id"], op["key"])].update(op["tokens"])
    for txn in transactions:
        if txn["status"] != "committed":
            continue
        for op in txn["ops"]:
            if op["op"] != "read":
                continue
            for (writer, key), final in finals.items():
                if key != op["key"] or writer == txn["id"]:
                    continue
                if not _proper_prefix(op["value"], final):
                    continue
                if not any(token in written[(writer, key)] for token in op["value"]):
                    continue
                return {
                    "edges": [
                        {
                            "kind": "intermediate-read",
                            "from": writer,
                            "to": txn["id"],
                            "key": key,
                        }
                    ],
                    "transactions": _ids(writer, txn["id"]),
                }
    return None


def _dependency_edges(transactions: list[dict]) -> list[dict]:
    installs: dict[str, list[tuple[str, list[str], list[str]]]] = defaultdict(list)
    reads: list[tuple[str, str, list[str]]] = []
    for txn in transactions:
        if txn["status"] != "committed":
            continue
        per_key_tokens: dict[str, list[str]] = defaultdict(list)
        per_key_final: dict[str, list[str]] = {}
        for op in txn["ops"]:
            if op["op"] == "append":
                per_key_tokens[op["key"]].extend(op["tokens"])
                per_key_final[op["key"]] = list(op["value"])
            elif op["op"] == "read":
                reads.append((txn["id"], op["key"], list(op["value"])))
        for key, final in per_key_final.items():
            installs[key].append((txn["id"], final, list(per_key_tokens[key])))
    edges: list[dict] = []
    for key, versions in installs.items():
        edges.extend(_write_edges(key, versions))
        edges.extend(_anti_edges(key, versions, reads))
        edges.extend(_read_edges(key, versions, reads))
    return edges


def _write_edges(key: str, versions: list[tuple[str, list[str], list[str]]]) -> list[dict]:
    edges = []
    for writer, installed, _tokens in versions:
        for other, other_list, other_tokens in versions:
            if writer == other or not _proper_prefix(installed, other_list):
                continue
            if other_list[len(installed) :] != other_tokens:
                continue
            if _between(installed, other_list, versions, writer, other):
                continue
            edges.append({"kind": EDGE_WRITE, "from": writer, "to": other, "key": key})
    return edges


def _read_edges(key: str, versions, reads) -> list[dict]:
    edges = []
    for reader, read_key, value in reads:
        if read_key != key:
            continue
        for writer, installed, _tokens in versions:
            if writer != reader and installed == value:
                edges.append({"kind": EDGE_READ, "from": writer, "to": reader, "key": key})
    return edges


def _anti_edges(key: str, versions, reads) -> list[dict]:
    edges = []
    for reader, read_key, value in reads:
        if read_key != key:
            continue
        for writer, installed, tokens in versions:
            if writer == reader or not _proper_prefix(value, installed):
                continue
            if installed[len(value) :] != tokens:
                continue
            if _between(value, installed, versions, "", writer):
                continue
            edges.append({"kind": EDGE_ANTI, "from": reader, "to": writer, "key": key})
    return edges


def _between(left: list[str], right: list[str], versions, skip_a: str, skip_b: str) -> bool:
    for txn_id, installed, _tokens in versions:
        if txn_id in (skip_a, skip_b):
            continue
        if (_proper_prefix(left, installed) or left == installed) and _proper_prefix(installed, right):
            return True
    return False


def _missing(transactions: list[dict], surviving: dict[str, list[str]]) -> list[str]:
    missing = []
    for txn in transactions:
        if txn["status"] != "committed":
            continue
        for op in txn["ops"]:
            if op["op"] != "append":
                continue
            have = surviving.get(op["key"], [])
            for token in op["tokens"]:
                if token not in have and token not in missing:
                    missing.append(token)
    return missing


def _missing_writers(transactions: list[dict], missing: list[str]) -> list[str]:
    found = []
    wanted = set(missing)
    for txn in transactions:
        if txn["status"] != "committed":
            continue
        for op in txn["ops"]:
            if op["op"] == "append" and any(token in wanted for token in op["tokens"]):
                if txn["id"] not in found:
                    found.append(txn["id"])
    return found


def _cycle(edges: list[dict]) -> dict | None:
    adjacent: dict[str, list[dict]] = defaultdict(list)
    nodes = set()
    for edge in edges:
        adjacent[edge["from"]].append(edge)
        nodes.add(edge["from"])
        nodes.add(edge["to"])
    color: dict[str, int] = {}
    for start in sorted(nodes):
        found = _dfs(start, adjacent, color, [], set())
        if found is None:
            continue
        loop = found + [found[0]]
        cycle_edges = [
            next(edge for edge in edges if edge["from"] == src and edge["to"] == dst)
            for src, dst in zip(loop, loop[1:])
        ]
        return {"transactions": found, "edges": cycle_edges}
    return None


def _dfs(node, adjacent, color, stack, onstack):
    color[node] = 1
    stack.append(node)
    onstack.add(node)
    for edge in adjacent.get(node, []):
        nxt = edge["to"]
        if color.get(nxt) == 2:
            continue
        if nxt in onstack:
            return stack[stack.index(nxt) :]
        if color.get(nxt) != 1:
            found = _dfs(nxt, adjacent, color, stack, onstack)
            if found is not None:
                return found
    stack.pop()
    onstack.remove(node)
    color[node] = 2
    return None


def _proper_prefix(left: list[str], right: list[str]) -> bool:
    return len(left) < len(right) and right[: len(left)] == left


def _ids(*names: str) -> list[str]:
    return sorted(set(names))
