import sys

from batchnote.run import run

# Human diagnostics are documented as UTF-8 whatever the console locale is.
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")

raise SystemExit(run(sys.argv[1:]))
