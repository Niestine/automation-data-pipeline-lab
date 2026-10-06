"""Logger tree for propagation and the first-call basicConfig edge.

The two-lock deadlock itself is a yield-lab scenario (``log_deadlock``).
This module is the sequential half: handler lists change only through
add/remove, and a handler attached at two levels is delivered twice while
``propagate`` is true. ``basicConfig`` on the first module-level call is the
behavior named by the logging packet card; the archived extract stops before
the Thread Safety section. ``lock_order`` records the card's acquisition order
by name. This sequential model takes no locks.
"""

from __future__ import annotations


class Record:
    def __init__(self, level: int, message: str, logger_name: str) -> None:
        self.level = level
        self.message = message
        self.logger_name = logger_name


class Handler:
    def __init__(self, name: str, level: int = 0) -> None:
        self.name = name
        self.level = level
        self.records: list[Record] = []


class Logger:
    def __init__(self, name: str, parent: "Logger | None" = None) -> None:
        self.name = name
        self.parent = parent
        self.handlers: list[Handler] = []
        self.propagate = True
        self.disabled = False

    def addHandler(self, handler: Handler) -> None:
        if handler not in self.handlers:
            self.handlers.append(handler)

    def removeHandler(self, handler: Handler) -> None:
        if handler in self.handlers:
            self.handlers.remove(handler)


class Manager:
    def __init__(self) -> None:
        self.root = Logger("root")
        self.loggers = {"root": self.root}
        self.configured = False
        self.lock_order: list[str] = []

    def getLogger(self, name: str) -> Logger:
        if name in ("", "root"):
            return self.root
        logger = self.loggers.get(name)
        if logger is None:
            logger = Logger(name, self.root)
            self.loggers[name] = logger
        return logger

    def basicConfig(self, handler: Handler | None = None) -> None:
        chosen = handler or Handler("lastResort")
        self.lock_order.append("module")
        self.lock_order.append(chosen.name)
        if not self.root.handlers:
            self.root.addHandler(chosen)
        self.configured = True
        self.lock_order.append(f"rel:{chosen.name}")
        self.lock_order.append("rel:module")

    def info(self, name: str, message: str) -> None:
        if name in ("", "root") and not self.root.handlers:
            self.basicConfig()
        self.emit(self.getLogger(name), 20, message)

    def emit(self, logger: Logger, level: int, message: str) -> Record:
        record = Record(level, message, logger.name)
        current: Logger | None = logger
        while current is not None:
            if not current.disabled:
                for handler in current.handlers:
                    if level < handler.level:
                        continue
                    handler.records.append(record)
            if not current.propagate:
                break
            current = current.parent
        return record
