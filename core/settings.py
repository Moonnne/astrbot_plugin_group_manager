"""Per-UMO JSON configuration, with separate bot state and atomic replacement."""

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import quote

from ..utils.errors import error_detail
from ..utils.scope import group_umo

DEFAULTS = {"enabled": False, "auto_approve": False, "skip_ban": False, "revision": 0}


class Settings:
    def __init__(self, directory: Path, *, logger=None):
        self.directory = directory
        self.locks = [asyncio.Lock() for _ in range(32)]
        self.log = logger

    def path(self, scope):
        return self.directory / (quote(group_umo(scope), safe="") + ".json")

    def _read(self, scope):
        path = self.path(scope)
        if not path.exists():
            return {"umo": group_umo(scope), "bots": {}}
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("umo") != group_umo(scope) or not isinstance(
            data.get("bots"), dict
        ):
            raise ValueError("Invalid group settings file")
        return data

    async def get(self, scope):
        async with self.locks[hash(group_umo(scope)) % len(self.locks)]:
            try:
                data = await asyncio.to_thread(self._read, scope)
            except Exception as exc:
                if self.log:
                    self.log.error(
                        "settings.read_failed umo=%s bot=%s error=%s",
                        group_umo(scope),
                        scope.bot,
                        error_detail(exc),
                    )
                raise
        row = {**DEFAULTS, **data["bots"].get(scope.bot, {}), "umo": group_umo(scope)}
        for key in ("enabled", "auto_approve", "skip_ban"):
            if type(row[key]) is not bool:
                raise ValueError("Invalid group setting type")
        return row

    async def update(self, scope, actor, now, **changes):
        async with self.locks[hash(group_umo(scope)) % len(self.locks)]:

            def write():
                data = self._read(scope)
                row = {**DEFAULTS, **data["bots"].get(scope.bot, {})}
                row.update(
                    changes,
                    updated_by=actor,
                    updated_at=now,
                    revision=row["revision"] + 1,
                )
                data["bots"][scope.bot] = row
                self.directory.mkdir(parents=True, exist_ok=True)
                path = self.path(scope)
                temporary = path.with_suffix(".json.tmp")
                temporary.write_bytes(
                    (json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                    .replace("\n", "\r\n")
                    .encode("utf-8")
                )
                os.replace(temporary, path)
                return {**row, "umo": group_umo(scope)}

            task = asyncio.create_task(asyncio.to_thread(write))
            try:
                result = await asyncio.shield(task)
                return result
            except asyncio.CancelledError:
                await task
                raise
