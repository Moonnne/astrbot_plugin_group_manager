"""Serialized SQLite access with cancellation-safe transactions."""

import asyncio
from pathlib import Path

import aiosqlite

from .errors import error_detail


class Database:
    def __init__(self, path: Path, resources: Path, *, logger=None):
        self.path, self.resources = path, resources
        self.lock = asyncio.Lock()
        self.db = None
        self.queries = {}
        self.log = logger

    async def open(self):
        for block in (
            (self.resources / "queries.sql")
            .read_text(encoding="utf-8")
            .split("-- name: ")[1:]
        ):
            name, sql = block.split("\n", 1)
            self.queries[name.strip()] = sql.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        try:
            await self.db.executescript(
                (self.resources / "schema.sql").read_text(encoding="utf-8")
            )
        except BaseException:
            await self.db.close()
            self.db = None
            raise

    async def _run(self, jobs, write):
        names = sorted({name for name, _ in jobs})
        async with self.lock:
            results = []
            try:
                # A read batch also holds a consistent snapshot.
                async with self.db.execute(
                    self.queries["begin_write" if write else "begin_read"]
                ):
                    pass
                for name, params in jobs:
                    async with self.db.execute(self.queries[name], params) as cursor:
                        results.append(
                            (
                                [dict(row) for row in await cursor.fetchall()],
                                cursor.rowcount,
                            )
                        )
                await self.db.commit()
            except BaseException as exc:
                await self.db.rollback()
                if self.log:
                    self.log.error(
                        "db.batch_rollback queries=%s error=%s",
                        names,
                        error_detail(exc),
                    )
                raise
            return results

    async def batch(self, jobs, *, write=True):
        task = asyncio.create_task(self._run(jobs, write))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def all(self, name, params=None):
        return (await self.batch([(name, params or {})], write=False))[0][0]

    async def one(self, name, params=None):
        rows = await self.all(name, params)
        return rows[0] if rows else None

    async def execute(self, name, params=None):
        return (await self.batch([(name, params or {})]))[0][1]

    async def close(self):
        async with self.lock:
            if self.db:
                try:
                    async with self.db.execute(self.queries["checkpoint"]) as cursor:
                        await cursor.fetchall()
                finally:
                    await self.db.close()
                    self.db = None
