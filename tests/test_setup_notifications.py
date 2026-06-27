"""Tests for the notification scheduler/worker in setup.notifications."""

from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.notifications import (
    run_notifications_refresh,
    schedule_notifications_refresh,
)

EMAIL = "user@example.com"
_MOD = "custom_components.alexa_media.setup.notifications"


def _ctx(account):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}}
    return SetupContext(hass=hass, config_entry=MagicMock(), email=EMAIL), hass


def test_schedule_notifications_refresh_marks_pending_and_starts_worker():
    account = {"notifications_pending": set(), "notifications_refresh_task": None}
    ctx, hass = _ctx(account)
    with patch(f"{_MOD}.run_notifications_refresh", return_value=MagicMock()):
        schedule_notifications_refresh(ctx, device_serial="SER1", reason="push")
    assert "SER1" in account["notifications_pending"]
    assert hass.async_create_task.called


def test_schedule_notifications_refresh_reuses_running_worker():
    running = MagicMock()
    running.done.return_value = False
    account = {"notifications_pending": set(), "notifications_refresh_task": running}
    ctx, hass = _ctx(account)
    with patch(f"{_MOD}.run_notifications_refresh", return_value=MagicMock()):
        schedule_notifications_refresh(ctx, device_serial="SER2")
    # A worker is already running -> do not spawn another.
    assert "SER2" in account["notifications_pending"]
    assert not hass.async_create_task.called


async def test_run_notifications_refresh_processes_and_clears_pending():
    login = MagicMock()
    account = {
        "notifications_pending": {"SER1"},
        "login_obj": login,
        "notifications_retry_count": 0,
        "notifications_refresh_task": MagicMock(),
    }
    ctx, _ = _ctx(account)
    with (
        patch(f"{_MOD}.AlexaAPI") as api,
        patch(f"{_MOD}.process_notifications", new=AsyncMock()) as proc,
    ):
        api.get_notifications = AsyncMock(return_value=[{"x": 1}])
        await run_notifications_refresh(ctx)
    proc.assert_awaited_once()
    assert account["notifications_pending"] == set()
    assert account["notifications_retry_count"] == 0
    # The worker always clears its task pointer on exit.
    assert account["notifications_refresh_task"] is None


async def test_run_notifications_refresh_noop_without_login():
    account = {"notifications_pending": {"SER1"}, "login_obj": None}
    ctx, _ = _ctx(account)
    with patch(f"{_MOD}.AlexaAPI") as api:
        api.get_notifications = AsyncMock()
        await run_notifications_refresh(ctx)
    api.get_notifications.assert_not_called()
