"""Tests for AlexaRuntimeData (entry.runtime_data Platinum storage)."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.alexa_media import const
from custom_components.alexa_media.runtime_data import AlexaRuntimeData


def _entry(data):
    """Build a minimal config entry exposing a real ``.data`` mapping."""
    return SimpleNamespace(data=data)


# ---------------------------------------------------------------------------
# Defaults / factory fields
# ---------------------------------------------------------------------------


def test_defaults_initialize_factory_fields():
    rd = AlexaRuntimeData()

    # No config_entry -> __post_init__ leaves options untouched.
    assert rd.options == {}
    # No login_obj -> email/url fall back to empty strings.
    assert rd.email == ""
    assert rd.url == ""

    # default_factory mutable fields are independent fresh instances.
    assert rd.http2_commands == {}
    assert rd.http2_activity == {"serials": {}, "refreshed": {}}
    assert rd.excluded == {}
    assert rd.notifications == {}
    assert rd.notifications_pending == set()
    assert rd.listeners == []

    assert set(rd.devices) == {
        "media_player",
        "switch",
        "guard",
        "light",
        "binary_sensor",
        "temperature",
        "smart_switch",
    }
    assert set(rd.entities) == {
        "media_player",
        "switch",
        "sensor",
        "light",
        "binary_sensor",
        "alarm_control_panel",
        "smart_switch",
    }

    # Scalar state defaults.
    assert rd.new_devices is True
    assert rd.should_get_network is True
    assert rd.second_account_index == 0
    assert rd.http2_error == 0
    assert isinstance(rd.last_called_probe_lock, asyncio.Lock)


def test_default_factory_fields_are_not_shared_between_instances():
    first = AlexaRuntimeData()
    second = AlexaRuntimeData()

    first.devices["media_player"]["SER1"] = {"x": 1}
    first.listeners.append(MagicMock())

    # Mutating one instance must not leak into another.
    assert second.devices["media_player"] == {}
    assert second.listeners == []


# ---------------------------------------------------------------------------
# __post_init__ options mirroring
# ---------------------------------------------------------------------------


def test_post_init_uses_defaults_for_missing_config_keys():
    rd = AlexaRuntimeData(config_entry=_entry({}))

    assert rd.options == {
        const.CONF_INCLUDE_DEVICES: "",
        const.CONF_EXCLUDE_DEVICES: "",
        const.CONF_QUEUE_DELAY: const.DEFAULT_QUEUE_DELAY,
        const.CONF_SCAN_INTERVAL: const.DEFAULT_SCAN_INTERVAL,
        const.CONF_PUBLIC_URL: const.DEFAULT_PUBLIC_URL,
        const.CONF_EXTENDED_ENTITY_DISCOVERY: const.DEFAULT_EXTENDED_ENTITY_DISCOVERY,
        const.CONF_DEBUG: False,
    }


def test_post_init_mirrors_provided_config_values():
    data = {
        const.CONF_INCLUDE_DEVICES: "Echo",
        const.CONF_EXCLUDE_DEVICES: "Dot",
        const.CONF_QUEUE_DELAY: 3.0,
        const.CONF_SCAN_INTERVAL: 120,
        const.CONF_PUBLIC_URL: "https://example.com",
        const.CONF_EXTENDED_ENTITY_DISCOVERY: True,
        const.CONF_DEBUG: True,
    }
    rd = AlexaRuntimeData(config_entry=_entry(data))

    assert rd.options == data


# ---------------------------------------------------------------------------
# email / url properties
# ---------------------------------------------------------------------------


def test_email_and_url_read_from_login_obj():
    login = SimpleNamespace(email="user@example.com", url="amazon.com")
    rd = AlexaRuntimeData(login_obj=login)

    assert rd.email == "user@example.com"
    assert rd.url == "amazon.com"


# ---------------------------------------------------------------------------
# get_device
# ---------------------------------------------------------------------------


def test_get_device_dict_branch_hit_miss_and_unknown_type():
    rd = AlexaRuntimeData()
    rd.devices["media_player"] = {"SER1": {"name": "Kitchen"}}

    assert rd.get_device("media_player", "SER1") == {"name": "Kitchen"}
    assert rd.get_device("media_player", "NOPE") is None
    # Unknown device_type falls back to the {} default -> None.
    assert rd.get_device("does_not_exist", "SER1") is None


def test_get_device_list_branch_matches_dict_and_object_elements():
    rd = AlexaRuntimeData()
    dict_device = {"serialNumber": "G1", "v": 1}
    obj_device = SimpleNamespace(serialNumber="G2")
    rd.devices["guard"] = [dict_device, obj_device]

    assert rd.get_device("guard", "G1") is dict_device
    assert rd.get_device("guard", "G2") is obj_device
    assert rd.get_device("guard", "MISSING") is None


# ---------------------------------------------------------------------------
# get_entity
# ---------------------------------------------------------------------------


def test_get_entity_dict_branch_hit_and_miss():
    rd = AlexaRuntimeData()
    rd.entities["media_player"] = {"key1": "entity1"}

    assert rd.get_entity("media_player", "key1") == "entity1"
    assert rd.get_entity("media_player", "absent") is None


def test_get_entity_list_branch_matches_unique_id_then_serial():
    rd = AlexaRuntimeData()
    by_unique_id = SimpleNamespace(unique_id="U1")
    by_serial = SimpleNamespace(serial="S1")
    rd.entities["light"] = [by_unique_id, by_serial]

    assert rd.get_entity("light", "U1") is by_unique_id
    assert rd.get_entity("light", "S1") is by_serial
    assert rd.get_entity("light", "MISSING") is None


# ---------------------------------------------------------------------------
# add_listener
# ---------------------------------------------------------------------------


def test_add_listener_appends_to_listeners():
    rd = AlexaRuntimeData()
    unsub = MagicMock()

    rd.add_listener(unsub)

    assert rd.listeners == [unsub]
