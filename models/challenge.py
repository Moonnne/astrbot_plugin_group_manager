"""One transient verification question for a membership lifetime."""

from dataclasses import dataclass

from .scope import Scope


@dataclass
class Challenge:
    scope: Scope
    qq: str
    session: str
    expression: str
    answer: int
    created_at: float
    state: str = "delivery"
    deadline: float = 0
    next_attempt: float = 0
    attempts: int = 0
    options: tuple[int, ...] = ()
