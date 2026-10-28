"""Moderation rules, group snapshots, expiry, and resource ownership."""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from monitor_plugin.core.commands import parse_command
from monitor_plugin.core.settings import Settings
from monitor_plugin.models.group_event import GroupEvent
from monitor_plugin.models.scope import Scope
from monitor_plugin.utils.scope import scope_key

pytestmark = pytest.mark.asyncio


async def command(e, text, actor="10001", superuser=False, scope=None):
    await e.c.commands.execute(scope or e.scope, actor, parse_command(text), superuser)


async def enable(e):
    await e.settings.update(e.scope, "10001", e.clock(), enabled=True)


async def request(e, qq="20001", flag=None):
    return await e.c.approval.receive(
        GroupEvent(
            e.scope,
            "request",
            qq,
            e.clock(),
            request_id=flag or f"flag-{qq}",
            reason="hello",
        )
    )


async def notice(e, qq="20001", kind="group_increase", subtype="approve"):
    if kind == "group_increase":
        e.api.members[qq] = e.clock()
    await e.c.handle(
        GroupEvent(
            e.scope,
            "join" if kind == "group_increase" else "leave",
            qq,
            e.clock(),
            subtype=subtype,
            inviter="10001",
            method="QQ 10001 邀请" if subtype == "invite" else "申请入群",
        )
    )
    await asyncio.gather(*list(e.c.background))


@pytest.mark.parametrize("text", ["/g", "/g help"])
async def test_help_is_image_even_when_disabled(env, text):
    env.api.fail_role = True
    await command(env, text, actor="20001")
    assert env.api.sent == [
        (scope_key(env.scope), "", "https://example.test/help.png", None)
    ]


async def test_help_text_fallback_mentions_caller(env):
    async def fallback(now, umo):
        return None, "指令清单"

    env.renderer.help = fallback
    await command(env, "/g help", actor="20001")
    assert env.api.sent == [(scope_key(env.scope), "指令清单", None, "20001")]


async def test_database_has_only_three_durable_tables(env):
    async with env.db.db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ) as cursor:
        names = {row[0] for row in await cursor.fetchall()}
    assert names == {"group_members", "group_list_entries", "public_blacklist_entries"}


async def test_database_rolls_back_partial_write_and_remains_usable(env):
    with pytest.raises(KeyError):
        await env.db.batch(
            [
                (
                    "add_local",
                    {
                        "scope": scope_key(env.scope),
                        "qq": "20001",
                        "kind": "black",
                        "note": "rollback",
                        "actor": "10001",
                        "now": env.clock(),
                    },
                ),
                ("missing_query", {}),
            ]
        )
    assert not (await env.db.flags(env.scope, "20001"))["black"]
    await command(env, "/g ban 20001 valid")
    assert (await env.db.flags(env.scope, "20001"))["black"]


async def test_umo_json_settings_and_bot_isolation(env):
    await command(env, "/g on")
    path = env.settings.path(env.scope)
    assert path.parent.name == "settings"
    assert path.name == "onebot%3AGroupMessage%3A12345.json"
    assert json.loads(path.read_text(encoding="utf-8"))["bots"]["99999"]["enabled"]
    reloaded = Settings(path.parent)
    assert (await reloaded.get(env.scope))["enabled"]
    other_bot = Scope("onebot", "88888", "12345")
    assert not (await reloaded.get(other_bot))["enabled"]


async def test_permissions_scope_and_admin_override(env):
    await command(env, "/g on", actor="20001")
    assert not (await env.settings.get(env.scope))["enabled"]
    await command(env, "/g on", actor="10002")
    await command(env, "/g ban 20001 ad", actor="10002")
    assert not (await env.db.flags(env.scope, "20001"))["black"]
    await command(env, "/g ban 20001 ad", actor="10000", superuser=True)
    assert (await env.db.flags(env.scope, "20001"))["black"]
    other = Scope("onebot", "99999", "54321")
    assert not (await env.db.flags(other, "20001"))["black"]
    await command(env, "/g on", actor="10000", superuser=True, scope=other)
    assert not (await env.settings.get(other))["enabled"]


async def test_whitelist_wins_over_both_blacklists_and_skip(env):
    await enable(env)
    for text in (
        "/g auto skip_ban",
        "/g ban 20001 ad",
        "/g pb 20001 ad",
        "/g wl 20001 trusted",
    ):
        await command(env, text)
    row = await request(env)
    assert row.status == "approved"
    assert env.api.approvals[-1][2] is True
    await notice(env)
    assert not env.c.verification.challenges
    assert (await env.db.member(env.scope, "20001"))["verification"] == "exempt"


async def test_auto_blacklist_downgrades_to_manual_and_skip_is_independent(env):
    await enable(env)
    await command(env, "/g auto")
    await command(env, "/g ban 20001 ad")
    assert (await request(env)).status == "pending"
    assert not env.api.approvals
    await command(env, "/g auto")
    await command(env, "/g auto skip_ban")
    row = await request(env, "20002")
    assert row.status == "pending"
    await command(env, "/g pb 20003 ad")
    assert (await request(env, "20003")).status == "rejected"


async def test_public_list_multiple_adders_and_original_adder_revocation(env):
    await command(env, "/g pb 20001 first")
    await command(env, "/g pb 20001 second", actor="10000", superuser=True)
    env.api.roles["10001"] = "member"
    await command(env, "/g rmpb 20001")
    flags = await env.db.flags(env.scope, "20001")
    assert flags["public"] and [row["actor"] for row in flags["public_entries"]] == [
        "10000"
    ]
    await command(env, "/g rmpb 20001", actor="10002")
    assert (await env.db.flags(env.scope, "20001"))["public"]
    await command(env, "/g rmpb 20001", actor="10000", superuser=True)
    assert not (await env.db.flags(env.scope, "20001"))["public"]


async def test_group_shared_snapshot_all_does_not_include_new_requests(env):
    await enable(env)
    first, second = await request(env), await request(env, "20002")
    await command(env, "/g y", actor="10001")
    snapshot = env.c.approval.snapshots[scope_key(env.scope)]
    assert snapshot.active and list(snapshot.items.values()) == [first.id, second.id]
    third = await request(env, "20003")
    await command(env, "/g y 1", actor="10002")
    assert first.status == "approved"
    await command(env, "/g n all", actor="10002")
    assert second.status == "rejected" and third.status == "pending"
    assert env.c.approval.snapshots[scope_key(env.scope)] is snapshot


async def test_group_snapshot_refresh_and_expiry_do_not_extend_request(env):
    await enable(env)
    first = await request(env)
    await request(env, "20002")
    await command(env, "/g y")
    old = env.c.approval.snapshots[scope_key(env.scope)]
    env.clock.now += 100
    await request(env, "20003")
    await command(env, "/g n", actor="10002")
    new = env.c.approval.snapshots[scope_key(env.scope)]
    assert new.id != old.id and len(new.items) == 3
    env.clock.now += 1701
    await command(env, "/g y 1")
    assert first.status == "pending" and not env.api.approvals


async def test_unknown_approval_is_never_retried(env):
    await enable(env)
    row = await request(env)
    env.api.fail_approval = True
    await command(env, "/g y")
    await command(env, "/g y")
    assert row.status == "unknown" and len(env.api.approvals) == 1


@pytest.mark.parametrize("subtype", ["approve", "invite"])
async def test_actual_join_sends_formula_and_answer_passes(env, subtype):
    await enable(env)
    await notice(env, subtype=subtype)
    row = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    assert row.state == "waiting" and row.deadline == env.clock() + 600
    assert env.api.sent[-1][2] == "https://example.test/formula.png"
    await command(env, "/ans 999999", actor="20001")
    assert row.deadline == env.clock() + 600
    await command(env, f"/ans {row.answer}", actor="20001")
    assert not env.c.verification.challenges
    assert (await env.db.member(env.scope, "20001"))["verification"] == "passed"
    before = list(env.api.sent)
    await command(env, "/ans 1", actor="20002")
    assert env.api.sent == before


async def test_failed_formula_does_not_arm_kick_and_has_bounded_retries(env):
    await enable(env)
    env.renderer.fail_formula = True
    await notice(env)
    row = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    assert row.state == "delivery" and row.deadline == 0
    for _ in range(2):
        env.clock.now += 31
        await env.c.verification.deliver(row)
    assert not env.c.verification.challenges and not env.api.kicks


async def test_timeout_rechecks_membership_and_removes_once(env):
    await enable(env)
    await notice(env)
    row = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    env.clock.now += 601
    await asyncio.gather(env.c.verification.expire(row), env.c.verification.expire(row))
    assert env.api.kicks == [(scope_key(env.scope), "20001")]
    assert not env.c.verification.challenges


async def test_timeout_does_not_kick_rejoin_or_admin(env):
    await enable(env)
    await notice(env)
    old = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    env.clock.now += 601
    env.api.members["20001"] = env.clock()
    await env.c.verification.expire(old)
    assert not env.api.kicks
    env.clock.now += 1
    await notice(env, "20002")
    row = env.c.verification.challenges[(scope_key(env.scope), "20002")]
    env.clock.now += 601
    env.api.roles["20002"] = "admin"
    await env.c.verification.expire(row)
    assert not env.api.kicks


async def test_off_cancels_transients_but_still_records_membership(env):
    await enable(env)
    await request(env)
    await request(env, "20002")
    await command(env, "/g y")
    await notice(env)
    await command(env, "/g off")
    assert (
        not env.c.approval.requests
        and not env.c.approval.snapshots
        and not env.c.verification.challenges
    )
    env.clock.now += 1
    await notice(env, kind="group_decrease", subtype="leave")
    member = await env.db.member(env.scope, "20001")
    assert member["leave_count"] == 1 and member["presence"] == "absent"
    await notice(env, kind="group_decrease", subtype="leave")
    assert (await env.db.member(env.scope, "20001"))["leave_count"] == 1
    env.clock.now += 1
    await notice(env)
    member = await env.db.member(env.scope, "20001")
    assert member["first_join"] < member["last_join"]
    assert not env.c.verification.challenges


async def test_info_only_exposes_cross_group_aggregate_for_public_targets(env):
    other = Scope("other", "88888", "54321")
    await env.db.execute(
        "join_member",
        {
            "scope": scope_key(other),
            "qq": "20001",
            "happened": env.clock(),
            "session": "x",
            "method": "申请入群",
            "reason": "history",
            "now": env.clock(),
        },
    )
    await command(env, "/g info 20001")
    assert not env.renderer.cards[-1][2]
    await command(env, "/g pb 20001 ad")
    await command(env, "/g info 20001")
    assert env.renderer.cards[-1][2][0]["scope"] == scope_key(other)
    assert all(text == "" and image for _, text, image, _ in env.api.sent[-1:])
    env.renderer.fail_card = True
    before = list(env.api.sent)
    await command(env, "/g info 20001")
    assert env.api.sent == before


async def test_runtime_caps_ttl_and_shutdown_cleanup(env):
    await enable(env)
    env.c.approval.MAX_PER_GROUP = 2
    await request(env)
    await request(env, "20002")
    assert await request(env, "20003") is None
    env.clock.now += 1801
    env.c.approval.prune()
    assert not env.c.approval.requests
    await env.c.start()
    env.c.enqueue(GroupEvent(env.scope, "join", "20001", env.clock()))
    await env.c.queue.join()
    await env.c.stop()
    assert not env.c.background and not env.c.workers and not env.c.seen
    assert not env.c.verification.challenges and env.c.queue.empty()


async def test_off_while_formula_is_rendering_discards_late_image(env):
    await enable(env)
    started, release = asyncio.Event(), asyncio.Event()

    async def render(*args):
        started.set()
        await release.wait()
        return "https://example.test/late.png"

    env.renderer.formula = render
    await env.c.members.receive(GroupEvent(env.scope, "join", "20001", env.clock()))
    await asyncio.wait_for(started.wait(), 1)
    await command(env, "/g off")
    release.set()
    await asyncio.gather(*list(env.c.background))
    assert not env.c.verification.challenges
    assert all(
        image != "https://example.test/late.png" for _, _, image, _ in env.api.sent
    )
    assert not env.api.kicks


async def test_unload_interrupts_verification_and_drains_managed_tasks(env):
    await enable(env)
    await notice(env)
    assert env.c.verification.challenges
    await env.c.stop()
    assert (await env.db.member(env.scope, "20001"))["verification"] == "interrupted"
    assert not env.c.background and not env.c.verification.challenges
    assert not env.api.kicks


async def test_kick_timeout_never_dispatches_twice(env):
    await enable(env)
    await notice(env)
    row = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    env.api.kick = AsyncMock(side_effect=TimeoutError)
    env.clock.now += 601
    await env.c.verification.expire(row)
    await env.c.verification.expire(row)
    env.api.kick.assert_awaited_once()
    assert (await env.db.member(env.scope, "20001"))["verification"] == "unknown"


async def test_empty_formula_result_does_not_arm_timeout(env):
    await enable(env)
    env.renderer.formula = AsyncMock(return_value=None)
    await notice(env)
    row = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    assert row.state == "delivery" and row.deadline == 0
    assert not env.api.sent


async def test_scope_guard_blocks_raw_events_and_expiry(env):
    await enable(env)
    await notice(env)
    row = env.c.verification.challenges[(scope_key(env.scope), "20001")]
    env.c.scope_guard = AsyncMock(return_value=False)
    env.clock.now += 601
    await env.c.verification.expire(row)
    await notice(env, "20002")
    assert not env.api.kicks and not await env.db.member(env.scope, "20002")


async def test_member_snapshot_updates_known_join_without_inventing_leaves(env):
    await notice(env)
    env.clock.now += 100
    env.api.members["20001"] = env.clock()
    await env.c.members.sync(env.scope)
    member = await env.db.member(env.scope, "20001")
    assert member["last_join"] == env.clock() and member["leave_count"] == 0
    assert member["session_id"] is None
