from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path


def as_int(value):
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize(rows):
    deduped = {}
    issues = []

    for index, raw in enumerate(rows):
        row_id = str(raw.get("id") or "").strip()
        if not row_id:
            issues.append({"index": index, "code": "missing_id"})
            continue

        record = {
            "id": row_id,
            "category": str(raw.get("category") or "").strip(),
            "price": as_int(raw.get("price")),
            "stock": as_int(raw.get("stock")),
        }

        if record["price"] is None:
            issues.append({"index": index, "id": row_id, "code": "invalid_price"})
        if record["stock"] is None:
            issues.append({"index": index, "id": row_id, "code": "invalid_stock"})

        if row_id in deduped:
            issues.append({"index": index, "id": row_id, "code": "duplicate_keep_last"})
        deduped[row_id] = record

    records = [deduped[key] for key in sorted(deduped)]
    return records, issues


def summarize(records):
    groups = defaultdict(list)
    for row in records:
        groups[row["category"]].append(row)

    result = {}
    for category in sorted(groups):
        rows = groups[category]
        prices = [r["price"] for r in rows if isinstance(r["price"], int)]
        stocks = [r["stock"] for r in rows if isinstance(r["stock"], int)]
        result[category] = {
            "records": len(rows),
            "price_total": sum(prices),
            "stock_total": sum(stocks),
        }
    return result


def main():
    if len(sys.argv) != 3:
        raise SystemExit("usage: python pipeline.py INPUT.json OUTPUT_DIR")

    src = Path(sys.argv[1])
    out = Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)

    rows = json.loads(src.read_text(encoding="utf-8"))
    records, issues = normalize(rows)
    summary = summarize(records)

    (out / "normalized.json").write_text(
        json.dumps({"records": records, "issues": issues}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    with (out / "normalized.csv").open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "category", "price", "stock"])
        writer.writeheader()
        writer.writerows(records)

    print(json.dumps({
        "records": len(records),
        "issues": len(issues),
        "categories": len(summary),
    }))


if __name__ == "__main__":
    main()
