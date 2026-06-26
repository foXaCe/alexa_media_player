"""Tests for the async setup/ helpers: context, bluetooth, dnd, notifications."""

import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.setup.bluetooth import update_bluetooth_state
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.dnd import update_dnd_state
from custom_components.alexa_media.setup.notifications import process_notifications


def _ctx(email="user@example.com", account=None):
    hass = MagicMock()
    hass.data = {
        DATA_ALEXAMEDIA: {"accounts": {email: account if account is not None else {}}}
    }
    ctx = SetupContext(hass=hass, config_entry=MagicMock(), email=email)
    return ctx, hass


def _login(email="user@example.com"):
    login = MagicMock()
    login.email = email
    return login


# ---------------------------------------------------------------------------
# SetupContext
# ---------------------------------------------------------------------------


def test_setup_context_config_property_and_defaults():
    entry = MagicMock()
    entry.data = {"email": "a@b.com", "scan_interval": 60}
    ctx = SetupContext(hass=MagicMock(), config_entry=entry, email="a@b.com")
    assert ctx.config == {"email": "a@b.com", "scan_interval": 60}
    # Per-invocation DND state is freshly initialised.
    assert ctx.last_dnd_update_times == {}
    assert ctx.pending_dnd_updates == {}
    assert ctx.scheduled_dnd_tasks == {}
    assert ctx.scan_interval == 60.0


# ---------------------------------------------------------------------------
# bluetooth.update_bluetooth_state
# ---------------------------------------------------------------------------


async def test_update_bluetooth_state_updates_matching_device():
    email = "user@example.com"
    account = {"devices": {"media_player": {"SER1": {}}}}
    ctx, hass = _ctx(email, account)
    bt = {"bluetoothStates": [{"deviceSerialNumber": "SER1", "connected": True}]}
    with patch("custom_components.alexa_media.setup.bluetooth.AlexaAPI") as api:
        api.get_bluetooth = AsyncMock(return_value=bt)
        result = await update_bluetooth_state(_login(email), ctx, "SER1")
    device = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"]["media_player"][
        "SER1"
    ]
    assert device["bluetooth_state"] == {
        "deviceSerialNumber": "SER1",
        "connected": True,
    }
    assert result == {"deviceSerialNumber": "SER1", "connected": True}


async def test_update_bluetooth_state_returns_none_without_match():
    email = "user@example.com"
    account = {"devices": {"media_player": {"SER1": {}}}}
    ctx, _ = _ctx(email, account)
    with patch("custom_components.alexa_media.setup.bluetooth.AlexaAPI") as api:
        api.get_bluetooth = AsyncMock(return_value={"bluetoothStates": []})
        result = await update_bluetooth_state(_login(email), ctx, "SER1")
    assert result is None


# ---------------------------------------------------------------------------
# dnd.update_dnd_state
# ---------------------------------------------------------------------------


async def test_update_dnd_state_dispatches_on_success():
    email = "user@example.com"
    ctx, hass = _ctx(email)
    dnd = {
        "doNotDisturbDeviceStatusList": [
            {"deviceSerialNumber": "SER1", "enabled": True}
        ]
    }
    with (
        patch("custom_components.alexa_media.setup.dnd.AlexaAPI") as api,
        patch(
            "custom_components.alexa_media.setup.dnd.async_dispatcher_send"
        ) as dispatch,
    ):
        api.get_dnd_state = AsyncMock(return_value=dnd)
        await update_dnd_state(_login(email), ctx)
    assert dispatch.called
    # async_dispatcher_send(hass, signal, payload)
    assert dispatch.call_args.args[2] == {
        "dnd_update": [{"deviceSerialNumber": "SER1", "enabled": True}]
    }
    assert email in ctx.last_dnd_update_times


async def test_update_dnd_state_no_dispatch_when_api_returns_none():
    email = "user@example.com"
    ctx, _ = _ctx(email)
    with (
        patch("custom_components.alexa_media.setup.dnd.AlexaAPI") as api,
        patch(
            "custom_components.alexa_media.setup.dnd.async_dispatcher_send"
        ) as dispatch,
    ):
        api.get_dnd_state = AsyncMock(return_value=None)
        await update_dnd_state(_login(email), ctx)
    assert not dispatch.called


# ---------------------------------------------------------------------------
# notifications.process_notifications
# ---------------------------------------------------------------------------


async def test_process_notifications_stores_and_dispatches():
    email = "user@example.com"
    account = {"notifications": {}, "last_notif_poll": 0.0}
    ctx, hass = _ctx(email, account)
    raw = [
        {
            "deviceSerialNumber": "DEV1",
            "type": "Timer",
            "notificationIndex": "n1",
            "status": "ON",
        },
        # notifications with no device are skipped
        {"type": "Timer", "notificationIndex": "n2"},
    ]
    with patch(
        "custom_components.alexa_media.setup.notifications.async_dispatcher_send"
    ) as dispatch:
        result = await process_notifications(_login(email), ctx, raw_notifications=raw)
    assert result is True
    notifs = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["notifications"]
    assert notifs["DEV1"]["Timer"]["n1"]["status"] == "ON"
    assert dispatch.called


async def test_process_notifications_skips_on_cooldown():
    email = "user@example.com"
    account = {"notifications": {}, "last_notif_poll": _time.time()}
    ctx, _ = _ctx(email, account)
    # raw_notifications=None + a fresh poll -> cooldown -> skip, no API call.
    result = await process_notifications(_login(email), ctx)
    assert result is False
