"""QQ Official group management through AstrBot's managed botpy client."""

import asyncio
import html
import re
import time
from collections import OrderedDict
from datetime import datetime
from urllib.parse import quote, urlsplit

from ...models.group_event import GroupEvent
from ...models.scope import Scope
from ...utils.errors import error_detail

OPEN_ID = re.compile(r"[A-Za-z0-9_-]{16,128}", re.ASCII)
EVENTS = {
    "GROUP_JOIN_REQUEST",
    "GROUP_MEMBER_ADD",
    "GROUP_MEMBER_REMOVE",
    "GROUP_ADD_ROBOT",
    "GROUP_DEL_ROBOT",
}


def timestamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("QQ Official timestamp has no timezone")
        return parsed.timestamp()
    raise ValueError("Invalid QQ Official timestamp")


class QQOfficial:
    legacy_group_ids = False

    async def groups(self, platform):
        # QQ Official exposes group info, but no enumeration of all joined groups.
        # Discovery therefore uses AstrBot's known group sessions.
        return {}

    def __init__(self, context, timeout=15, *, logger=None):
        self.context, self.timeout, self.log = context, timeout, logger
        self.profiles = OrderedDict()

    @staticmethod
    def valid_user(value):
        return bool(OPEN_ID.fullmatch(str(value)))

    def platform(self, scope):
        platform = self.context.get_platform_inst(scope.platform)
        if (
            platform is None
            or platform.meta().name not in {"qq_official", "qq_official_webhook"}
            or scope.bot != f"qqofficial:{platform.appid}"
        ):
            raise RuntimeError("QQ Official platform or AppID is unavailable")
        return platform

    def scope(self, event):
        platform = self.context.get_platform_inst(event.get_platform_id())
        return Scope(
            event.get_platform_id(),
            f"qqofficial:{platform.appid}",
            event.get_group_id(),
        )

    @staticmethod
    def is_message(event):
        raw = event.message_obj.raw_message
        return bool(
            getattr(raw, "group_openid", None)
            and str(raw.group_openid) == event.get_group_id()
            and getattr(getattr(raw, "author", None), "member_openid", None)
        )

    @staticmethod
    def is_notice(event):
        raw = event.message_obj.raw_message
        return (
            isinstance(raw, dict)
            and raw.get("t") in EVENTS
            and isinstance(raw.get("d"), dict)
        )

    def normalize(self, event):
        if not self.is_notice(event):
            return None
        raw, scope = event.message_obj.raw_message, self.scope(event)
        data, kind = raw["d"], raw["t"]
        if (
            not OPEN_ID.fullmatch(scope.group)
            or data.get("group_openid") != scope.group
        ):
            return None
        member = str(data.get("member_openid") or "")
        if kind in {"GROUP_ADD_ROBOT", "GROUP_DEL_ROBOT"}:
            member = scope.bot
        elif not self.valid_user(member):
            return None
        happened = timestamp(
            data.get("apply_at") or data.get("timestamp") or event.message_obj.timestamp
        )
        nickname = str(data.get("username") or "")[:160]
        if nickname:
            self._remember(scope, member, {"nickname": nickname})
        if kind == "GROUP_JOIN_REQUEST":
            handle = data.get("join_request_id")
            if not isinstance(handle, str) or not 0 < len(handle) <= 4096:
                return None
            verify = data.get("verify_info") or {}
            reason = str(verify.get("verify_message") or "")
            if verify.get("method") == "admin_review_qa":
                reason = "\n".join(
                    f"{item.get('question', '')}: {item.get('answer', '')}"
                    for item in verify.get("review_qa_list", [])[:20]
                )
            inviter = str(data.get("invited_by") or "")
            return GroupEvent(
                scope,
                "request",
                member,
                happened,
                request_id=handle,
                reason=reason[:2000],
                method=f"OpenID {inviter} 邀请" if inviter else "申请入群",
                inviter=inviter,
                nickname=nickname,
                approved=bool(data.get("auto_approved")),
            )
        return GroupEvent(
            scope,
            "join" if kind in {"GROUP_MEMBER_ADD", "GROUP_ADD_ROBOT"} else "leave",
            member,
            happened,
        )

    async def request(self, scope, method, path, **kwargs):
        """Use the public HTTP transport owned and authenticated by AstrBot's SDK client."""
        from botpy.http import Route

        if not OPEN_ID.fullmatch(scope.group):
            raise ValueError("Invalid QQ Official group OpenID")
        client = self.platform(scope).get_client()
        async with asyncio.timeout(self.timeout):
            result = await client.http.request(
                Route(method, path, group_openid=scope.group), **kwargs
            )
        if not isinstance(result, dict) or result.get("code", 0) not in (0, None):
            raise RuntimeError("QQ Official returned an unsuccessful response")
        return result

    async def member(self, scope, member):
        if not self.valid_user(member):
            raise ValueError("Invalid member OpenID")
        data = await self.request(
            scope, "GET", f"/v2/groups/{{group_openid}}/members/{member}"
        )
        if data.get("member_openid") != member or data.get("member_role") not in {
            "owner",
            "admin",
            "member",
        }:
            raise ValueError("Invalid QQ Official member response")
        self._remember(
            scope, member, {"nickname": str(data.get("username") or member)[:160]}
        )
        return self._member(data)

    @staticmethod
    def _member(data):
        return {
            "user_id": str(data["member_openid"]),
            "nickname": str(data.get("username") or data["member_openid"]),
            "role": data.get("member_role", "member"),
            "join_time": timestamp(data["joined_at"])
            if data.get("joined_at")
            else None,
        }

    async def role(self, scope, member):
        if member == scope.bot:
            data = await self.request(
                scope, "GET", "/v2/groups/{group_openid}/bot_state"
            )
            if data.get("member_role") not in {"owner", "admin", "member"}:
                raise ValueError("Invalid QQ Official bot state")
            return data["member_role"]
        return (await self.member(scope, member))["role"]

    async def iter_members(self, scope):
        cursor, visited = "", set()
        for _ in range(100):
            data = await self.request(
                scope,
                "GET",
                "/v2/groups/{group_openid}/members",
                params={"cursor": cursor},
            )
            rows = data.get("members")
            if not isinstance(rows, list) or len(rows) > 30:
                raise ValueError("Invalid QQ Official member list")
            for row in rows:
                if not self.valid_user(row.get("member_openid", "")) or row.get(
                    "member_role"
                ) not in {"owner", "admin", "member"}:
                    raise ValueError("Invalid member in QQ Official list")
                yield self._member(row)
            cursor = str(data.get("next_cursor") or "")
            if not cursor:
                return
            if cursor in visited:
                raise ValueError("Repeated QQ Official member cursor")
            visited.add(cursor)
        raise RuntimeError("QQ Official member pagination exceeded its bound")

    async def approve(self, scope, request, approve):
        member = request["qq"]
        if not self.valid_user(member):
            raise ValueError("Invalid applicant OpenID")
        payload = {
            "op": "approve" if approve else "decline",
            "join_request_id": request["flag"],
        }
        if not approve:
            payload.update(
                reject_reason="群管理审核未通过", add_to_member_blacklist=False
            )
        return await self.request(
            scope,
            "POST",
            f"/v2/groups/{{group_openid}}/approval_join_request/{member}",
            json=payload,
        )

    async def kick(self, scope, member):
        if not self.valid_user(member):
            raise ValueError("Invalid member OpenID")
        data = await self.request(
            scope,
            "POST",
            "/v2/groups/{group_openid}/batch_remove_members",
            json={"member_openids": [member], "add_to_member_blacklist": False},
        )
        if data.get("remove_members_result") != "success":
            raise RuntimeError("QQ Official member removal outcome is uncertain")

    async def send(self, scope, text="", image=None, mention=None, *, card=None):
        if card and card.buttons:
            return await self._send_card(scope, card, image, mention)
        from astrbot.api.event import MessageChain
        from astrbot.api.message_components import At, Image, Plain
        from astrbot.api.platform import MessageType
        from astrbot.core.platform.message_session import MessageSession

        chain = []
        if text:
            if mention and self.valid_user(mention):
                chain.append(At(qq=mention))
                text = " " + text.lstrip()
            if image:
                text = text.rstrip("\r\n") + "\n"
            chain.append(Plain(text))
        if image:
            chain.append(Image.fromURL(image))
        async with asyncio.timeout(self.timeout):
            await self.platform(scope).send_by_session(
                MessageSession(scope.platform, MessageType.GROUP_MESSAGE, scope.group),
                MessageChain(chain=chain),
            )

    async def _send_card(self, scope, card, image, mention):
        # Command buttons enter AstrBot's ordinary message and permission pipeline.
        # UI permissions are supplementary; the command handler remains authoritative.
        content = html.escape(card.text, quote=False).replace("\r", "")
        content = re.sub(r"([\\`*_{}\[\]()#+.!|~-])", r"\\\1", content)
        if content and mention and self.valid_user(mention):
            content = f'<qqbot-at-user id="{mention}" /> ' + content.lstrip()
        if image:
            parsed = urlsplit(image)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or any(char in image for char in "\r\n")
            ):
                raise ValueError("Card image must have a public HTTP image URL")
            address = quote(image, safe=":/?&=%#+@,;~!$'*")
            content = content.rstrip() + f"\n![MONITOR]({address})"
        buttons = []
        for index, button in enumerate(card.buttons):
            permission = {"type": 2}
            if button.member:
                if not self.valid_user(button.member):
                    raise ValueError("Invalid command button member")
                permission = {"type": 0, "specify_user_ids": [button.member]}
            buttons.append(
                {
                    "id": str(index + 1),
                    "render_data": {
                        "label": button.label,
                        "visited_label": button.label,
                        "style": 1,
                    },
                    "action": {
                        "type": 2,
                        "permission": permission,
                        "data": button.command,
                        "enter": True,
                        "reply": False,
                        "unsupport_tips": "请手动发送卡片中的对应指令",
                    },
                }
            )
        result = await self.request(
            scope,
            "POST",
            "/v2/groups/{group_openid}/messages",
            json={
                "msg_type": 2,
                "markdown": {"content": content},
                "keyboard": {"content": {"rows": [{"buttons": buttons}]}},
            },
        )
        if not result.get("id"):
            raise RuntimeError("QQ Official card send outcome is uncertain")
        return result

    def _remember(self, scope, member, values):
        key = (scope.bot, scope.group, member)
        self.profiles[key] = (time.monotonic() + 120, dict(values))
        self.profiles.move_to_end(key)
        while len(self.profiles) > 256:
            self.profiles.popitem(last=False)

    async def profile(self, scope, member):
        self.prune()
        profile = {
            "nickname": member,
            "level": "未知",
            "area": "未公开",
            "avatar": "",
            "cover": "",
            "sex": "未公开",
            "age": "未公开",
            "signature": "",
            "id_label": "OpenID",
        }
        cached = self.profiles.get((scope.bot, scope.group, member))
        if not cached:
            try:
                await self.member(scope, member)
                cached = self.profiles.get((scope.bot, scope.group, member))
            except Exception as exc:
                if self.log:
                    self.log.debug(
                        "QQ Official profile unavailable: %s", error_detail(exc)
                    )
        if cached:
            profile.update(cached[1])
        return profile

    def prune(self):
        now = time.monotonic()
        for key, (expires, _) in list(self.profiles.items()):
            if expires <= now:
                self.profiles.pop(key)

    def clear(self):
        self.profiles.clear()
