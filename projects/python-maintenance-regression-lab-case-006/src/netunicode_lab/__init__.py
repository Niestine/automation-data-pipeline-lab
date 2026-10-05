"""Partner-note interchange: strict UTF-8 in, Net-Unicode CSV out."""

from .diagnose import diagnose_mojibake
from .emit import assert_interchange_bytes, emit_interchange_csv
from .files import atomic_write_bytes
from .ingest import read_foreign_text

__all__ = [
    "assert_interchange_bytes",
    "atomic_write_bytes",
    "diagnose_mojibake",
    "emit_interchange_csv",
    "read_foreign_text",
]
