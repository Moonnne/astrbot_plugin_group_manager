"""Named SQLite transactions with cancellation-safe connection ownership."""

import asyncio
from pathlib import Path

import aiosqlite


class Repository:
    def __init__(self, path: Path, resources: Path):
        self.path = path
        self.resources = resources
        self.lock = asyncio.Lock()
        self.db = None
        self.queries = {}

    async def open(self):
        text = (self.resources / "queries.sql").read_text(encoding="utf-8")
        for block in text.split("-- name: ")[1:]:
            name, sql = block.split("\n", 1)
            self.queries[name.strip()] = sql.strip()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        try:
            await self.db.executescript(
                (self.resources / "schema.sql").read_text(encoding="utf-8")
            )
            if (await self.one("version"))["version"] != 1:
                raise RuntimeError("Unsupported database schema version")
        except BaseException:
            await self.db.close()
            self.db = None
            raise

    async def _run(self, jobs, write):
        async with self.lock:
            results = []
            try:
                if write:
                    await self.db.execute(self.queries["begin"])
                for name, params in jobs:
                    async with self.db.execute(self.queries[name], params) as cursor:
                        rows = [dict(row) for row in await cursor.fetchall()]
                        results.append((rows, cursor.rowcount))
                if write:
                    await self.db.execute(self.queries["commit"])
            except BaseException:
                if write and self.db.in_transaction:
                    await self.db.execute(self.queries["rollback"])
                raise
            return results

    async def batch(self, jobs, *, write=True):
        """Finish queued SQL before propagating cancellation.

        Args:
            jobs: Ordered pairs of named SQL and bound parameters.
            write: Whether the batch requires a write transaction.

        Returns:
            Materialized rows and affected counts for each statement.
        """
        task = asyncio.create_task(self._run(jobs, write))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    async def one(self, name, params=None):
        rows = await self.all(name, params)
        return rows[0] if rows else None

    async def all(self, name, params=None):
        return (await self.batch([(name, params or {})], write=False))[0][0]

    async def execute(self, name, params=None):
        return (await self.batch([(name, params or {})]))[0][1]

    async def flags(self, scope, qq):
        params = {"scope": scope.key, "qq": qq}
        results = await self.batch(
            [("flags_local", params), ("flags_public", params)], write=False
        )
        local, public = results[0][0], results[1][0]
        return {
            "white": any(row["kind"] == "white" for row in local),
            "black": any(row["kind"] == "black" for row in local),
            "public": bool(public),
            "entries": local,
            "public_entries": public,
        }

    async def close(self):
        async with self.lock:
            if self.db:
                try:
                    async with self.db.execute(self.queries["checkpoint"]) as cursor:
                        await cursor.fetchall()
                finally:
                    await self.db.close()
                    self.db = None
