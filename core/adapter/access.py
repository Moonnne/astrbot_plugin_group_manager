"""AstrBot session permissions, separate from platform group roles."""

from astrbot.core.star.session_plugin_manager import SessionPluginManager

from ...utils.scope import group_umo


class HostAccess:
    def __init__(self, context):
        self.context = context

    def is_admin(self, user, umo):
        return user in {
            str(value) for value in self.context.get_config(umo).get("admins_id", [])
        }

    async def allows(self, scope):
        umo = group_umo(scope)
        config = self.context.get_config(umo)
        plugins = config.get("plugin_set", ["*"])
        if plugins != ["*"] and "astrbot_plugin_group_manager" not in plugins:
            return False
        settings = config.get("platform_settings", {})
        whitelist = {
            str(value).strip()
            for value in settings.get("id_whitelist", [])
            if str(value).strip()
        }
        if (
            settings.get("enable_id_white_list")
            and whitelist
            and not ({scope.group, umo} & whitelist)
        ):
            return False
        return await SessionPluginManager.is_plugin_enabled_for_session(
            umo, "astrbot_plugin_group_manager"
        )
