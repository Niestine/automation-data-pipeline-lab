"""Async-generator lifetime checks from the asyncio developer notes.

This is a step model of the documented shutdown order, not a host event loop.
``aclosing`` closes the inner generator before the outer one asserts the flag.
A generator primed before the loop is absent from shutdown and can ignore
``GeneratorExit``. A second ``asend`` while one is in progress is rejected.
"""

from __future__ import annotations

from yieldlab.errors import GeneratorReentered, Invariant


class LoopHooks:
    def __init__(self) -> None:
        self.started = False
        self.registered: list[AsyncGen] = []

    def start(self) -> None:
        self.started = True

    def shutdown_asyncgens(self) -> None:
        for gen in list(self.registered):
            gen.aclose()


class AsyncGen:
    def __init__(self, loop: LoopHooks, name: str, inner: "AsyncGen | None" = None) -> None:
        self.loop = loop
        self.name = name
        self.inner = inner
        self.created_in_running_loop = loop.started
        self.running = False
        self.closed = False
        self.hooked = False
        self.primed = False
        self.flag = False

    def prime(self) -> None:
        if self.primed:
            return
        self.primed = True
        if self.loop.started and self.created_in_running_loop:
            self.hooked = True
            self.loop.registered.append(self)

    def asend(self) -> str:
        if self.running:
            raise GeneratorReentered("anext(): asynchronous generator is already running")
        if self.closed:
            raise StopAsyncIteration(self.name)
        self.running = True
        return "yield"

    def resume_done(self) -> None:
        """The in-flight ``asend`` reached its next ``yield``."""
        if not self.running:
            raise Invariant("resume_done without an active asend")
        self.running = False

    def aclose(self) -> None:
        if self.running:
            raise GeneratorReentered("aclose(): asynchronous generator is already running")
        if self.closed:
            return
        self.closed = True
        if self.inner is not None and not self.inner.flag:
            raise Invariant("cursor_rows_shutdown")
        self.flag = True

    def finalize(self) -> None:
        if self.hooked:
            self.aclose()
            return
        raise RuntimeError("async generator ignored GeneratorExit")


def cursor_rows(use_aclosing: bool) -> str:
    loop = LoopHooks()
    loop.start()
    rows = AsyncGen(loop, "rows")
    cursor = AsyncGen(loop, "cursor", inner=rows)
    rows.prime()
    cursor.prime()
    if use_aclosing:
        # ``aclosing`` closes the inner generator before the outer finally runs.
        rows.aclose()
    # Without it, shutdown finalizes the outer generator first and its
    # assertion on the inner flag fails.
    cursor.aclose()
    return "ok"
