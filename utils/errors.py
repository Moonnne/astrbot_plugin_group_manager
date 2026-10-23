"""Concise, redacted exception descriptions for host logging."""

import re

SECRET = re.compile(
    r"""(?i)(["']?(?:access_?token|token|authorization|password|secret|cookie)["']?\s*[:=]\s*["']?)(?:Bearer\s+)?[^\s,;"']+"""
)


def redact(value):
    return SECRET.sub(r"\1<REDACTED>", str(value))


def error_detail(exc):
    return f"{type(exc).__name__}: {redact(str(exc))[:500]}"
