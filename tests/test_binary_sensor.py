"""Tests for binary_sensor.py - AlexaContact entity and platform setup/unload."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_EMAIL
from homeassistant.exceptions import ConfigEntryNotReady
import pytest

from custom_components.alexa_media.binary_sensor import (
    AlexaContact,
    async_setup_entry,
    async_setup_platform,
    async_unload_entry,
)
from custom_components.alexa_media.const import (
    CONF_EXTENDED_ENTITY_DISCOVERY,
    DATA_ALEXAMEDIA,
)

_PARSE = (
    "custom_components.alexa_media.binary_sensor.parse_detection_state_from_coordinator"
)
_ADD = "custom_components.alexa_media.binary_sensor.add_devices"


def _make_contact(detail_id="contact-1", name="Front Door"):
    coordinator = MagicMock()
    return AlexaContact(coordinator, {"id": detail_id, "name": name}), coordinator


def _account_dict(entities=None, extended=True):
    return {
        "coordinator": MagicMock(),
        "devices": {"binary_sensor": entities if entities is not None else []},
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: extended},
        "entities": {"binary_sensor": []},
    }


def _hass_with(account, account_dict):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {account: account_dict}}}
    return hass


# --------------------------------------------------------------------------- #
# AlexaContact entity
# --------------------------------------------------------------------------- #


def test_contact_basic_properties():
    contact, _ = _make_contact()
    assert contact.name == "Front Door"
    assert contact.unique_id == "contact-1"


@patch(_PARSE)
def test_contact_is_on_states(mock_parse):
    contact, _ = _make_contact()
    mock_parse.return_value = "DETECTED"
    assert contact.is_on is True
    mock_parse.return_value = "NOT_DETECTED"
    assert contact.is_on is False
    mock_parse.return_value = None
    assert contact.is_on is None


def test_contact_assumed_state():
    contact, coordinator = _make_contact(detail_id="c1")
    coordinator.data = {"c1": {}}
    assert contact.assumed_state is False
    coordinator.data = {}
    assert contact.assumed_state is True


# --------------------------------------------------------------------------- #
# async_setup_platform / async_setup_entry
# --------------------------------------------------------------------------- #


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_creates_entities_with_extended_discovery(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _account_dict([{"id": "c1", "name": "Door"}], extended=True)
    hass = _hass_with(account, account_dict)
    result = await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert result is True
    assert len(account_dict["entities"]["binary_sensor"]) == 1
    mock_add.assert_awaited_once()


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_skips_without_extended_discovery(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _account_dict([{"id": "c1", "name": "Door"}], extended=False)
    hass = _hass_with(account, account_dict)
    result = await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert result is True
    assert account_dict["entities"]["binary_sensor"] == []


async def test_setup_platform_without_account_raises():
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {}}}
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_platform(hass, {}, MagicMock(), discovery_info=None)


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_entry_delegates_to_platform(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _account_dict(extended=True)
    hass = _hass_with(account, account_dict)
    entry = MagicMock()
    entry.data = {CONF_EMAIL: account}
    result = await async_setup_entry(hass, entry, MagicMock())
    assert result is True


# --------------------------------------------------------------------------- #
# async_unload_entry
# --------------------------------------------------------------------------- #


async def test_unload_entry_removes_entities():
    account = "a@example.com"
    sensor = AsyncMock()
    account_dict = {"entities": {"binary_sensor": [sensor]}}
    hass = _hass_with(account, account_dict)
    entry = MagicMock()
    entry.data = {CONF_EMAIL: account}
    result = await async_unload_entry(hass, entry)
    assert result is True
    sensor.async_remove.assert_awaited_once()
