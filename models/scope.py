"""Original OneBot group identity."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    platform: str
    bot: str
    group: str
