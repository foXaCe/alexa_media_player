"""Tests for the Do-Not-Disturb sync helpers in ``setup.dnd``.

Covers both public coroutines:

* ``update_dnd_state`` (decorated with ``@_catch_login_errors`` so ``login_obj``
  is its first positional argument) -- happy path, the "no useful payload"
  branches, the in-function error handlers, and the throttling fork that defers
  work to a background task.
* ``schedule_update_dnd_state`` -- the deferred worker: early exits, the
  cooldown sleep/loop, the missing ``login_obj`` guard, the successful forced
  refresh, and cancellation/finally cleanup.

Style mirrors ``tests/test_setup_notifications.py``: a module-level ``_MOD``
patch-path constant and small ``_ctx`` / ``_login`` factories.
"""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyConnectionError, AlexapyLoginError
import pytest

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.dnd import (
    schedule_update_dnd_state,
    update_dnd_state,
)

EMAIL = "user@example.com"
_MOD = "custom_components.alexa_media.setup.dnd"


def _ctx(email=EMAIL, account=None):
    hass = MagicMock()
    hass.data = {
        DATA_ALEXAMEDIA: {"accounts": {email: account if account is not None else {}}}
    }
    ctx = SetupContext(hass=hass, config_entry=MagicMock(), email=email)
    return ctx, hass


def _login(email=EMAIL):
    login = MagicMock()
    login.email = email
    return login


# ---------------------------------------------------------------------------
# update_dnd_state -- happy path / no-op payloads
# ---------------------------------------------------------------------------


async def test_update_dnd_state_dispatches_on_success():
    """A populated DND list is dispatched and the run time is recorded."""
    ctx, hass = _ctx()
    dnd = {
        "doNotDisturbDeviceStatusList": [
            {"deviceSerialNumber": "SER1", "enabled": True}
        ]
    }
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
    ):
        api.get_dnd_state = AsyncMock(return_value=dnd)
        await update_dnd_state(_login(), ctx)

    dispatch.assert_called_once()
    args = dispatch.call_args.args
    assert args[0] is hass
    assert args[2] == {"dnd_update": dnd["doNotDisturbDeviceStatusList"]}
    # The unthrottled path stamps the last-run time.
    assert EMAIL in ctx.last_dnd_update_times


async def test_update_dnd_state_no_dispatch_when_api_returns_none():
    """``get_dnd_state`` returning ``None`` falls through without dispatching."""
    ctx, _ = _ctx()
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
    ):
        api.get_dnd_state = AsyncMock(return_value=None)
        await update_dnd_state(_login(), ctx)

    dispatch.assert_not_called()
    # The fetch was still attempted, so the run time is recorded.
    assert EMAIL in ctx.last_dnd_update_times


async def test_update_dnd_state_no_dispatch_when_key_missing():
    """A non-``None`` payload lacking the status list does not dispatch."""
    ctx, _ = _ctx()
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
    ):
        api.get_dnd_state = AsyncMock(return_value={"unexpected": 1})
        await update_dnd_state(_login(), ctx)

    dispatch.assert_not_called()


# ---------------------------------------------------------------------------
# update_dnd_state -- in-function error handlers
# ---------------------------------------------------------------------------


async def test_update_dnd_state_handles_timeout():
    """A ``TimeoutError`` is caught locally: no dispatch, no propagation."""
    ctx, _ = _ctx()
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
    ):
        api.get_dnd_state = AsyncMock(side_effect=TimeoutError)
        # Returns ``None`` (swallowed) rather than raising.
        assert await update_dnd_state(_login(), ctx) is None

    dispatch.assert_not_called()


async def test_update_dnd_state_handles_connection_error():
    """A connection error hits the broad ``except`` and is swallowed."""
    ctx, _ = _ctx()
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
    ):
        api.get_dnd_state = AsyncMock(side_effect=AlexapyConnectionError("down"))
        assert await update_dnd_state(_login(), ctx) is None

    dispatch.assert_not_called()


async def test_update_dnd_state_login_error_swallowed_not_relogin():
    """A login error from ``get_dnd_state`` is caught by the broad ``except``.

    Because the in-function handler runs first, the ``@_catch_login_errors``
    relogin path never fires -- ``login.test_loggedin`` is not consulted.
    """
    ctx, _ = _ctx()
    login = _login()
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
    ):
        api.get_dnd_state = AsyncMock(side_effect=AlexapyLoginError("bad"))
        assert await update_dnd_state(login, ctx) is None

    dispatch.assert_not_called()
    login.test_loggedin.assert_not_called()


# ---------------------------------------------------------------------------
# update_dnd_state -- throttling fork
# ---------------------------------------------------------------------------


async def test_update_dnd_state_throttles_and_schedules_task():
    """A rapid second call defers via a freshly scheduled background task."""
    ctx, _ = _ctx()
    # A very recent run keeps us inside the cooldown window.
    ctx.last_dnd_update_times[EMAIL] = datetime.now(UTC)
    fake_task = MagicMock()
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.async_dispatcher_send") as dispatch,
        # Force a plain MagicMock: the real symbol is ``async def`` so a bare
        # ``patch`` would build an AsyncMock and leak an un-awaited coroutine.
        patch(f"{_MOD}.schedule_update_dnd_state", new_callable=MagicMock) as sched,
        patch(f"{_MOD}.asyncio.create_task", return_value=fake_task) as create_task,
    ):
        api.get_dnd_state = AsyncMock()
        await update_dnd_state(_login(), ctx)

    assert ctx.pending_dnd_updates[EMAIL] is True
    sched.assert_called_once_with(ctx, EMAIL)
    create_task.assert_called_once()
    assert ctx.scheduled_dnd_tasks[EMAIL] is fake_task
    # Throttled: no live fetch and nothing dispatched.
    api.get_dnd_state.assert_not_called()
    dispatch.assert_not_called()


async def test_update_dnd_state_throttles_when_task_already_scheduled():
    """When a live deferred task already exists, no second one is spawned."""
    ctx, _ = _ctx()
    ctx.last_dnd_update_times[EMAIL] = datetime.now(UTC)
    existing = MagicMock()
    existing.done.return_value = False
    ctx.scheduled_dnd_tasks[EMAIL] = existing
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.schedule_update_dnd_state", new_callable=MagicMock) as sched,
        patch(f"{_MOD}.asyncio.create_task") as create_task,
    ):
        api.get_dnd_state = AsyncMock()
        await update_dnd_state(_login(), ctx)

    assert ctx.pending_dnd_updates[EMAIL] is True
    sched.assert_not_called()
    create_task.assert_not_called()
    assert ctx.scheduled_dnd_tasks[EMAIL] is existing
    api.get_dnd_state.assert_not_called()


# ---------------------------------------------------------------------------
# schedule_update_dnd_state -- the deferred worker
# ---------------------------------------------------------------------------


async def test_schedule_returns_immediately_without_pending():
    """No pending flag -> the worker pops its task pointer and exits at once."""
    ctx, _ = _ctx()
    ctx.pending_dnd_updates[EMAIL] = False
    ctx.scheduled_dnd_tasks[EMAIL] = MagicMock()
    with patch(f"{_MOD}.update_dnd_state", new=AsyncMock()) as upd:
        await schedule_update_dnd_state(ctx, EMAIL)

    upd.assert_not_called()
    assert EMAIL not in ctx.scheduled_dnd_tasks


async def test_schedule_executes_update_when_due():
    """With no prior run there is no cooldown, so the forced update fires."""
    login = _login()
    ctx, _ = _ctx(account={"login_obj": login})
    ctx.pending_dnd_updates[EMAIL] = True
    with (
        patch(f"{_MOD}.update_dnd_state", new=AsyncMock()) as upd,
        patch(f"{_MOD}.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await schedule_update_dnd_state(ctx, EMAIL)

    upd.assert_awaited_once_with(login, ctx)
    sleep.assert_not_called()
    assert ctx.pending_dnd_updates[EMAIL] is False


async def test_schedule_skips_when_login_missing():
    """A cleared ``login_obj`` aborts the forced update after clearing pending."""
    ctx, _ = _ctx(account={})  # no login_obj
    ctx.pending_dnd_updates[EMAIL] = True
    with patch(f"{_MOD}.update_dnd_state", new=AsyncMock()) as upd:
        await schedule_update_dnd_state(ctx, EMAIL)

    upd.assert_not_called()
    assert ctx.pending_dnd_updates[EMAIL] is False


async def test_schedule_aborts_when_pending_cleared_during_sleep():
    """If the pending flag is cleared while sleeping, the worker bails out."""
    ctx, _ = _ctx(account={"login_obj": _login()})
    ctx.pending_dnd_updates[EMAIL] = True
    # A recent run forces a positive ``remaining`` and therefore a sleep.
    ctx.last_dnd_update_times[EMAIL] = datetime.now(UTC)
    ctx.scheduled_dnd_tasks[EMAIL] = MagicMock()

    def fake_sleep(*_a, **_k):
        ctx.pending_dnd_updates[EMAIL] = False

    with (
        patch(f"{_MOD}.update_dnd_state", new=AsyncMock()) as upd,
        patch(f"{_MOD}.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)) as sleep,
    ):
        await schedule_update_dnd_state(ctx, EMAIL)

    sleep.assert_awaited_once()
    upd.assert_not_called()
    assert EMAIL not in ctx.scheduled_dnd_tasks


async def test_schedule_loops_when_still_cooling_down():
    """Still inside the cooldown after sleeping -> ``continue`` re-evaluates."""
    ctx, _ = _ctx(account={"login_obj": _login()})
    ctx.pending_dnd_updates[EMAIL] = True
    ctx.last_dnd_update_times[EMAIL] = datetime.now(UTC)
    ctx.scheduled_dnd_tasks[EMAIL] = MagicMock()

    state = {"sleeps": 0}

    def fake_sleep(*_a, **_k):
        # First sleep leaves us still cooling (-> continue); the second clears
        # the pending flag so the loop exits on the next iteration.
        state["sleeps"] += 1
        if state["sleeps"] >= 2:
            ctx.pending_dnd_updates[EMAIL] = False

    with (
        patch(f"{_MOD}.update_dnd_state", new=AsyncMock()) as upd,
        patch(f"{_MOD}.asyncio.sleep", new=AsyncMock(side_effect=fake_sleep)),
    ):
        await schedule_update_dnd_state(ctx, EMAIL)

    # Two sleeps proves the worker looped through the ``continue`` branch.
    assert state["sleeps"] >= 2
    upd.assert_not_called()
    assert EMAIL not in ctx.scheduled_dnd_tasks


async def test_schedule_propagates_cancellation():
    """Cancellation is logged, re-raised, and the matching task is cleared."""
    ctx, _ = _ctx()
    ctx.pending_dnd_updates[EMAIL] = True
    ctx.last_dnd_update_times[EMAIL] = datetime.now(UTC)  # forces a sleep
    # Store *this* running task so the ``finally`` block's identity check
    # (task is current_task) is True and pops the pointer.
    ctx.scheduled_dnd_tasks[EMAIL] = asyncio.current_task()

    with (
        patch(f"{_MOD}.update_dnd_state", new=AsyncMock()) as upd,
        patch(
            f"{_MOD}.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)
        ),
        pytest.raises(asyncio.CancelledError),
    ):
        await schedule_update_dnd_state(ctx, EMAIL)

    upd.assert_not_called()
    assert EMAIL not in ctx.scheduled_dnd_tasks
