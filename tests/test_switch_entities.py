"""Tests for switch.py entities (AlexaMediaSwitch family + SmartSwitch + unload)."""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.const import CONF_EMAIL
from homeassistant.helpers.entity import EntityCategory

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.switch import (
    AlexaMediaSwitch,
    DNDSwitch,
    RepeatSwitch,
    ShuffleSwitch,
    SmartSwitch,
    async_unload_entry,
)

_POWER = "custom_components.alexa_media.switch.parse_power_from_coordinator"
_API = "custom_components.alexa_media.switch.AlexaAPI"
_EMAIL = "test@example.com"


def _client(*, value=True, available=True, assumed=False, unique_id="SN1"):
    client = MagicMock()
    client._login = MagicMock()
    client._login.email = _EMAIL
    client.unique_id = unique_id
    client.device_serial_number = unique_id
    client.available = available
    client.assumed_state = assumed
    client.dnd_state = value
    client.shuffle = value
    client.repeat_state = value
    return client


# --------------------------------------------------------------------------- #
# AlexaMediaSwitch family - properties
# --------------------------------------------------------------------------- #


def test_dnd_switch_properties_on():
    switch = DNDSwitch(_client(value=True))
    assert switch.unique_id == "SN1_do not disturb"
    assert switch.device_class == "switch"
    assert switch.is_on is True
    assert switch.available is True
    assert switch.hidden is False
    assert switch.should_poll is True
    assert switch.assumed_state is False
    assert switch.icon == "mdi:minus-circle"
    assert switch.entity_category == EntityCategory.CONFIG
    assert switch.device_info["identifiers"] == {("alexa_media", "SN1")}


def test_dnd_switch_off_icon():
    switch = DNDSwitch(_client(value=False))
    assert switch.is_on is False
    assert switch.icon == "mdi:minus-circle-off"


def test_switch_unavailable_when_property_none():
    client = _client()
    client.dnd_state = None
    switch = DNDSwitch(client)
    assert switch.available is False
    assert switch.hidden is True


def test_shuffle_switch_properties():
    switch = ShuffleSwitch(_client(value=True))
    assert switch.unique_id == "SN1_shuffle"
    assert switch.icon == "mdi:shuffle"
    assert switch.entity_category == EntityCategory.CONFIG


def test_repeat_switch_properties():
    switch = RepeatSwitch(_client(value=True))
    assert switch.unique_id == "SN1_repeat"
    assert switch.icon == "mdi:repeat"
    switch_off = RepeatSwitch(_client(value=False))
    assert switch_off.icon == "mdi:repeat-off"


# --------------------------------------------------------------------------- #
# _set_switch / turn_on / turn_off
# --------------------------------------------------------------------------- #


@patch.object(AlexaMediaSwitch, "name", new_callable=PropertyMock, return_value="DND")
async def test_set_switch_success_updates_client(_mock_name):
    client = _client(value=False)
    switch = DNDSwitch(client)
    switch.alexa_api = MagicMock()
    switch.alexa_api.set_dnd_state = AsyncMock(return_value=True)
    switch.schedule_update_ha_state = MagicMock()
    await switch._set_switch(True)
    switch.alexa_api.set_dnd_state.assert_awaited_once_with(True)
    assert client.dnd_state is True
    switch.schedule_update_ha_state.assert_called_once()


@patch.object(AlexaMediaSwitch, "name", new_callable=PropertyMock, return_value="DND")
async def test_set_switch_failure_triggers_client_update(_mock_name):
    client = _client(value=False)
    client.async_update = AsyncMock()
    switch = DNDSwitch(client)
    switch.alexa_api = MagicMock()
    switch.alexa_api.set_dnd_state = AsyncMock(return_value=False)
    await switch._set_switch(True)
    client.async_update.assert_awaited_once()


async def test_turn_on_off_delegate_to_set_switch():
    switch = DNDSwitch(_client())
    switch._set_switch = AsyncMock()
    await switch.async_turn_on()
    await switch.async_turn_off()
    assert switch._set_switch.await_args_list[0].args == (True,)
    assert switch._set_switch.await_args_list[1].args == (False,)


def test_dnd_handle_event_updates_state():
    client = _client(value=False)
    switch = DNDSwitch(client)
    switch.schedule_update_ha_state = MagicMock()
    switch._handle_event(
        {"dnd_update": [{"deviceSerialNumber": "SN1", "enabled": True}]}
    )
    assert client.dnd_state is True
    switch.schedule_update_ha_state.assert_called_once()


def test_dnd_handle_event_ignores_other_serial():
    client = _client(value=False)
    switch = DNDSwitch(client)
    switch.schedule_update_ha_state = MagicMock()
    switch._handle_event(
        {"dnd_update": [{"deviceSerialNumber": "OTHER", "enabled": True}]}
    )
    switch.schedule_update_ha_state.assert_not_called()


# --------------------------------------------------------------------------- #
# SmartSwitch
# --------------------------------------------------------------------------- #


def _smart(details=None, login=None):
    return SmartSwitch(
        MagicMock(), login or MagicMock(), details or {"id": "sw1", "name": "Plug"}
    )


def test_smart_switch_basic_properties():
    switch = _smart()
    assert switch.name == "Plug"
    assert switch.unique_id == "sw1"


@patch(_POWER)
def test_smart_switch_is_on(mock_power):
    switch = _smart()
    mock_power.return_value = "ON"
    assert switch.is_on is True
    mock_power.return_value = "OFF"
    assert switch.is_on is False
    mock_power.return_value = None
    assert switch.is_on is False
    switch._requested_power = True
    assert switch.is_on is True


def test_smart_switch_assumed_state():
    coordinator = MagicMock()
    switch = SmartSwitch(coordinator, MagicMock(), {"id": "sw1", "name": "Plug"})
    coordinator.data = {"sw1": {}}
    assert switch.assumed_state is False
    coordinator.data = {}
    assert switch.assumed_state is True


def _smart_with_hass():
    login = MagicMock()
    login.email = _EMAIL
    switch = SmartSwitch(MagicMock(), login, {"id": "sw1", "name": "Plug"})
    switch.hass = MagicMock()
    debouncer = MagicMock()
    debouncer.async_call = AsyncMock()
    switch.hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {_EMAIL: {"confirm_refresh_debouncer": debouncer}}
        }
    }
    switch.schedule_update_ha_state = MagicMock()
    switch.coordinator.async_request_refresh = AsyncMock()
    return switch, debouncer


@patch(_API)
async def test_smart_switch_set_state_success(mock_api):
    mock_api.set_light_state = AsyncMock(
        return_value={"controlResponses": [{"code": "SUCCESS"}]}
    )
    switch, debouncer = _smart_with_hass()
    await switch._set_state(True)
    assert switch._requested_power is True
    switch.schedule_update_ha_state.assert_called_once()
    debouncer.async_call.assert_awaited_once()


@patch(_API)
async def test_smart_switch_set_state_non_dict_refreshes(mock_api):
    mock_api.set_light_state = AsyncMock(return_value=None)
    switch, _debouncer = _smart_with_hass()
    await switch._set_state(True)
    switch.coordinator.async_request_refresh.assert_awaited_once()


@patch(_API)
async def test_smart_switch_set_state_failed_response_refreshes(mock_api):
    mock_api.set_light_state = AsyncMock(
        return_value={"controlResponses": [{"code": "FAILURE"}]}
    )
    switch, _debouncer = _smart_with_hass()
    await switch._set_state(True)
    switch.coordinator.async_request_refresh.assert_awaited_once()


async def test_smart_switch_turn_on_off():
    switch = _smart()
    switch._set_state = AsyncMock()
    await switch.async_turn_on()
    await switch.async_turn_off()
    assert switch._set_state.await_args_list[0].args == (True,)
    assert switch._set_state.await_args_list[1].args == (False,)


# --------------------------------------------------------------------------- #
# async_unload_entry
# --------------------------------------------------------------------------- #


async def test_unload_entry_removes_switches():
    device = AsyncMock()
    # entities["switch"] is keyed twice by the same key: {key: {key: {id: device}}}
    account = {"entities": {"switch": {"dnd": {"dnd": {"SN1": device}}}}}
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}
    entry = MagicMock()
    entry.data = {CONF_EMAIL: _EMAIL}
    result = await async_unload_entry(hass, entry)
    assert result is True
    device.async_remove.assert_awaited_once()
