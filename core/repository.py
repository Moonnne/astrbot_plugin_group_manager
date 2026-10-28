"""Durable member aggregates and local/public list operations."""

from ..utils.database import Database
from ..utils.scope import public_identity, scope_key


class Repository(Database):
    async def member(self, scope, qq):
        return await self.one("member", {"scope": scope_key(scope), "qq": qq})

    async def flags(self, scope, qq):
        params = {"scope": scope_key(scope), "qq": qq}
        results = await self.batch(
            [
                ("flags_local", params),
                ("flags_public", {"qq": public_identity(scope, qq)}),
            ],
            write=False,
        )
        local, public = results[0][0], results[1][0]
        if scope.bot.startswith("qqofficial:"):
            for row in public:
                row["actor"] = row["actor"].removeprefix(scope.bot + ":")
                row["qq"] = qq
        return {
            "white": any(row["kind"] == "white" for row in local),
            "black": any(row["kind"] == "black" for row in local),
            "public": bool(public),
            "entries": local,
            "public_entries": public,
        }

    async def edit_list(self, scope, actor, cmd, superuser, now):
        p = {
            "scope": scope_key(scope),
            "actor": actor,
            "qq": cmd.target,
            "now": now,
            "note": cmd.note,
            "super": int(superuser),
            "kind": "white" if cmd.verb in {"wl", "rmwl"} else "black",
        }
        name = {
            "pb": "add_public",
            "rmpb": "revoke_public",
            "ban": "add_local",
            "wl": "add_local",
            "rmb": "revoke_local",
            "rmwl": "revoke_local",
        }[cmd.verb]
        if cmd.verb in {"pb", "rmpb"}:
            p["qq"] = public_identity(scope, cmd.target)
            p["actor"] = public_identity(scope, actor)
        return await self.execute(name, p)

    async def verification(self, scope, qq, session, state, now):
        await self.execute(
            "verification",
            {
                "scope": scope_key(scope),
                "qq": qq,
                "session": session,
                "state": state,
                "now": now,
            },
        )

    async def history(self, scope, user, limit, offset):
        return await self.all(
            "history_public",
            {
                "scope": scope_key(scope),
                "qq": user,
                "limit": limit,
                "offset": offset,
                "domain": scope.bot if scope.bot.startswith("qqofficial:") else "",
            },
        )
