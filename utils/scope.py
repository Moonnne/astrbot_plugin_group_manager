"""Stable identity encoding without transport or business dependencies."""

import json

from ..models.scope import Scope


def scope_key(scope: Scope) -> str:
    return json.dumps([scope.platform, scope.bot, scope.group], separators=(",", ":"))


def group_umo(scope: Scope) -> str:
    return f"{scope.platform}:GroupMessage:{scope.group}"


def parse_scope(value: str) -> Scope:
    return Scope(*json.loads(value))
