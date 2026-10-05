"""Library entry. `run` returns a status and does not exit the process."""

from __future__ import annotations

import argparse
import difflib
import errno
import inspect
import json
import logging
import os
import sys
import traceback
from typing import BinaryIO, Callable, List, Mapping, Optional, Sequence, TextIO, Tuple

from batchnote import __version__, status
from batchnote.problems import Problem, emit

PROG = "batchnote"
REJECT_STAMP = "[[reject]]"
HELP_EPILOG = """\
Public statuses: 0 success, 1 failure, 2 usage.
A file containing the stamp [[reject]] is a data error and is skipped.
stdout is one JSON object per successful file, with keys path, bytes, and text.
Diagnostics go to stderr. The default report is human. --report json is additive.
Scraping the human line or the detail string is soft-deprecated.
Machine readers use --report json and read type and locator.
Options go before operands. Every token after -- is an operand.\
"""

LOGGER = logging.getLogger("batchnote")
if not any(isinstance(handler, logging.NullHandler) for handler in LOGGER.handlers):
    LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False

SUGGEST_ON_ERROR = "suggest_on_error" in inspect.signature(
    argparse.ArgumentParser.__init__
).parameters
_COLOR_SUPPORTED = "color" in inspect.signature(argparse.ArgumentParser.__init__).parameters
_SUGGESTION_CUTOFF = 0.8
_VALUED_OPTIONS = {"--report", "--config"}

Opener = Callable[[str], BinaryIO]


class _EarlyExit(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


class _DebugHandler(logging.StreamHandler):
    """Debug lines are best effort. A dead stderr must not print a logging traceback."""

    def handleError(self, record: logging.LogRecord) -> None:
        return None


class _LogState:
    def __init__(self) -> None:
        self.handler: Optional[logging.Handler] = None
        self.previous: Optional[int] = None


class ConfigError(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def _open_binary(path: str) -> BinaryIO:
    try:
        return open(path, "rb")
    except PermissionError:
        # Windows reports a directory operand as EACCES. Name the real cause so
        # the problem type is io on every platform.
        if os.path.isdir(path):
            raise IsADirectoryError(errno.EISDIR, os.strerror(errno.EISDIR), path) from None
        raise


def _column_of(line: str, index: int) -> int:
    """1-based column. Tabs advance to the next stop of width 8."""
    column = 1
    for position, char in enumerate(line):
        if position == index:
            return column
        if char == "\t":
            column += 8 - ((column - 1) % 8)
        else:
            column += 1
    return column


def _find_stamp(text: str) -> Optional[Tuple[int, int]]:
    for line_number, line in enumerate(text.splitlines(), start=1):
        index = line.find(REJECT_STAMP)
        if index != -1:
            return line_number, _column_of(line, index)
    return None


def _read_bytes(opener: Opener, path: str) -> bytes:
    handle = opener(path)
    try:
        data = handle.read()
    finally:
        close = getattr(handle, "close", None)
        if callable(close):
            close()
    if isinstance(data, bytearray):
        return bytes(data)
    if not isinstance(data, bytes):
        raise RuntimeError("opener did not return bytes")
    return data


def _os_detail(path: str, exc: OSError) -> str:
    name = exc.filename if isinstance(exc.filename, str) and exc.filename else path
    reason = exc.strerror or "operating system error"
    return f"cannot read {name}: {reason}"


def _locator(path: str, argv_index: Optional[int], line: Optional[int] = None, column: Optional[int] = None) -> dict:
    locator: dict = {"path": path}
    if line is not None:
        locator["line"] = line
    if column is not None:
        locator["column"] = column
    if argv_index is not None:
        locator["argv_index"] = argv_index
    return locator


def _report_mode(argv: Sequence[str]) -> str:
    mode = "human"
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token == "--report" and index + 1 < len(argv):
            mode = argv[index + 1]
            index += 2
            continue
        if token.startswith("--report="):
            mode = token.split("=", 1)[1]
        index += 1
    if mode not in {"human", "json"}:
        return "human"
    return mode


def _known_long_options(parser: argparse.ArgumentParser) -> List[str]:
    names: List[str] = []
    for action in parser._actions:
        for option in action.option_strings:
            if option.startswith("--") and option not in names:
                names.append(option)
    return names


def _suggestion(argv: Sequence[str], parser: argparse.ArgumentParser) -> Tuple[Optional[str], Optional[int]]:
    known = _known_long_options(parser)
    unknown: List[Tuple[str, int]] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            break
        if token in _VALUED_OPTIONS:
            index += 2
            continue
        if token.startswith("--") and "=" in token:
            name = token.split("=", 1)[0]
            if name not in known:
                unknown.append((name, index))
            index += 1
            continue
        if token.startswith("--") and token not in known:
            unknown.append((token, index))
        index += 1
    if len(unknown) != 1:
        return None, None
    name, at = unknown[0]
    matches = difflib.get_close_matches(name, known, n=1, cutoff=_SUGGESTION_CUTOFF)
    if len(matches) != 1:
        return None, at
    return matches[0], at


def _build_parser(stdout: TextIO, stderr: TextIO) -> argparse.ArgumentParser:
    class HelpAction(argparse.Action):
        def __init__(self, option_strings, dest, nargs=None, **kwargs):
            super().__init__(option_strings, dest, nargs=0, **kwargs)

        def __call__(self, parser, namespace, values, option_string=None):
            stdout.write(parser.format_help())
            raise _EarlyExit(status.OK)

    class VersionAction(argparse.Action):
        def __init__(self, option_strings, dest, nargs=None, **kwargs):
            super().__init__(option_strings, dest, nargs=0, **kwargs)

        def __call__(self, parser, namespace, values, option_string=None):
            stdout.write(f"{PROG} {__version__}\n")
            raise _EarlyExit(status.OK)

    kwargs = {
        "prog": PROG,
        "usage": (
            "%(prog)s [-h] [--version] [--report {human,json}] "
            "[--verbose] [--config file] [--] path [path ...]"
        ),
        "description": (
            f"batchnote {__version__} inventories note files.\n"
            "Each successful operand writes one JSON line to stdout."
        ),
        "epilog": HELP_EPILOG,
        "formatter_class": argparse.RawDescriptionHelpFormatter,
        "allow_abbrev": False,
        "add_help": False,
        "exit_on_error": False,
    }
    if _COLOR_SUPPORTED:
        kwargs["color"] = False
    if SUGGEST_ON_ERROR:
        kwargs["suggest_on_error"] = True
    parser = argparse.ArgumentParser(**kwargs)
    parser.add_argument(
        "-h",
        "--help",
        action=HelpAction,
        nargs=0,
        dest=argparse.SUPPRESS,
        help="show this help message and exit",
    )
    parser.add_argument(
        "--version",
        action=VersionAction,
        nargs=0,
        dest=argparse.SUPPRESS,
        help="show the version and exit",
    )
    parser.add_argument(
        "--report",
        choices=("human", "json"),
        default="human",
        help="diagnostic dialect (default: human)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="write debug lines to stderr",
    )
    parser.add_argument(
        "--config",
        metavar="file",
        help="JSON object with a non-empty batch string; checked before operands",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        metavar="path",
        help="note file to inventory",
    )

    def _error(message: str) -> None:
        raise argparse.ArgumentError(None, message)

    def _exit(code: int = 0, message: Optional[str] = None) -> None:
        if message:
            target = stdout if code == 0 else stderr
            target.write(message)
        raise _EarlyExit(status.OK if code in (None, 0) else int(code))

    parser.error = _error  # type: ignore[method-assign]
    parser.exit = _exit  # type: ignore[method-assign]
    return parser


def _from_early(done: _EarlyExit) -> int:
    if done.code == status.OK:
        return status.OK
    if done.code == status.USAGE:
        return status.usage_status()
    return status.FAILURE


def _usage(
    parser: argparse.ArgumentParser,
    argv: Sequence[str],
    stderr: TextIO,
    exc: argparse.ArgumentError,
) -> int:
    report = _report_mode(argv)
    suggestion, argv_index = _suggestion(argv, parser)
    if report != "json":
        try:
            stderr.write(parser.format_usage())
        except OSError:
            return status.FAILURE
    locator = {"argv_index": argv_index} if argv_index is not None else None
    problem = Problem("usage", str(exc), locator=locator, suggestion=suggestion)
    if not emit(problem, stderr, prog=PROG, report=report):
        return status.FAILURE
    return status.usage_status()


def _attach_verbose(stderr: TextIO, enabled: bool, log: _LogState) -> None:
    if not enabled:
        return
    handler = _DebugHandler(stderr)
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("batchnote: debug: %(message)s"))
    LOGGER.addHandler(handler)
    log.handler = handler
    log.previous = LOGGER.level
    LOGGER.setLevel(logging.INFO)


def _detach(log: _LogState) -> None:
    if log.handler is None:
        return
    LOGGER.removeHandler(log.handler)
    log.handler.close()
    if log.previous is not None:
        LOGGER.setLevel(log.previous)
    log.handler = None


def _emit_failure(
    kind: str,
    detail: str,
    stderr: TextIO,
    report: str,
    locator: Optional[Mapping[str, object]] = None,
    exc: Optional[BaseException] = None,
) -> bool:
    problem = Problem(kind, detail, locator=locator)
    wrote = emit(problem, stderr, prog=PROG, report=report)
    if exc is not None and os.environ.get("BATCHNOTE_DEBUG") == "1":
        try:
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=stderr)
        except OSError:
            return False
    return wrote


def _parse_config(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError("configuration is not utf-8") from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConfigError("configuration is not a json object") from exc
    if not isinstance(document, dict):
        raise ConfigError("configuration requires a batch string")
    batch = document.get("batch")
    if not isinstance(batch, str) or batch == "":
        raise ConfigError("configuration requires a batch string")
    return batch


def _operand_positions(argv: Sequence[str], paths: Sequence[str]) -> List[Optional[int]]:
    positions: List[Optional[int]] = []
    cursor = 0
    tokens = list(argv)
    # After the first "--" every token is an operand, including option names.
    operands_only = False
    for path in paths:
        found: Optional[int] = None
        while cursor < len(tokens):
            token = tokens[cursor]
            if not operands_only:
                if token == "--":
                    operands_only = True
                    cursor += 1
                    continue
                if token in _VALUED_OPTIONS:
                    cursor += 2
                    continue
                if token.startswith("--report=") or token.startswith("--config="):
                    cursor += 1
                    continue
            if token == path:
                found = cursor
                cursor += 1
                break
            cursor += 1
        positions.append(found)
    return positions


def _load_config(
    path: str,
    opener: Opener,
    stderr: TextIO,
    report: str,
) -> bool:
    """Return True when the config gate passes. Failures return immediately."""
    LOGGER.info("reading config %s", path)
    try:
        raw = _read_bytes(opener, path)
    except OSError as exc:
        _emit_failure("config", _os_detail(path, exc), stderr, report, {"path": path})
        return False
    except Exception as exc:  # noqa: BLE001 — software boundary, not a traceback by default
        _emit_failure(
            "software",
            "internal invariant failed",
            stderr,
            report,
            {"path": path},
            exc,
        )
        return False
    try:
        _parse_config(raw)
    except ConfigError as exc:
        _emit_failure("config", exc.detail, stderr, report, {"path": path})
        return False
    LOGGER.info("loaded config %s", path)
    return True


def _write_record(stdout: TextIO, path: str, raw: bytes, text: str) -> None:
    record = {"path": path, "bytes": len(raw), "text": text}
    line = json.dumps(record, ensure_ascii=True) + "\n"
    stdout.write(line)


def _one_operand(
    path: str,
    argv_index: Optional[int],
    opener: Opener,
    stdout: TextIO,
    stderr: TextIO,
    report: str,
) -> str:
    """Return 'ok', 'fail', or 'stop'."""
    LOGGER.info("reading %s", path)
    locator = _locator(path, argv_index)
    try:
        raw = _read_bytes(opener, path)
    except FileNotFoundError as exc:
        _emit_failure("no-input", _os_detail(path, exc), stderr, report, locator)
        LOGGER.info("skipped %s", path)
        return "fail"
    except PermissionError as exc:
        _emit_failure("no-input", _os_detail(path, exc), stderr, report, locator)
        LOGGER.info("skipped %s", path)
        return "fail"
    except IsADirectoryError as exc:
        _emit_failure("io", _os_detail(path, exc), stderr, report, locator)
        LOGGER.info("skipped %s", path)
        return "fail"
    except OSError as exc:
        _emit_failure("os", _os_detail(path, exc), stderr, report, locator)
        return "stop"
    except Exception as exc:  # noqa: BLE001 — software boundary, not a traceback by default
        _emit_failure(
            "software",
            "internal invariant failed",
            stderr,
            report,
            locator,
            exc,
        )
        return "stop"

    text = raw.decode("utf-8", "surrogateescape")
    stamp = _find_stamp(text)
    if stamp is not None:
        line, column = stamp
        _emit_failure(
            "data",
            "rejected note stamp",
            stderr,
            report,
            _locator(path, argv_index, line, column),
        )
        LOGGER.info("skipped %s", path)
        return "fail"

    try:
        _write_record(stdout, path, raw, text)
    except OSError as exc:
        reason = exc.strerror or "input/output error"
        _emit_failure("io", f"cannot write stdout: {reason}", stderr, report)
        return "stop"
    except Exception as exc:  # noqa: BLE001 — software boundary, not a traceback by default
        _emit_failure(
            "software",
            "internal invariant failed",
            stderr,
            report,
            locator,
            exc,
        )
        return "stop"
    LOGGER.info("wrote %s (%s bytes)", path, len(raw))
    return "ok"


def _run(
    argv: List[str],
    stdout: TextIO,
    stderr: TextIO,
    opener: Opener,
    log: _LogState,
) -> int:
    parser = _build_parser(stdout, stderr)
    try:
        namespace, extras = parser.parse_known_args(argv)
        if extras:
            joined = " ".join(extras)
            raise argparse.ArgumentError(None, f"unrecognized arguments: {joined}")
        if not namespace.paths:
            raise argparse.ArgumentError(None, "missing operand")
    except _EarlyExit as done:
        return _from_early(done)
    except argparse.ArgumentError as exc:
        return _usage(parser, argv, stderr, exc)

    _attach_verbose(stderr, bool(namespace.verbose), log)
    report = namespace.report
    if namespace.config:
        if not _load_config(namespace.config, opener, stderr, report):
            return status.FAILURE

    positions = _operand_positions(argv, namespace.paths)
    failures = 0
    for path, argv_index in zip(namespace.paths, positions):
        outcome = _one_operand(path, argv_index, opener, stdout, stderr, report)
        if outcome == "stop":
            return status.FAILURE
        if outcome == "fail":
            failures += 1
    result = status.finalize(failures)
    LOGGER.info("recoverable failures: %s", failures)
    return result


def run(
    argv: Sequence[str],
    *,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    opener: Optional[Opener] = None,
) -> int:
    """Run one invocation. Returns 0, 1, or 2. Does not exit the process."""
    out = sys.stdout if stdout is None else stdout
    err = sys.stderr if stderr is None else stderr
    open_path = _open_binary if opener is None else opener
    log = _LogState()
    try:
        return _run(list(argv), out, err, open_path, log)
    finally:
        _detach(log)
