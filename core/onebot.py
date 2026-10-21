"""OneBot access, capability fallbacks, and verified message routing."""

import asyncio
from urllib.parse import urlparse


class OneBot:
    def __init__(self, context, timeout=15):
        self.context = context
        self.timeout = timeout

    async def call(self, scope, action, **params):
        """Resolve and verify the original account before each protocol action.

        Args:
            scope: Persisted platform, bot account, and group identity.
            action: OneBot action name.
            **params: Validated action parameters.

        Returns:
            The action data returned by aiocqhttp.

        Raises:
            RuntimeError: The platform or logged-in account does not match.
        """
        platform = self.context.get_platform_inst(scope.platform)
        if platform is None or platform.meta().name != "aiocqhttp":
            raise RuntimeError("OneBot platform is unavailable")
        client = platform.get_client()
        # A reverse-WS reconnect can reuse the same client with another account.
        async with asyncio.timeout(self.timeout):
            identity = await client.call_action("get_login_info")
        if str(identity.get("user_id")) != scope.bot:
            raise RuntimeError("OneBot identity mismatch")
        async with asyncio.timeout(self.timeout):
            return await client.call_action(action, **params)

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
        profile = {
            "nickname": qq,
            "level": "Unknown",
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
            profile["nickname"] = str(info.get("nickname") or qq)
            profile["signature"] = str(
                info.get("long_nick") or info.get("longNick") or ""
            )
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
            )
            profile["sex"] = {"male": "男", "female": "女"}.get(
                info.get("sex"), "未公开"
            )
            age = info.get("age")
            if isinstance(age, int) and 0 < age < 130:
                profile["age"] = str(age)
        except Exception:
            pass
        try:
            dress = await self.call(scope, "_get_friend_dress", user_id=int(qq))
            profile["avatar"] = safe_image(dress.get("avatar_url")) or profile["avatar"]
            for item in dress.get("items", []):
                if str(item.get("app_id")) == "15":
                    profile["cover"] = safe_image(item.get("preview_url"))
                    if profile["cover"]:
                        break
        except Exception:
            pass
        return profile


def safe_image(value):
    """Allow only known public QQ image hosts in remote render templates."""
    if not isinstance(value, str):
        return ""
    try:
        url = urlparse(value)
        host = (url.hostname or "").lower()
        allowed = ("qq.com", "qpic.cn", "qlogo.cn", "gtimg.cn", "gtimg.com")
        if url.scheme in {"http", "https"} and not url.username and not url.password:
            if url.port in {None, 80, 443} and any(
                host == h or host.endswith("." + h) for h in allowed
            ):
                return value
    except ValueError:
        pass
    return ""
