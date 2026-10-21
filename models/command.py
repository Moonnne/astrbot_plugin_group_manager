"""Validated management command values."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Command:
    verb: str
    target: str = ""
    note: str = ""
