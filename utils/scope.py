"""Stable identity encoding without transport or business dependencies."""

import json
import re

from ..models.scope import Scope


def scope_key(scope: Scope) -> str:
    return json.dumps([scope.platform, scope.bot, scope.group], separators=(",", ":"))


def group_umo(scope: Scope) -> str:
    return f"{scope.platform}:GroupMessage:{scope.group}"


def parse_scope(value: str) -> Scope:
    return Scope(*json.loads(value))


def valid_selection(value):
    if re.fullmatch(r"[1-9][0-9]{4,14}", value, re.ASCII):
        return True
    if ":GroupMessage:" not in value:
        return False
    platform, group = value.split(":GroupMessage:", 1)
    return bool(
        platform
        and len(platform) <= 256
        and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", group, re.ASCII)
    )


def public_identity(scope, user):
    return f"{scope.bot}:{user}" if scope.bot.startswith("qqofficial:") else user
