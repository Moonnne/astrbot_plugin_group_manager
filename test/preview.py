"""Render synthetic preview cards with an isolated headless Chromium process."""

import asyncio
import base64
import importlib
import sys
import types
from pathlib import Path

from jinja2 import Environment, StrictUndefined

ROOT = Path(__file__).resolve().parents[1]
package = types.ModuleType("monitor_preview")
package.__path__ = [str(ROOT)]
sys.modules["monitor_preview"] = package
Renderer = importlib.import_module("monitor_preview.core.rendering").Renderer


async def preview(browser_path, output):
    """Capture actual template bounds using a new isolated browser context."""
    from playwright.async_api import async_playwright

    output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(
            executable_path=str(browser_path), headless=True
        )
        try:
            page = await browser.new_page()

            async def capture(template, data, **kwargs):
                name = (
                    "formula"
                    if "mathml" in data
                    else (
                        "help"
                        if data.get("panel") == "help"
                        else "card-lists"
                        if data.get("list_cards")
                        else "card"
                    )
                    + ("-light" if data["light"] else "-dark")
                )
                markup = (
                    Environment(undefined=StrictUndefined)
                    .from_string(template)
                    .render(data)
                )
                await page.set_viewport_size(
                    {"width": kwargs["options"]["viewport_width"], "height": 1}
                )
                await page.set_content(markup, wait_until="load")
                await page.evaluate("document.fonts.ready")
                target = output / f"{name}.png"
                await page.screenshot(
                    path=str(target),
                    full_page=True,
                    omit_background=kwargs["options"]["omit_background"],
                )
                bounds = await page.locator("body").bounding_box()
                print(f"{target}: {bounds}")
                return str(target)

            renderer = Renderer(capture, ROOT / "resource/templates")
            avatar = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="#9da8b3"/><text x="50" y="70" font-size="64" text-anchor="middle" fill="white">M</text></svg>'
            profile = {
                "nickname": "示例成员",
                "level": "64",
                "area": "天津 · 南开",
                "sex": "男",
                "age": "21",
                "signature": "保持好奇，认真生活。",
                "cover": renderer.help_background,
                "avatar": "data:image/svg+xml;base64,"
                + base64.b64encode(avatar.encode()).decode(),
            }
            flags = {
                "white": False,
                "black": False,
                "public": False,
                "entries": [],
                "public_entries": [],
            }
            request = {
                "subtype": "invite",
                "inviter": "10003",
                "reason": "希望加入群聊，一起交流。",
                "status": "pending",
                "expires_at": 1792557000,
            }
            for now in (1792555200, 1792598400):
                request["expires_at"] = now + 1800
                await renderer.card(
                    profile,
                    "20001",
                    flags,
                    None,
                    [],
                    request,
                    "审批列表 · 操作人 10001 · #1",
                    now,
                    "preview",
                )
                list_flags = {
                    "white": True,
                    "black": True,
                    "public": True,
                    "entries": [
                        {
                            "kind": "white",
                            "actor": "10001",
                            "created_at": now - 3600,
                            "note": "已核实身份，免入群验证。",
                        },
                        {
                            "kind": "black",
                            "actor": "10002",
                            "created_at": now - 1800,
                            "note": "曾多次发送广告，需人工核实。",
                        },
                    ],
                    "public_entries": [
                        {
                            "actor": "10003",
                            "created_at": now - 900,
                            "note": "公共风险记录：其他群曾记录重复广告行为。",
                        }
                    ],
                }
                await renderer.card(
                    profile,
                    "20001",
                    list_flags,
                    {
                        "first_join": now - 90 * 86400,
                        "last_join": now - 7 * 86400,
                        "last_leave": now - 2 * 86400,
                        "leave_count": 3,
                    },
                    [],
                    {
                        **request,
                        "status": "approved",
                        "actor": "10001",
                        "decided_at": now - 60,
                    },
                    "名单展示示例",
                    now,
                    "preview",
                )
                await renderer.help(now, "preview")
            await renderer.formula(r"(7 + 3) \times 4", "preview")
        finally:
            await browser.close()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("Usage: preview.py BROWSER_PATH OUTPUT_DIRECTORY")
    asyncio.run(preview(Path(sys.argv[1]), Path(sys.argv[2])))
