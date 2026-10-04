"""Tool-usable RFC 4180 CSV export of canonical products."""

from __future__ import annotations

from io import StringIO
from pathlib import Path
import csv

from .errors import CheckpointError
from .models import CSV_FIELDS, Product
from .persist import atomic_write_text, safe_stem


def render_csv(products: list[Product]) -> str:
    buffer = StringIO()
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(CSV_FIELDS),
        extrasaction="ignore",
        lineterminator="\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writeheader()
    for product in sorted(products, key=lambda item: item.sku):
        row = product.csv_row()
        writer.writerow({key: row[key] for key in CSV_FIELDS})
    return buffer.getvalue()


def write_csv(path: Path, products: list[Product]) -> str:
    text = render_csv(products)
    try:
        atomic_write_text(path, text)
    except OSError as exc:
        raise CheckpointError(f"could not write csv {path}: {exc}") from exc
    return text


def csv_path_for(directory: Path, job_id: str) -> Path:
    return Path(directory) / f"{safe_stem(job_id)}.csv"

