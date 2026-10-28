"""Interactive content for adapters that support native command cards."""

from dataclasses import dataclass

from .button import Button


@dataclass(frozen=True)
class ActionCard:
    text: str
    buttons: tuple[Button, ...]
