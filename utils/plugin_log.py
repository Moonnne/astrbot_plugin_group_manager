"""Bounded plugin diagnostics with asynchronous persistence and secret redaction."""

import hashlib
import logging
import os
import re
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

SECRET = re.compile(
    r"""(?i)(["']?(?:access_?token|token|authorization|password|secret|cookie)["']?\s*[:=]\s*["']?)(?:Bearer\s+)?[^\s,;"']+"""
)


def redact(value):
    return SECRET.sub(r"\1<REDACTED>", str(value))


def error_detail(exc):
    return f"{type(exc).__name__}: {redact(str(exc))[:500]}"


def flag_ref(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:12] if value else "missing"


def event_summary(raw):
    fields = {
        key: raw.get(key)
        for key in (
            "post_type",
            "request_type",
            "notice_type",
            "sub_type",
            "self_id",
            "group_id",
            "user_id",
            "operator_id",
            "invited_id",
            "time",
        )
        if key in raw
    }
    fields["flag_ref"] = flag_ref(raw.get("flag"))
    fields["comment_chars"] = len(str(raw.get("comment") or ""))
    return fields


class TailLog(logging.Handler):
    LIMIT = 1000
    LINE_LIMIT = 2000

    def __init__(self, path: Path, report_error):
        super().__init__(logging.DEBUG)
        self.path, self.report_error = path, report_error
        self.lines = deque(maxlen=self.LIMIT)
        self.condition = threading.Condition()
        self.dirty = True
        self.stopping = False
        self.failed = False
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as source:
                for line in source:
                    self.lines.append(redact(line.rstrip("\r\n"))[: self.LINE_LIMIT])
        self.worker = threading.Thread(
            target=self._run, name="monitor-log", daemon=True
        )
        self.worker.start()

    def emit(self, record):
        try:
            text = redact(record.getMessage())
            stamp = datetime.fromtimestamp(
                record.created, timezone(timedelta(hours=8))
            ).strftime("%Y-%m-%d %H:%M:%S UTC+8")
            with self.condition:
                if self.stopping:
                    return
                for line in text.splitlines() or [""]:
                    self.lines.append(
                        f"{stamp} [{record.levelname}] {line}"[: self.LINE_LIMIT]
                    )
                self.dirty = True
                self.condition.notify_all()
        except Exception:
            self.handleError(record)

    def _run(self):
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.dirty or self.stopping)
                self.condition.wait_for(lambda: self.stopping, timeout=0.2)
                snapshot = list(self.lines)
                self.dirty = False
                stopping = self.stopping
            try:
                temporary = self.path.with_suffix(".log.tmp")
                temporary.write_bytes(
                    ("\r\n".join(snapshot) + ("\r\n" if snapshot else "")).encode(
                        "utf-8"
                    )
                )
                os.replace(temporary, self.path)
                self.failed = False
            except OSError as exc:
                if not self.failed:
                    self.report_error(
                        "Monitor log persistence failed: %s", type(exc).__name__
                    )
                self.failed = True
            if stopping:
                break

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.worker.join()
        super().close()


class PluginLog(logging.LoggerAdapter):
    def __init__(self, host_logger, path: Path):
        super().__init__(host_logger, {})
        self.sink = TailLog(path, host_logger.error)

    def log(self, level, msg, *args, **kwargs):
        # File diagnostics remain complete even when host console logging is muted.
        record = self.logger.makeRecord(self.logger.name, level, "", 0, msg, args, None)
        self.sink.handle(record)
        self.logger.log(level, msg, *args, **kwargs)

    def close(self):
        self.sink.close()
