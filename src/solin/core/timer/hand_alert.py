"""Display state for a meeting timer's hand indicator."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TimerHandAlert:
    manual: bool = False
    names: tuple[str, ...] = ()
