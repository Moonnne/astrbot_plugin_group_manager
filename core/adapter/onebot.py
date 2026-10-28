"""OneBot actions through AstrBot's platform adapter and account routing."""

import asyncio
import re
import time
from collections import OrderedDict

from ...models.group_event import GroupEvent
from ...models.scope import Scope
from ...utils.errors import error_detail
from ...utils.scope import scope_key


class OneBot:
    legacy_group_ids = True
    user_pattern = re.compile(r"[1-9][0-9]{4,14}", re.ASCII)

    @classmethod
    def valid_user(cls, value):
        return bool(cls.user_pattern.fullmatch(str(value)))

    @staticmethod
    def scope(event):
        return Scope(event.get_platform_id(), event.get_self_id(), event.get_group_id())

    @staticmethod
    def is_message(event):
        raw = event.message_obj.raw_message
        return isinstance(raw, dict) and raw.get("post_type") == "message"

    @staticmethod
    def is_notice(event):
        raw = event.message_obj.raw_message
        return isinstance(raw, dict) and (
            raw.get("post_type") == "request"
            and raw.get("request_type") == "group"
            or raw.get("post_type") == "notice"
            and raw.get("notice_type") in {"group_increase", "group_decrease"}
        )

    def normalize(self, event):
        if not self.is_notice(event):
            return None
        raw, scope = event.message_obj.raw_message, self.scope(event)
        if len(str(raw.get("flag") or "")) > 4096:
            return None
        if (
            not self.valid_user(scope.group)
            or not self.valid_user(scope.bot)
            or str(raw.get("group_id")) != scope.group
            or str(raw.get("self_id")) != scope.bot
        ):
            return None
        subtype = str(raw.get("sub_type") or "")
        inviter = str(raw.get("operator_id") or "")
        member = str(raw.get("user_id") or "")
        if raw.get("post_type") == "request":
            if subtype == "invite":
                inviter, member = member, str(raw.get("invited_id") or "")
            if subtype not in {"add", "invite"} or not self.valid_user(member):
                return None
            handle = raw.get("flag")
            if not isinstance(handle, str) or not 0 < len(handle) <= 4096:
                return None
            return GroupEvent(
                scope,
                "request",
                member,
                float(raw.get("time") or event.message_obj.timestamp),
                request_id=handle,
                subtype=subtype,
                reason=str(raw.get("comment") or "")[:2000],
                inviter=inviter if subtype == "invite" else "",
                method=f"QQ {inviter} 邀请" if subtype == "invite" else "申请入群",
            )
        if not self.valid_user(member):
            return None
        return GroupEvent(
            scope,
            "join" if raw["notice_type"] == "group_increase" else "leave",
            member,
            float(raw.get("time") or event.message_obj.timestamp),
            subtype=subtype,
            inviter=inviter,
            method=f"QQ {inviter} 邀请"
            if subtype == "invite" and inviter
            else "申请入群",
        )

    async def iter_members(self, scope):
        for member in await self.call(
            scope, "get_group_member_list", group_id=int(scope.group)
        ):
            yield member

    async def groups(self, platform):
        result = await platform.get_client().call_action("get_group_list")
        if not isinstance(result, list):
            raise ValueError("Invalid OneBot group list")
        return {
            str(row["group_id"]): str(row.get("group_name") or "").strip()
            for row in result
            if self.valid_user(row.get("group_id", ""))
        }

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

    async def send(self, scope, text="", image=None, mention=None, *, card=None):
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
