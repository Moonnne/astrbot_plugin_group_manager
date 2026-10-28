"""A command button with an optional target-member restriction."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Button:
    label: str
    command: str
    member: str = ""
