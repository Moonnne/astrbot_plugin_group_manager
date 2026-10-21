"""OneBot non-message subscriptions, isolated from AstrBot chat wake handling."""

from ..models.scope import Scope
from ..utils.plugin_log import event_summary


class GroupEvents:
    def __init__(self, context, controller, logger):
        self.context, self.controller, self.log = context, controller, logger
        self.bindings = []
        self.stopped = False

    def bind(self, platform):
        self.log.debug(
            "event.bind_candidate platform=%s adapter=%s stopped=%s",
            platform.meta().id,
            platform.meta().name,
            self.stopped,
        )
        if self.stopped or platform.meta().name != "aiocqhttp":
            return
        client = platform.get_client()
        if any(current is client for current, _, _ in self.bindings):
            self.log.debug(
                "event.bind_skip reason=already_bound platform=%s client=%s",
                platform.meta().id,
                id(client),
            )
            return
        platform_id = str(platform.meta().id)

        async def receive(event):
            self.log.info(
                "event.received platform=%s client=%s data=%s",
                platform_id,
                id(client),
                event_summary(event),
            )
            # The callback must remain short: bounded workers own accepted work.
            group, bot = (
                str(event.get("group_id") or ""),
                str(event.get("self_id") or ""),
            )
            if (
                group.isdigit()
                and bot.isdigit()
                and self.controller.allows_group(group)
            ):
                if len(str(event.get("flag") or "")) > 4096:
                    self.log.warning(
                        "event.filtered reason=oversized_flag platform=%s group=%s",
                        platform_id,
                        group,
                    )
                    return
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
                raw = {key: event[key] for key in fields if key in event}
                if isinstance(raw.get("comment"), str):
                    raw["comment"] = raw["comment"][:2000]
                self.controller.enqueue(Scope(platform_id, bot, group), raw)
            else:
                self.log.info(
                    "event.filtered platform=%s group=%s valid_ids=%s group_enabled=%s",
                    platform_id,
                    group,
                    group.isdigit() and bot.isdigit(),
                    self.controller.allows_group(group),
                )

        # CQHttp subscribe/unsubscribe are public; adapter handlers remain installed.
        for name in ("request.group", "notice.group_increase", "notice.group_decrease"):
            client.subscribe(name, receive)
            self.bindings.append((client, name, receive))
            self.log.info(
                "event.subscribed platform=%s client=%s event=%s",
                platform_id,
                id(client),
                name,
            )
        self.log.info("OneBot group observers attached: platform=%s", platform_id)

    def bind_all(self):
        platforms = self.context.platform_manager.platform_insts
        self.log.info(
            "event.inventory platforms=%s bound_callbacks=%s",
            len(platforms),
            len(self.bindings),
        )
        clients = [p.get_client() for p in platforms if p.meta().name == "aiocqhttp"]
        for client, name, callback in list(self.bindings):
            if not any(client is current for current in clients):
                client.unsubscribe(name, callback)
                self.log.info(
                    "event.unsubscribed reason=adapter_removed client=%s event=%s",
                    id(client),
                    name,
                )
                self.bindings.remove((client, name, callback))
        for platform in platforms:
            self.bind(platform)

    def stop(self):
        self.stopped = True
        for client, name, callback in self.bindings:
            client.unsubscribe(name, callback)
            self.log.info(
                "event.unsubscribed reason=unload client=%s event=%s", id(client), name
            )
        self.bindings.clear()
