"""OneBot actions through AstrBot's platform adapter and account routing."""

import asyncio
import time
from collections import OrderedDict

from ..utils.errors import error_detail
from ..utils.scope import scope_key


class OneBot:
    def __init__(self, context, timeout=15, *, logger=None):
        self.context = context
        self.timeout = timeout
        self.profiles = OrderedDict()
        self.log = logger

    def clear(self):
        self.profiles.clear()

    def prune(self):
        now = time.monotonic()
        for key, (expires, _) in list(self.profiles.items()):
            if expires <= now:
                self.profiles.pop(key)

    async def call(self, scope, action, **params):
        """Call the AstrBot adapter using the original platform and bot account."""
        platform = self.context.get_platform_inst(scope.platform)
        if platform is None or platform.meta().name != "aiocqhttp":
            raise RuntimeError("OneBot platform is unavailable")
        async with asyncio.timeout(self.timeout):
            return await platform.get_client().call_action(
                action, self_id=int(scope.bot), **params
            )

    async def member(self, scope, qq):
        result = await self.call(
            scope,
            "get_group_member_info",
            group_id=int(scope.group),
            user_id=int(qq),
            no_cache=True,
        )
        if not isinstance(result, dict) or str(result.get("user_id")) != qq:
            raise RuntimeError("Invalid member response")
        return result

    async def role(self, scope, qq):
        return (await self.member(scope, qq)).get("role", "member")

    async def approve(self, scope, request, approve):
        return await self.call(
            scope,
            "set_group_add_request",
            flag=request["flag"],
            sub_type=request["subtype"],
            approve=approve,
            reason="" if approve else "群管理审核未通过",
        )

    async def kick(self, scope, qq):
        return await self.call(
            scope,
            "set_group_kick",
            group_id=int(scope.group),
            user_id=int(qq),
            reject_add_request=False,
        )

    async def send(self, scope, text="", image=None, mention=None):
        segments = []
        if text:
            if mention:
                segments.append({"type": "at", "data": {"qq": str(mention)}})
                text = " " + text.lstrip()
            if image:
                text = text.rstrip("\r\n") + "\n"
            segments.append({"type": "text", "data": {"text": text}})
        if image:
            segments.append({"type": "image", "data": {"file": image}})
        return await self.call(
            scope, "send_group_msg", group_id=int(scope.group), message=segments
        )

    async def profile(self, scope, qq):
        now = time.monotonic()
        self.prune()
        key = (scope.platform, scope.bot, qq)
        if key in self.profiles:
            return dict(self.profiles[key][1])
        profile = {
            "nickname": qq,
            "level": "未知",
            "area": "未公开",
            "avatar": f"https://q1.qlogo.cn/g?b=qq&nk={qq}&s=640",
            "cover": "",
            "sex": "未公开",
            "age": "未公开",
            "signature": "",
        }
        try:
            info = await self.call(
                scope, "get_stranger_info", user_id=int(qq), no_cache=True
            )
            profile["nickname"] = str(info.get("nickname") or qq)[:160]
            profile["signature"] = str(
                info.get("long_nick") or info.get("longNick") or ""
            )[:300]
            level = next(
                (
                    info[k]
                    for k in ("qq_level", "qqLevel", "level")
                    if isinstance(info.get(k), (int, str)) and str(info[k]).isdigit()
                ),
                None,
            )
            if level is not None:
                profile["level"] = str(level)
            profile["area"] = (
                " ".join(
                    str(info[k]) for k in ("country", "province", "city") if info.get(k)
                )
                or "未公开"
            )[:160]
            profile["sex"] = {"male": "男", "female": "女"}.get(
                info.get("sex"), "未公开"
            )
            age = info.get("age")
            if isinstance(age, int) and 0 < age < 130:
                profile["age"] = str(age)
        except Exception as exc:
            if self.log:
                self.log.warning(
                    "profile.lookup_failed scope=%s qq=%s error=%s",
                    scope_key(scope),
                    qq,
                    error_detail(exc),
                )
        # Dress preview_url can contain an entire card. It is not a profile cover.
        # Verified SnowLuma 2.0.3 exposes no dedicated cover image; use the theme fallback.
        self.profiles[key] = (now + 120, dict(profile))
        while len(self.profiles) > 256:
            self.profiles.popitem(last=False)
        return profile
