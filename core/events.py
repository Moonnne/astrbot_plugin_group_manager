"""Validate OneBot payloads delivered by the AstrBot group event handler."""

from ..models.scope import Scope
from .rules import GROUP_ID, QQ


class GroupEvents:
    def __init__(self, controller, logger):
        self.controller, self.log = controller, logger
        self.stopped = False

    @staticmethod
    def matches(raw):
        return isinstance(raw, dict) and (
            raw.get("post_type") == "request"
            and raw.get("request_type") == "group"
            or raw.get("post_type") == "notice"
            and raw.get("notice_type") in {"group_increase", "group_decrease"}
        )

    def receive(self, event):
        raw = event.message_obj.raw_message
        if self.stopped or self.controller.stopping or not self.matches(raw):
            return
        platform = event.get_platform_id()
        group, bot, user = (
            str(raw.get(key) or "") for key in ("group_id", "self_id", "user_id")
        )
        if (
            event.get_platform_name() != "aiocqhttp"
            or not (
                GROUP_ID.fullmatch(group) and QQ.fullmatch(bot) and QQ.fullmatch(user)
            )
            or group != event.get_group_id()
            or bot != event.get_self_id()
            or not self.controller.allows_group(group)
        ):
            return
        if len(str(raw.get("flag") or "")) > 4096:
            self.log.warning("event.filtered reason=oversized_flag group=%s", group)
            return
        if event.get_extra("monitor_event_handled"):
            return
        event.set_extra("monitor_event_handled", True)
        fields = (
            "post_type",
            "request_type",
            "notice_type",
            "sub_type",
            "group_id",
            "self_id",
            "user_id",
            "operator_id",
            "invited_id",
            "flag",
            "comment",
            "time",
        )
        payload = {key: raw[key] for key in fields if key in raw}
        if isinstance(payload.get("comment"), str):
            payload["comment"] = payload["comment"][:2000]
        # Workers retain only necessary wire fields, never the host event object.
        self.controller.enqueue(Scope(platform, bot, group), payload)

    def stop(self):
        self.stopped = True
