"""Force a recorded thread-id sequence.

Replay does not re-roll priorities or change points. A tid that is not
enabled, or a sequence that ends before or after the subject, is ``diverged``.
"""

from __future__ import annotations

from typing import Optional

from schedlab.machine import Machine
from schedlab.policies import note_choice
from schedlab.soundness import assert_sound
from schedlab.subjects import Subject


def replay(
    subject: Subject,
    schedule: list[int],
    *,
    n_max: int,
    k: int,
) -> Machine:
    assert_sound(subject)
    machine = Machine.from_subject(subject, n_max=n_max, k=k)
    if machine.terminal is not None:
        if schedule:
            machine.terminal = "diverged"
        return machine
    prev: Optional[int] = None
    for tid in schedule:
        if machine.terminal is not None:
            machine.terminal = "diverged"
            return machine
        if not note_choice(machine, tid, prev, "replay"):
            machine.terminal = "diverged"
            return machine
        prev = tid
        machine.execute(tid)
    if machine.terminal is None:
        machine.terminal = "diverged"
    return machine
