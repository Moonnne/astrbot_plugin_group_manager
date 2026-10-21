"""Populate the host's searchable group selector without changing its frontend."""

import asyncio

from .rules import GROUP_ID


class GroupSelector:
    def __init__(self, context, config, logger):
        self.context, self.config, self.log = context, config, logger

    async def refresh(self):
        """Update option IDs and labels, preserving unavailable selected groups.

        Group names come from connected OneBot clients, with known host session
        names as a fallback. Only schema metadata changes; selection values keep
        their existing group-ID semantics.
        """
        schema = getattr(self.config, "schema", None)
        if schema is None:
            return
        platforms = {
            str(platform.meta().id): platform
            for platform in self.context.platform_manager.platform_insts
            if platform.meta().name == "aiocqhttp"
        }
        choices = dict.fromkeys(self.config["enabled_groups"], "未获取群名")
        try:
            rows = await self.context.get_db().get_umo_aliases()
            for row in rows:
                parts = row.umo.split(":", 2)
                if (
                    len(parts) != 3
                    or parts[0] not in platforms
                    or parts[1] != "GroupMessage"
                ):
                    continue
                group = parts[2].removeprefix(f"{row.creator_sender_id}_")
                if GROUP_ID.fullmatch(group):
                    name = row.user_alias or row.auto_name
                    if name:
                        choices[group] = str(name)
        except Exception as exc:
            self.log.debug("Known group names unavailable: %s", type(exc).__name__)
        results = await asyncio.gather(
            *(self._group_names(platform) for platform in platforms.values())
        )
        for names in results:
            for group, name in names.items():
                if name:
                    choices[group] = name
                else:
                    choices.setdefault(group, "未获取群名")
        ordered = sorted(choices, key=lambda group: (choices[group].casefold(), group))
        schema["enabled_groups"].update(
            options=ordered,
            labels=[f"{choices[group]}（{group}）" for group in ordered],
        )

    async def _group_names(self, platform):
        """Bound each independent adapter lookup so one offline bot cannot block discovery."""
        try:
            async with asyncio.timeout(3):
                groups = await platform.get_client().call_action("get_group_list")
            if not isinstance(groups, list):
                raise ValueError("Invalid group list response")
            names = {}
            for group in groups:
                group_id = str(group.get("group_id", ""))
                if GROUP_ID.fullmatch(group_id):
                    names[group_id] = str(group.get("group_name") or "").strip()
            return names
        except Exception as exc:
            self.log.debug(
                "Group discovery unavailable: platform=%s error=%s",
                platform.meta().id,
                type(exc).__name__,
            )
            return {}
