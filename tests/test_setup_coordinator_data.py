"""Tests for setup.coordinator_data.async_update_data guards + error handling."""

from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyConnectionError, AlexapyLoginError
from homeassistant.const import CONF_EMAIL
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.alexa_media.const import (
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_EXTENDED_ENTITY_DISCOVERY,
    CONF_INCLUDE_DEVICES,
    DATA_ALEXAMEDIA,
)
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.coordinator_data import async_update_data

_MOD = "custom_components.alexa_media.setup.coordinator_data"
EMAIL = "user@example.com"


def _ctx(accounts):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": accounts}}
    entry = MagicMock()
    entry.data = {CONF_EMAIL: EMAIL, CONF_INCLUDE_DEVICES: "", CONF_EXCLUDE_DEVICES: ""}
    entry.entry_id = "entry1"
    return SetupContext(hass=hass, config_entry=entry, email=EMAIL, metrics=None), hass


def _login(network_ok=True):
    login = MagicMock()
    login.email = EMAIL
    login.close_requested = not network_ok
    login.session.closed = False
    login.status = {"login_successful": True}
    return login


def _full_account(login):
    return {
        "login_obj": login,
        "entities": {
            "media_player": {},
            "sensor": {},
            "light": [],
            "binary_sensor": [],
            "alarm_control_panel": {},
            "smart_switch": [],
        },
        "auth_info": None,
        "new_devices": True,
        "should_get_network": False,
        "first_run": False,
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: False, CONF_DEBUG: False},
    }


def _patch_api(get_devices_mock):
    """Patch the serial helpers and AlexaAPI; get_devices uses the given mock."""
    api = MagicMock()
    api.get_devices = get_devices_mock
    api.get_bluetooth = AsyncMock(return_value={})
    api.get_device_preferences = AsyncMock(return_value={})
    api.get_dnd_state = AsyncMock(return_value={})
    api.get_authentication = AsyncMock(return_value={})
    return (
        patch(f"{_MOD}._existing_serials", return_value=set()),
        patch(f"{_MOD}._entity_backed_serials", return_value=set()),
        patch(f"{_MOD}.AlexaAPI", api),
    )


# --- guards -----------------------------------------------------------------


async def test_returns_none_when_account_missing():
    ctx, _ = _ctx({})
    assert await async_update_data(ctx) is None


async def test_returns_none_when_login_obj_missing():
    ctx, _ = _ctx({EMAIL: {}})
    assert await async_update_data(ctx) is None


async def test_returns_none_when_network_not_allowed():
    ctx, _ = _ctx({EMAIL: {"login_obj": _login(network_ok=False)}})
    assert await async_update_data(ctx) is None


# --- error handling ---------------------------------------------------------


async def test_raises_update_failed_on_connection_error():
    ctx, _ = _ctx({EMAIL: _full_account(_login())})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=AlexapyConnectionError("boom")))
    with p1, p2, p3, pytest.raises(UpdateFailed):
        await async_update_data(ctx)


async def test_login_error_fires_relogin_and_returns_none():
    ctx, hass = _ctx({EMAIL: _full_account(_login())})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=AlexapyLoginError("bad")))
    with p1, p2, p3:
        result = await async_update_data(ctx)
    assert result is None
    fired_events = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" in fired_events
