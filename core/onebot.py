"""OneBot access, capability fallbacks, and verified message routing."""

import asyncio
import time
from collections import OrderedDict

from ..utils.plugin_log import error_detail, flag_ref
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
        """Route through CQHttp's explicit account selection.

        Args:
            scope: Persisted platform, bot account, and group identity.
            action: OneBot action name.
            **params: Validated action parameters.

        Returns:
            The action data returned by aiocqhttp.

        Raises:
            RuntimeError: The requested OneBot platform is unavailable.
        """
        started = time.monotonic()
        if self.log:
            summary = {
                key: params[key]
                for key in (
                    "group_id",
                    "user_id",
                    "sub_type",
                    "approve",
                    "reject_add_request",
                )
                if key in params
            }
            if "flag" in params:
                summary["flag_ref"] = flag_ref(params["flag"])
            if isinstance(params.get("message"), list):
                summary["segments"] = [part.get("type") for part in params["message"]]
            self.log.debug(
                "api.begin scope=%s action=%s params=%s",
                scope_key(scope),
                action,
                summary,
            )
        platform = self.context.get_platform_inst(scope.platform)
        if platform is None or platform.meta().name != "aiocqhttp":
            if self.log:
                self.log.error(
                    "api.route_failed scope=%s action=%s reason=platform_unavailable",
                    scope_key(scope),
                    action,
                )
            raise RuntimeError("OneBot platform is unavailable")
        client = platform.get_client()
        try:
            async with asyncio.timeout(self.timeout):
                result = await client.call_action(
                    action, self_id=int(scope.bot), **params
                )
            if self.log:
                shape = (
                    list(result)[:20]
                    if isinstance(result, dict)
                    else len(result)
                    if isinstance(result, list)
                    else type(result).__name__
                )
                self.log.debug(
                    "api.complete action=%s scope=%s result_shape=%s duration_ms=%.0f",
                    action,
                    scope_key(scope),
                    shape,
                    (time.monotonic() - started) * 1000,
                )
            return result
        except asyncio.CancelledError:
            if self.log:
                self.log.warning(
                    "api.cancelled action=%s scope=%s", action, scope_key(scope)
                )
            raise
        except Exception as exc:
            if self.log:
                self.log.error(
                    "api.failed action=%s scope=%s duration_ms=%.0f error=%s",
                    action,
                    scope_key(scope),
                    (time.monotonic() - started) * 1000,
                    error_detail(exc),
                )
            raise

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
        if mention:
            segments.append({"type": "at", "data": {"qq": str(mention)}})
        if text:
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
            if self.log:
                self.log.debug("profile.cache_hit scope=%s qq=%s", scope_key(scope), qq)
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
        if self.log:
            self.log.debug(
                "profile.complete scope=%s qq=%s nickname_available=%s level_available=%s cover_available=%s cache_size=%s",
                scope_key(scope),
                qq,
                profile["nickname"] != qq,
                profile["level"] != "未知",
                bool(profile["cover"]),
                len(self.profiles),
            )
        return profile
