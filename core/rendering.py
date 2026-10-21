"""Card view construction and deterministic LaTeX-subset rendering."""

import asyncio
import base64
import html
import re
from pathlib import Path

from ..models.domain import Scope, display_time, light_theme

STATUS = {
    "pending": "待审批",
    "approved": "已同意",
    "rejected": "已拒绝",
    "unknown": "结果待核实",
    "expired": "接管超时",
    "joined": "已加入",
    "abandoned": "停止接管",
    "processing": "正在处理",
}

HELP_COMMANDS = (
    ("/g · /g help", "所有成员", "显示指令清单"),
    ("/g on · /g off", "管理员", "开关审批与验证"),
    ("/g y · /g n", "管理员", "同意 / 拒绝，存在多条申请时进入消歧页"),
    (
        "/g y [编号/all] · /g n [编号/all]",
        "管理员",
        "处理最新消歧列表，all 仅包含已展示申请，使用 /g y 或 /g n 刷新列表",
    ),
    ("/g auto", "管理员", "切换到普通申请自动批准，默认关闭"),
    ("/g auto skip_ban", "群主", "切换黑名单自动拒绝，非自动模式可用"),
    ("/g ban [QQ号] [理由]", "群主", "添加本群黑名单"),
    ("/g rmb [QQ号]", "群主", "撤销本群黑名单"),
    ("/g wl [QQ号] [备注]", "群主", "添加本群白名单，自动批准且免验证"),
    ("/g rmwl [QQ号]", "群主", "撤销本群白名单"),
    ("/g pb [QQ号] [理由]", "群主", "标记为公共黑名单"),
    ("/g rmpb [QQ号]", "原操作人", "撤销本人公共黑名单标记"),
    ("/g info [QQ号]", "管理员", "查询本群黑名单和公共黑名单"),
    ("/ans [整数答案]", "待验证成员", "回答本人入群题；成功投递后限时 10 分钟"),
)


class Renderer:
    def __init__(self, render, resource: Path, timeout=25):
        self.render = render
        self.timeout = timeout
        self.card_template = (resource / "card.html").read_text(encoding="utf-8")
        self.formula_template = (resource / "formula.html").read_text(encoding="utf-8")
        self.help_template = (resource / "help.html").read_text(encoding="utf-8")
        self.help_background = "data:image/png;base64," + base64.b64encode(
            (resource.parent / "background/default.png").read_bytes()
        ).decode("ascii")
        self.limit = asyncio.Semaphore(2)

    async def help(self, now, umo):
        """Render public command guidance using the host's session-aware service."""
        data = {
            "panel": "help",
            "commands": HELP_COMMANDS,
            "background": self.help_background,
            "light": light_theme(now),
            "time": "Create Time: " + display_time(now),
        }
        plain = "\n".join(
            [
                "MONITOR · 指令清单",
                *(
                    f"{command} [{role}]\n{description}"
                    for command, role, description in HELP_COMMANDS
                ),
            ]
        )
        try:
            image = await self._render(self.help_template, data, umo, 760, True)
        except Exception:
            image = None
        return image, plain

    async def card(self, profile, qq, flags, member, history, request, title, now, umo):
        """Build the card and its equivalent plain-text fallback.

        Args:
            profile: Normalized public QQ profile.
            qq: Subject account.
            flags: Effective local and public list entries.
            member: Current group's observed member record, if available.
            history: Authorized membership history for this page.
            request: Associated join request, if available.
            title: Page heading, including review numbering when applicable.
            now: Current UTC epoch used to select the Beijing theme.
            umo: Original session for the host render endpoint.

        Returns:
            An optional image URL and a plain-text fallback.
        """
        activity = {
            "method": "",
            "reason": "",
            "status": "",
            "operation_time": "",
            "actor": "",
            "stats": [],
        }
        if request:
            # Search/QR attribution is not exposed by the verified protocol versions.
            if request["subtype"] == "invite":
                activity["method"] = (
                    f"QQ {request['inviter']} 邀请"
                    if request.get("inviter")
                    else "邀请入群"
                )
            else:
                activity["method"] = "申请入群"
            activity["reason"] = request["reason"]
            activity["status"] = STATUS.get(request["status"], request["status"])
            if request["status"] in {"approved", "rejected", "joined"} and request.get(
                "decided_at"
            ):
                activity["operation_time"] = display_time(request["decided_at"])
                activity["actor"] = str(request.get("actor") or "")
        if member:
            activity["stats"] = [
                (label, display_time(member[key]))
                for label, key in (
                    ("最早入群", "first_join"),
                    ("最近入群", "last_join"),
                    ("最近退群", "last_leave"),
                )
                if member.get(key)
            ]
            if member.get("leave_count"):
                activity["stats"].append(("退群次数", str(member["leave_count"])))
        if not (request or activity["stats"]):
            activity = None
        list_cards = []
        for rows, public in (
            (flags["entries"], False),
            (flags["public_entries"], True),
        ):
            for row in rows:
                kind = "public" if public else row["kind"]
                list_cards.append(
                    {
                        "kind": kind,
                        "title": {
                            "white": "白名单",
                            "black": "黑名单",
                            "public": "公共黑名单",
                        }[kind],
                        "actor": str(row["actor"]),
                        "time": display_time(row["created_at"]),
                        "reason_label": "备注" if kind == "white" else "理由",
                        "note": row["note"],
                    }
                )
        history_notes = []
        history_cards = []
        for row in history:
            scope = Scope.parse(row["scope"])
            history_cards.append(
                {
                    "group": scope.group,
                    "method": row["method"],
                    "reason": row["reason"],
                    "joined": display_time(row["joined_at"])
                    if row["joined_at"]
                    else "",
                    "left": display_time(row["left_at"]) if row["left_at"] else "",
                    "verification": row["verification"],
                }
            )
            history_notes.append(
                f"群 {scope.group} · {display_time(row['joined_at'])} 入群"
                f" · {display_time(row['left_at'])} 退群 · {row['method']}"
                f" · {row['reason'] or '理由未知'} · 验证 {row['verification']}"
            )
        # Plain text is also the deterministic fallback if the renderer fails.
        signature = profile.get("signature") or "暂无个性签名"
        activity_text = []
        if activity:
            activity_text.extend(
                f"{label}：{activity[key]}"
                for label, key in (
                    ("加入方式", "method"),
                    ("加入理由", "reason"),
                    ("审核状态", "status"),
                    ("操作人", "actor"),
                    ("操作时间", "operation_time"),
                )
                if activity[key]
            )
            activity_text.extend(
                f"{label}：{value}" for label, value in activity["stats"]
            )
        plain = "\n".join(
            [
                title,
                f"{profile['nickname']} ({qq})",
                f"QQ 等级：{profile['level']}",
                f"性别：{profile['sex']} · 年龄：{profile['age']} · 地区：{profile['area']}",
                f"个性签名：{signature}",
                *activity_text,
                *history_notes,
                *(
                    f"{card['title']} · 操作人 {card['actor']} · {card['time']}\n{card['reason_label']}：{card['note']}"
                    for card in list_cards
                ),
                "Create Time: " + display_time(now),
            ]
        )
        data = {
            "profile": profile,
            "qq": qq,
            "signature": signature,
            "activity": activity,
            "list_cards": list_cards,
            "history_cards": history_cards,
            "light": light_theme(now),
            "time": "Create Time: " + display_time(now),
        }
        try:
            image = await self._render(self.card_template, data, umo, 640, True)
        except Exception:
            image = None
        return image, plain

    async def formula(self, expression, umo):
        # This parser accepts exactly the LaTeX subset produced by arithmetic().
        tokens = re.findall(r"\\times|[0-9]+|[()+-]", expression)
        if "".join(tokens) != expression.replace(" ", ""):
            raise ValueError("Unsupported formula")
        markup = "".join(
            f"<mn>{token}</mn>"
            if token.isdigit()
            else f"<mo>{'×' if token == chr(92) + 'times' else html.escape(token)}</mo>"
            for token in tokens
        )
        markup += "<mo>=</mo><mo>?</mo>"
        return await self._render(
            self.formula_template, {"mathml": markup}, umo, 1, False
        )

    async def _render(self, template, data, umo, width, transparent):
        async with self.limit, asyncio.timeout(self.timeout):
            return await self.render(
                template,
                data,
                return_url=True,
                umo=umo,
                options={
                    "type": "png",
                    "full_page": True,
                    "omit_background": transparent,
                    "viewport_width": width,
                    "viewport_height": 1,
                    "animations": "disabled",
                    "scale": "css",
                },
            )
