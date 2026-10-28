"""Strict command parsing and platform-independent moderation actions."""

import re

from ..models.command import Command
from ..utils.formatting import display_time
from ..utils.scope import group_umo, scope_key

COMMAND = re.compile(r"^/?(group|g|ans)(?=\s|$)", re.ASCII)
QQ = re.compile(r"^[1-9][0-9]{4,14}$", re.ASCII)


def parse_command(text: str, valid_user=QQ.fullmatch) -> Command | None:
    """Parse only owned command forms without raising user-facing errors."""
    match = COMMAND.match(text.strip())
    if not match:
        return None
    parts = text.strip()[match.end() :].strip().split()
    if match[1] == "ans":
        if len(parts) in {1, 2} and re.fullmatch(r"-?[0-9]{1,8}", parts[0]):
            if len(parts) == 1:
                return Command("ans", parts[0])
            if re.fullmatch(r"v_[0-9a-f]{32}", parts[1]):
                return Command("ans", parts[0], context_id=parts[1][2:])
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
        if (
            not target
            or target == "all"
            or re.fullmatch(r"[1-9][0-9]{0,5}|r_[0-9a-f]{32}", target)
        ):
            return Command(verb, target)
    if verb in {"ban", "wl", "pb"} and len(parts) >= 3 and valid_user(parts[1]):
        note = " ".join(parts[2:])
        if len(note) <= 1000:
            return Command(verb, parts[1], note)
    if verb in {"rmb", "rmwl", "rmpb", "info"} and len(parts) == 2:
        if valid_user(parts[1]):
            return Command(verb, parts[1])
    return None


class Commands:
    def __init__(self, runtime):
        self.c = runtime

    async def disable(self, scope, actor):
        await self.c.settings.update(scope, actor, self.c.clock(), enabled=False)
        self.c.approval.clear_group(scope)
        for key, row in list(self.c.verification.challenges.items()):
            if row.scope == scope:
                await self.c.verification.finish(row, "cancelled")

    async def execute(self, scope, actor, cmd, superuser=False):
        if not cmd or self.c.stopping or not await self.c.allows_scope(scope):
            return
        if cmd.verb == "help":
            image, plain = await self.c.renderer.help(self.c.clock(), group_umo(scope))
            await self.c.api.for_scope(scope).send(
                scope,
                "" if image else plain,
                image=image,
                mention=None if image else actor,
            )
            return
        reply, review, rows, info = "", None, [], False
        async with self.c.lock(scope):
            settings = await self.c.settings.get(scope)
            if cmd.verb == "ans":
                if settings["enabled"] and await self.c.verification.answer(
                    scope, actor, cmd.target, cmd.context_id
                ):
                    reply = "验证通过"
            else:
                role = (
                    "owner"
                    if superuser
                    else await self.c.api.for_scope(scope).role(scope, actor)
                )
                rank = {"owner": 2, "admin": 1}.get(role, 0)
                required = (
                    2
                    if cmd.verb in {"ban", "wl", "pb", "rmb", "rmwl", "skip_ban"}
                    else 1
                )
                if cmd.verb == "rmpb":
                    required = 0
                if rank < required:
                    return
                if cmd.verb in {"on", "off"}:
                    enabled = cmd.verb == "on"
                    if settings["enabled"] == enabled:
                        return
                    if enabled:
                        await self.c.settings.update(
                            scope, actor, self.c.clock(), enabled=True
                        )
                        self.c.spawn(self.c.members.sync(scope))
                    else:
                        await self.disable(scope, actor)
                    reply = "审批与入群验证已" + ("开启。" if enabled else "关闭。")
                elif cmd.verb in {"auto", "skip_ban"}:
                    field = "auto_approve" if cmd.verb == "auto" else "skip_ban"
                    await self.c.settings.update(
                        scope, actor, self.c.clock(), **{field: not settings[field]}
                    )
                    reply = ("自动审批" if cmd.verb == "auto" else "黑名单自动拒绝") + (
                        "已关闭" if settings[field] else "已开启"
                    )
                elif cmd.verb in {"ban", "wl", "pb", "rmb", "rmwl", "rmpb"}:
                    if await self.c.db.edit_list(
                        scope, actor, cmd, superuser, self.c.clock()
                    ):
                        reply = "操作完成，数据库已更新"
                    if cmd.verb == "wl":
                        challenge = self.c.verification.challenges.get(
                            (scope_key(scope), cmd.target)
                        )
                        if challenge:
                            await self.c.verification.finish(challenge, "exempt")
                elif cmd.verb == "info":
                    info = True
                elif cmd.verb in {"y", "n"} and settings["enabled"]:
                    rows, review = self.c.approval.select(scope, cmd.target)
        if rows and not review:
            counts = {"approved": 0, "rejected": 0, "unknown": 0}
            for row in rows:
                async with self.c.lock(scope):
                    if not superuser and await self.c.api.for_scope(scope).role(
                        scope, actor
                    ) not in {
                        "owner",
                        "admin",
                    }:
                        break
                    status = await self.c.approval.act(
                        scope, row, actor, cmd.verb == "y"
                    )
                    if status:
                        counts[status] += 1
            if sum(counts.values()):
                reply = f"同意 {counts['approved']}，拒绝 {counts['rejected']}，结果待核实 {counts['unknown']}。"
        if reply:
            await self.c.api.for_scope(scope).send(scope, reply, mention=actor)
        if info:
            await self.c.cards.info(scope, cmd.target)
        if review:
            for number, row in enumerate(rows, 1):
                if not await self.c.cards.request(
                    scope, row, f"审批列表 · #{number}", review
                ):
                    return
                async with self.c.lock(scope):
                    if self.c.approval.snapshots.get(scope_key(scope)) is not review:
                        return
                    review.items[number] = row.id
            async with self.c.lock(scope):
                if (
                    self.c.approval.snapshots.get(scope_key(scope)) is review
                    and review.expires_at > self.c.clock()
                ):
                    review.active = True
                    await self.c.api.for_scope(scope).send(
                        scope,
                        "请使用 /group y [编号/all] 或 /group n [编号/all]。\n"
                        f"编号全群共用，截止 {display_time(review.expires_at)}；申请仍以各自的半小时期限为准。",
                        mention=actor,
                    )
