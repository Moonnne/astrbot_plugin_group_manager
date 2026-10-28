"""Render and deliver request cards and scoped member histories."""

from dataclasses import asdict

from ..models.action_card import ActionCard
from ..models.button import Button
from ..utils.scope import group_umo, scope_key


class Cards:
    def __init__(self, runtime):
        self.c = runtime

    async def request(self, scope, row, title="入群申请", review=None):
        profile = await self.c.api.for_scope(scope).profile(scope, row.qq)
        flags = await self.c.db.flags(scope, row.qq)
        member = await self.c.db.member(scope, row.qq)
        image, plain = await self.c.renderer.card(
            profile,
            row.qq,
            flags,
            member,
            [],
            asdict(row),
            title,
            self.c.clock(),
            group_umo(scope),
        )
        async with self.c.lock(scope):
            if (
                self.c.approval.requests.get(row.id) is not row
                or row.expires_at <= self.c.clock()
                or not (await self.c.settings.get(scope))["enabled"]
                or not await self.c.allows_scope(scope)
            ):
                return False
            if review and self.c.approval.snapshots.get(scope_key(scope)) is not review:
                return False
            await self.c.api.for_scope(scope).send(
                scope,
                "" if image else plain,
                image=image,
                mention=None if image else row.qq,
                card=ActionCard(
                    f"{title}：请选择同意或拒绝，申请接管期限为 30 分钟。"
                    if image
                    else plain,
                    (
                        Button("同意", f"/group y r_{row.id}"),
                        Button("拒绝", f"/group n r_{row.id}"),
                    ),
                )
                if row.status == "pending"
                else None,
            )
        return True

    async def info(self, scope, qq):
        profile = await self.c.api.for_scope(scope).profile(scope, qq)
        flags = await self.c.db.flags(scope, qq)
        member = await self.c.db.member(scope, qq)
        request = self.c.approval.latest(scope, qq)
        offset = 0
        while True:
            history = (
                await self.c.db.history(scope, qq, 5, offset) if flags["public"] else []
            )
            image, _ = await self.c.renderer.card(
                profile,
                qq,
                flags,
                member,
                history,
                asdict(request) if request else None,
                "成员记录",
                self.c.clock(),
                group_umo(scope),
            )
            current = await self.c.db.flags(scope, qq)
            if flags["public"] and not current["public"]:
                return
            if not image:
                self.c.log.warning(
                    "Member info render failed: scope=%s", scope_key(scope)
                )
                return
            if not await self.c.allows_scope(scope):
                return
            await self.c.api.for_scope(scope).send(scope, image=image)
            if len(history) < 5:
                break
            offset += 5
