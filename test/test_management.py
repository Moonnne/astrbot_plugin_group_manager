"""Behavioral tests for moderation, isolation, persistence, and races."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from monitor_plugin.models.domain import Scope, parse_command

pytestmark = pytest.mark.asyncio


async def command(e, text, actor="10001", superuser=False, scope=None):
    await e.c.command(scope or e.scope, actor, parse_command(text), superuser)


async def enable(e):
    await e.db.execute(
        "set_enabled",
        {
            "scope": e.scope.key,
            "actor": "10001",
            "now": e.clock(),
            "value": 1,
        },
    )


async def request(e, qq="20001", flag=None, **extra):
    raw = {
        "post_type": "request",
        "request_type": "group",
        "sub_type": "add",
        "user_id": int(qq),
        "flag": flag or f"flag-{qq}",
        "comment": "hello",
        "time": e.clock(),
        **extra,
    }
    async with e.c.locks[e.scope.key]:
        return await e.c.approval.receive(e.scope, raw)


async def join(e, qq="20001", subtype="approve"):
    e.api.members[qq] = e.clock()
    async with e.c.locks[e.scope.key]:
        await e.c.verification.notice(
            e.scope,
            {
                "notice_type": "group_increase",
                "sub_type": subtype,
                "user_id": qq,
                "time": e.clock(),
                "operator_id": "10001",
            },
        )


async def challenge(e, qq="20001"):
    return await e.db.one("challenge", {"scope": e.scope.key, "qq": qq})


@pytest.mark.parametrize("text", ["/g", "/g help"])
async def test_help_is_available_to_members_while_disabled(env, text):
    e = env
    e.api.fail_role = True
    await command(e, text, actor="20001")
    assert e.api.sent == [(e.scope.key, "", "https://example.test/help.png", None)]
    settings = await e.db.one("settings", {"scope": e.scope.key})
    assert not settings["enabled"]
    assert not e.api.approvals and not e.api.kicks


async def test_live_join_after_snapshot_still_creates_challenge(env):
    e = env
    await enable(e)
    e.api.members["20001"] = e.clock()
    await e.c.sync_members(e.scope)
    await join(e)
    assert await challenge(e)


async def test_defaults_permissions_and_local_isolation(env):
    e = env
    settings = await e.db.one("settings", {"scope": e.scope.key})
    assert (
        not settings["enabled"]
        and not settings["auto_approve"]
        and not settings["skip_ban"]
    )
    await command(e, "/g on", actor="20001")
    assert not e.api.sent
    await command(e, "/g ban 20001 reason", actor="10002")
    assert not (await e.db.flags(e.scope, "20001"))["black"]
    await command(e, "/g ban 20001 reason")
    assert (await e.db.flags(e.scope, "20001"))["black"]
    assert not (await e.db.flags(Scope("onebot", "99999", "54321"), "20001"))["black"]
    assert await e.db.one("member", {"scope": e.scope.key, "qq": "20001"}) is None


async def test_whitelist_overrides_both_blacklists_and_skip(env):
    e = env
    await enable(e)
    for text in (
        "/g ban 20001 local",
        "/g pb 20001 shared",
        "/g wl 20001 trusted",
        "/g auto skip_ban",
    ):
        await command(e, text)
    await request(e)
    assert e.api.approvals[-1][2] is True
    await join(e)
    assert await challenge(e) is None


async def test_blacklist_manual_auto_and_independent_skip(env):
    e = env
    await enable(e)
    await command(e, "/g ban 20001 reason")
    await command(e, "/g auto")
    await request(e)
    assert not e.api.approvals
    await command(e, "/g y")
    assert e.api.approvals[-1][2] is True
    await command(e, "/g auto")
    await command(e, "/g auto skip_ban")
    await request(e, flag="new-request")
    assert e.api.approvals[-1][2] is False


async def test_individual_snapshots_stable_numbers_and_all_excludes_new(env):
    e = env
    await enable(e)
    await request(e, "20001")
    e.clock.now += 1
    await request(e, "20002")
    await command(e, "/g y")
    image_messages = [sent for sent in e.api.sent if sent[2]]
    assert [sent[1] for sent in image_messages] == [
        "审批列表 · 操作人 10001 · #1",
        "审批列表 · 操作人 10001 · #2",
    ]
    await command(e, "/g n", actor="10002")
    await command(e, "/g y 1")
    e.clock.now += 1
    await request(e, "20003")
    await command(e, "/g n all", actor="10002")
    assert [(x[1], x[2]) for x in e.api.approvals] == [
        ("20001", True),
        ("20002", False),
    ]
    await command(e, "/g y 2")
    assert len(e.api.approvals) == 2
    await command(e, "/g y")
    assert e.api.approvals[-1][1] == "20003"


async def test_expiry_snapshot_does_not_extend_request(env):
    e = env
    await enable(e)
    await request(e, "20001")
    e.clock.now += 1700
    await request(e, "20002")
    await command(e, "/g y")
    e.clock.now += 101
    await command(e, "/g y 1")
    assert not e.api.approvals
    await command(e, "/g y 2")
    assert e.api.approvals[-1][1] == "20002"


async def test_parallel_decision_once_and_unknown_not_retried(env):
    e = env
    await enable(e)
    await request(e)
    e.api.fail_approval = True
    await asyncio.gather(command(e, "/g y"), command(e, "/g n", "10002"))
    assert len(e.api.approvals) == 1
    row = await e.db.one(
        "request_by_flag", {"scope": e.scope.key, "flag": "flag-20001"}
    )
    assert row["status"] == "unknown"


async def test_public_multiple_owners_revoke_and_superuser(env):
    e = env
    e.api.roles["10003"] = "owner"
    await command(e, "/g pb 20001 first")
    await command(e, "/g pb 20001 second", "10003")
    e.api.roles["10001"] = "member"
    await command(e, "/g rmpb 20001", "10001")
    flags = await e.db.flags(e.scope, "20001")
    assert (
        len(flags["public_entries"]) == 1
        and flags["public_entries"][0]["actor"] == "10003"
    )
    await command(e, "/g rmpb 20001", "20002")
    assert (await e.db.flags(e.scope, "20001"))["public"]
    await command(e, "/g rmpb 20001", "10000", True)
    assert not (await e.db.flags(e.scope, "20001"))["public"]


async def test_off_cancels_existing_but_tracks_members(env):
    e = env
    await enable(e)
    await request(e)
    await join(e, "20002")
    row = await challenge(e, "20002")
    await e.c.verification.deliver(row)
    await command(e, "/g off", "10002")
    assert await challenge(e, "20002") is None
    await join(e, "20003")
    assert await challenge(e, "20003") is None
    assert (await e.db.one("member", {"scope": e.scope.key, "qq": "20003"}))[
        "presence"
    ] == "present"
    await command(e, "/g on")
    await command(e, "/g y")
    assert not e.api.approvals and not e.api.kicks


async def test_answer_is_scoped_correct_and_cancels_timeout(env):
    e = env
    await enable(e)
    await join(e)
    row = await challenge(e)
    await e.c.verification.deliver(row)
    await command(e, f"/ans {row['answer']}", actor="20002")
    assert (await challenge(e))["state"] == "waiting"
    await command(e, "/ans 999999", actor="20001")
    assert (await challenge(e))["attempts"] == 1
    await command(e, f"/ans {row['answer']}", actor="20001")
    assert await challenge(e) is None
    e.clock.now += 601
    await e.c.verification.expire(row)
    assert not e.api.kicks


async def test_render_failure_never_arms_kick(env):
    e = env
    await enable(e)
    await join(e)
    row = await challenge(e)
    e.renderer.fail_formula = True
    await e.c.verification.deliver(row)
    stored = await challenge(e)
    assert stored["deadline"] is None and stored["state"] == "delivery"
    e.clock.now += 900
    await e.c.verification.expire(row)
    assert not e.api.kicks


async def test_timeout_kick_once_without_permanent_ban(env):
    e = env
    await enable(e)
    await join(e)
    row = await challenge(e)
    await e.c.verification.deliver(row)
    e.clock.now += 601
    await asyncio.gather(e.c.verification.expire(row), e.c.verification.expire(row))
    assert e.api.kicks == [(e.scope.key, "20001")]
    assert (await e.db.one("member", {"scope": e.scope.key, "qq": "20001"}))[
        "presence"
    ] == "present"


async def test_leave_rejoin_old_deadline_cannot_kick_new_membership(env):
    e = env
    await enable(e)
    await join(e)
    old = await challenge(e)
    await e.c.verification.deliver(old)
    e.clock.now += 5
    await e.c.verification.notice(
        e.scope,
        {
            "notice_type": "group_decrease",
            "sub_type": "leave",
            "user_id": "20001",
            "time": e.clock(),
        },
    )
    e.clock.now += 5
    await join(e)
    new = await challenge(e)
    assert old["session_id"] != new["session_id"]
    e.clock.now += 601
    await e.c.verification.expire(old)
    assert not e.api.kicks
    member = await e.db.one("member", {"scope": e.scope.key, "qq": "20001"})
    assert member["leave_count"] == 1 and member["first_join"] < member["last_join"]


async def test_whitelist_during_verification_and_admin_promotion(env):
    e = env
    await enable(e)
    await join(e)
    row = await challenge(e)
    await e.c.verification.deliver(row)
    await command(e, "/g wl 20001 trusted")
    e.clock.now += 601
    await e.c.verification.expire(row)
    assert not e.api.kicks
    await join(e, "20002")
    row2 = await challenge(e, "20002")
    await e.c.verification.deliver(row2)
    e.api.roles["20002"] = "admin"
    e.clock.now += 601
    await e.c.verification.expire(row2)
    assert not e.api.kicks


async def test_duplicate_request_and_notice_no_double_counts(env):
    e = env
    await enable(e)
    first = await request(e)
    e.clock.now += 100
    assert await request(e) is None
    assert (await e.db.one("request", {"scope": e.scope.key, "id": first[0]}))[
        "expires_at"
    ] == e.clock() + 1700
    await join(e)
    await join(e)
    assert (
        len(await e.db.all("history_local", {"scope": e.scope.key, "qq": "20001"})) == 1
    )


async def test_invite_identifies_target_and_excludes_robot(env):
    e = env
    await enable(e)
    assert await request(e, sub_type="invite") is None
    assert await request(e, sub_type="invite", invited_id=99999) is None
    result = await request(e, sub_type="invite", invited_id=20002)
    row = await e.db.one("request", {"scope": e.scope.key, "id": result[0]})
    assert row["qq"] == "20002" and row["inviter"] == "20001"


async def test_restart_recovers_expired_challenge_without_immediate_kick(env):
    e = env
    await enable(e)
    await join(e)
    old = await challenge(e)
    await e.c.verification.deliver(old)
    e.clock.now += 601
    await e.db.execute("recover_challenges", {"now": e.clock()})
    recovered = await challenge(e)
    assert recovered["state"] == "delivery" and recovered["deadline"] is None
    await e.c.verification.deliver(recovered)
    assert (await challenge(e))["deadline"] == e.clock() + 600
    assert not e.api.kicks


async def test_history_cross_group_requires_current_public_flag(env):
    e = env
    await join(e)
    other = Scope("onebot", "99999", "54321")
    await e.db.execute(
        "ensure_group",
        {"scope": other.key, "umo": "other", "actor": "10001", "now": e.clock()},
    )
    e.clock.now += 5
    await e.c.verification.notice(
        other,
        {
            "notice_type": "group_increase",
            "sub_type": "approve",
            "user_id": "20001",
            "time": e.clock(),
        },
    )
    await command(e, "/g info 20001")
    assert all(row["scope"] == e.scope.key for row in e.renderer.cards[-1][2])
    await command(e, "/g pb 20001 shared")
    await command(e, "/g info 20001")
    assert {row["scope"] for row in e.renderer.cards[-1][2]} == {other.key, e.scope.key}
    await command(e, "/g rmpb 20001")
    await command(e, "/g info 20001")
    assert all(row["scope"] == e.scope.key for row in e.renderer.cards[-1][2])


async def test_transaction_failure_rolls_back_all_changes(env):
    e = env
    with pytest.raises(KeyError):
        await e.db.batch(
            [
                (
                    "set_enabled",
                    {
                        "scope": e.scope.key,
                        "value": 1,
                        "actor": "10001",
                        "now": e.clock(),
                    },
                ),
                ("missing_query", {}),
            ]
        )
    assert not (await e.db.one("settings", {"scope": e.scope.key}))["enabled"]


async def test_rendering_off_does_not_emit_stale_card(env):
    e = env
    await enable(e)
    result = await request(e)
    started, release = asyncio.Event(), asyncio.Event()
    original = e.renderer.card

    async def slow(*args):
        started.set()
        await release.wait()
        return await original(*args)

    e.renderer.card = slow
    task = asyncio.create_task(e.c.show_request(e.scope, *result))
    await started.wait()
    await command(e, "/g off")
    release.set()
    assert await task is False
    assert all(sent[2] is None for sent in e.api.sent)


async def test_rendering_snapshot_cannot_reactivate_after_empty_refresh(env):
    e = env
    await enable(e)
    await request(e)
    await request(e, "20002")
    started, release = asyncio.Event(), asyncio.Event()
    original = e.renderer.card

    async def slow(*args):
        started.set()
        await release.wait()
        return await original(*args)

    e.renderer.card = slow
    task = asyncio.create_task(command(e, "/g y"))
    await started.wait()
    e.clock.now += 1801
    await command(e, "/g y")
    release.set()
    await task
    assert not e.api.sent
    assert not await e.db.all(
        "snapshot_items",
        {
            "scope": e.scope.key,
            "actor": "10001",
            "number": 0,
            "now": e.clock(),
        },
    )


async def test_batch_stops_after_operator_loses_role(env):
    e = env
    await enable(e)
    await request(e)
    await request(e, "20002")
    await command(e, "/g y", "10002")
    original = e.api.approve

    async def revoke_after_first(*args):
        await original(*args)
        e.api.roles["10002"] = "member"

    e.api.approve = revoke_after_first
    await command(e, "/g y all", "10002")
    assert len(e.api.approvals) == 1


async def test_unknown_role_and_missing_bot_permission_do_not_kick(env):
    e = env
    await enable(e)
    await join(e)
    row = await challenge(e)
    await e.c.verification.deliver(row)
    e.clock.now += 601
    e.api.fail_role = True
    await e.c.verification.expire(row)
    assert not e.api.kicks
    e.api.fail_role = False
    e.api.roles["99999"] = "member"
    await e.c.verification.expire(row)
    assert not e.api.kicks
    stored = await e.db.one("challenge_id", {"id": row["id"]})
    assert stored["state"] == "no_permission"


async def test_database_cancel_finishes_transaction_before_release(env):
    e = env
    await e.db.lock.acquire()
    task = asyncio.create_task(
        e.db.execute(
            "set_enabled",
            {
                "scope": e.scope.key,
                "value": 1,
                "actor": "10001",
                "now": e.clock(),
            },
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    e.db.lock.release()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await e.db.one("settings", {"scope": e.scope.key}))["enabled"] == 1
    assert not e.db.db.in_transaction


async def test_new_join_after_missed_leave_supersedes_old_challenge(env):
    e = env
    await enable(e)
    await join(e)
    old = await challenge(e)
    await e.c.verification.deliver(old)
    e.clock.now += 60
    await join(e)
    new = await challenge(e)
    assert new["session_id"] != old["session_id"]
    assert (await e.db.one("challenge_id", {"id": old["id"]}))["state"] == "superseded"
    member = await e.db.one("member", {"scope": e.scope.key, "qq": "20001"})
    assert member["leave_count"] == 0
    e.clock.now += 601
    await e.c.verification.expire(old)
    assert not e.api.kicks


@pytest.mark.parametrize("fail", [False, True])
async def test_approval_operation_time_is_successful_completion(env, fail):
    e = env
    await enable(e)
    await request(e)
    started = e.clock()
    original = e.api.approve

    async def delayed(*args):
        e.clock.now += 8
        if fail:
            raise TimeoutError()
        return await original(*args)

    e.api.approve = delayed
    await command(e, "/g y")
    row = await e.db.one(
        "request_by_flag", {"scope": e.scope.key, "flag": "flag-20001"}
    )
    assert row["status"] == ("unknown" if fail else "approved")
    assert row["decided_at"] == (None if fail else started + 8)
    observed = []
    original_card = e.renderer.card

    async def capture_card(*args):
        observed.append(args[5])
        return await original_card(*args)

    e.renderer.card = capture_card
    await command(e, "/g info 20001")
    assert observed[0]["decided_at"] == row["decided_at"]


async def test_group_scope_blocks_commands_events_and_member_snapshots(env):
    e = env
    other = Scope("onebot", "99999", "54321")
    for raw, text in (
        ({"post_type": "message"}, "/g on"),
        ({"post_type": "request", "request_type": "group"}, ""),
        ({"post_type": "notice", "notice_type": "group_increase"}, ""),
    ):
        await e.c.handle(other, "outside", raw, "10000", text, True)
    await command(e, "/g help", actor="10000", superuser=True, scope=other)
    e.api.call = AsyncMock(return_value=[])
    await e.c.sync_members(other)
    e.api.call.assert_not_called()
    assert not await e.db.one("settings", {"scope": other.key})
    assert not e.api.sent and not e.api.kicks and not e.api.approvals


@pytest.mark.parametrize("waiting", [False, True])
async def test_excluded_group_cannot_deliver_or_expire_verification(env, waiting):
    e = env
    await enable(e)
    await join(e)
    row = await challenge(e)
    if waiting:
        await e.c.verification.deliver(row)
        e.clock.now += 601
    e.api.sent.clear()
    e.c.enabled_groups = frozenset()
    await (e.c.verification.expire(row) if waiting else e.c.verification.deliver(row))
    assert not e.api.sent and not e.api.kicks
    assert (await challenge(e))["state"] == ("waiting" if waiting else "delivery")


async def test_reload_removes_group_tasks_and_retains_history(env):
    e = env
    await enable(e)
    await request(e)
    await request(e, "20002")
    await command(e, "/g y")
    await join(e, "20003")
    row = await challenge(e, "20003")
    await e.c.verification.deliver(row)
    e.api.sent.clear()
    replacement = type(e.c)(
        e.db,
        e.api,
        e.renderer,
        e.c.host_admin,
        e.c.log,
        e.clock,
        enabled_groups=frozenset(),
    )
    await replacement.start()
    try:
        await asyncio.sleep(0)
        settings = await e.db.one("settings", {"scope": e.scope.key})
        assert not settings["enabled"]
        assert not await e.db.all("pending", {"scope": e.scope.key, "now": e.clock()})
        assert (
            await e.db.one(
                "request_by_flag", {"scope": e.scope.key, "flag": "flag-20001"}
            )
        )["status"] == "abandoned"
        assert (await e.db.one("challenge_id", {"id": row["id"]}))[
            "state"
        ] == "cancelled"
        snapshot = await e.db.one(
            "latest_snapshot", {"scope": e.scope.key, "actor": "10001"}
        )
        assert not snapshot["active"]
        assert await e.db.one("member", {"scope": e.scope.key, "qq": "20003"})
        assert not e.api.sent and not e.api.kicks
    finally:
        await replacement.stop()
