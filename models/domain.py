"""Pure domain rules and wire-independent values."""

import json
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

SHANGHAI = timezone(timedelta(hours=8), "Asia/Shanghai")
COMMAND = re.compile(r"^/?(g|ans)(?=\s|$)", re.ASCII)
QQ = re.compile(r"^[1-9][0-9]{4,14}$", re.ASCII)
GROUP_ID = re.compile(r"^[1-9][0-9]{4,14}$", re.ASCII)


@dataclass(frozen=True)
class Scope:
    platform: str
    bot: str
    group: str

    @property
    def key(self) -> str:
        return json.dumps([self.platform, self.bot, self.group], separators=(",", ":"))

    @classmethod
    def parse(cls, key: str) -> "Scope":
        return cls(*json.loads(key))


@dataclass(frozen=True)
class Command:
    verb: str
    target: str = ""
    note: str = ""


def parse_command(text: str) -> Command | None:
    """Parse only owned command forms without raising user-facing errors."""
    match = COMMAND.match(text.strip())
    if not match:
        return None
    parts = text.strip()[match.end() :].strip().split()
    if match[1] == "ans":
        if len(parts) == 1 and re.fullmatch(r"-?[0-9]{1,8}", parts[0]):
            return Command("ans", parts[0])
        return None
    if not parts or parts == ["help"]:
        return Command("help")
    verb = parts[0]
    if verb in {"on", "off", "auto"} and len(parts) == 1:
        return Command(verb)
    if parts == ["auto", "skip_ban"]:
        return Command("skip_ban")
    if verb in {"y", "n"} and len(parts) <= 2:
        target = parts[1] if len(parts) == 2 else ""
        if not target or target == "all" or re.fullmatch(r"[1-9][0-9]{0,5}", target):
            return Command(verb, target)
    if verb in {"ban", "wl", "pb"} and len(parts) >= 3 and QQ.fullmatch(parts[1]):
        note = " ".join(parts[2:])
        if len(note) <= 1000:
            return Command(verb, parts[1], note)
    if verb in {"rmb", "rmwl", "rmpb", "info"} and len(parts) == 2:
        if QQ.fullmatch(parts[1]):
            return Command(verb, parts[1])
    return None


def decision(settings: dict, flags: dict) -> str:
    if not settings["enabled"]:
        return "ignore"
    if flags["white"]:
        return "approve"
    if flags["black"] or flags["public"]:
        return "reject" if settings["skip_ban"] else "manual"
    return "approve" if settings["auto_approve"] else "manual"


def display_time(value: float | None) -> str:
    if not value:
        return "未知"
    return datetime.fromtimestamp(value, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S UTC+8")


def light_theme(now: float) -> bool:
    return 6 <= datetime.fromtimestamp(now, SHANGHAI).hour < 18


def arithmetic(rng=None) -> tuple[str, int]:
    """Generate a safe expression; never evaluate received text."""
    rng = rng or random.SystemRandom()
    a, b, c = (rng.randint(1, 9) for _ in range(3))
    kind = rng.randrange(4)
    if kind == 0:
        return f"{a} + {b}", a + b
    if kind == 1:
        a, b = max(a, b), min(a, b)
        return f"{a} - {b}", a - b
    if kind == 2:
        return rf"{a} \times {b}", a * b
    return rf"({a} + {b}) \times {c}", (a + b) * c
