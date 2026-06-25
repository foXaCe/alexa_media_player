"""Tests for sensor.py: TemperatureSensor, AirQualitySensor, helpers and unload."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import CONF_EMAIL, UnitOfTemperature
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.sensor import (
    AirQualitySensor,
    TemperatureSensor,
    async_unload_entry,
    lookup_device_info,
)

_TEMP = "custom_components.alexa_media.sensor.parse_temperature_from_coordinator"
_AQ = "custom_components.alexa_media.sensor.parse_air_quality_from_coordinator"
_EMAIL = "test@example.com"
_IDENT = ("alexa_media", "SN1")


# --------------------------------------------------------------------------- #
# lookup_device_info
# --------------------------------------------------------------------------- #


def test_lookup_device_info_found():
    mediaplayer = MagicMock()
    mediaplayer.device_info = {"identifiers": {_IDENT}}
    account = {"entities": {"media_player": {"SN1": mediaplayer}}}
    assert lookup_device_info(account, "SN1") == _IDENT


def test_lookup_device_info_not_found():
    account = {"entities": {"media_player": {}}}
    assert lookup_device_info(account, "SN1") is None


# --------------------------------------------------------------------------- #
# TemperatureSensor
# --------------------------------------------------------------------------- #


def _temp(value_scale, **kwargs):
    with patch(_TEMP, return_value=value_scale):
        return TemperatureSensor(MagicMock(), "ent1", "Echo Temp", _IDENT, **kwargs)


def test_temperature_sensor_init():
    sensor = _temp({"value": 21.5, "scale": "CELSIUS"})
    assert sensor._attr_unique_id == "ent1_temperature"
    assert sensor.native_value == 21.5
    assert sensor.native_unit_of_measurement == UnitOfTemperature.CELSIUS
    assert sensor.device_class == SensorDeviceClass.TEMPERATURE
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.device_info["identifiers"] == {_IDENT}


def test_temperature_sensor_with_serial_exposes_rich_device():
    sensor = _temp({"value": 20, "scale": "FAHRENHEIT"}, device_serial="HW1")
    assert sensor.native_unit_of_measurement == UnitOfTemperature.FAHRENHEIT
    assert sensor.device_info["serial_number"] == "HW1"
    assert sensor.device_info["model"] == "Indoor Air Quality Monitor"


def test_temperature_sensor_without_ident_has_no_device():
    with patch(_TEMP, return_value={"value": 1, "scale": "KELVIN"}):
        sensor = TemperatureSensor(MagicMock(), "ent1", "X", None)
    assert sensor._attr_device_info is None
    assert sensor.native_unit_of_measurement == UnitOfTemperature.KELVIN


def test_temperature_value_and_scale_helpers():
    sensor = _temp({"value": 10, "scale": "CELSIUS"})
    assert sensor._get_temperature_value({"value": 5}) == 5
    assert sensor._get_temperature_value(None) is None
    assert sensor._get_temperature_value({}) is None
    assert (
        sensor._get_temperature_scale({"scale": "KELVIN"}) == UnitOfTemperature.KELVIN
    )
    assert sensor._get_temperature_scale({"scale": "BOGUS"}) is None
    assert sensor._get_temperature_scale(None) is None


def test_temperature_handle_coordinator_update():
    sensor = _temp({"value": 18, "scale": "CELSIUS"})
    with (
        patch(_TEMP, return_value={"value": 25, "scale": "CELSIUS"}),
        patch.object(CoordinatorEntity, "_handle_coordinator_update"),
    ):
        sensor._handle_coordinator_update()
    assert sensor.native_value == 25


# --------------------------------------------------------------------------- #
# AirQualitySensor
# --------------------------------------------------------------------------- #


def _aq(sensor_name, value=42, unit="MICROGRAMS_PER_CUBIC_METER", **kwargs):
    with patch(_AQ, return_value=value):
        return AirQualitySensor(
            MagicMock(),
            "ent1",
            "AQM",
            _IDENT,
            sensor_name,
            "inst1",
            unit,
            **kwargs,
        )


def test_air_quality_sensor_known_type():
    sensor = _aq("Alexa.AirQuality.ParticulateMatter", device_serial="HW1")
    assert sensor._attr_translation_key == "air_quality_particulate_matter"
    assert sensor.native_value == 42
    assert sensor._attr_unique_id == "ent1_particulate_matter"
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.device_info["serial_number"] == "HW1"


def test_air_quality_sensor_unknown_type_defaults():
    sensor = _aq("Alexa.AirQuality.Unknown")
    assert sensor._attr_translation_key == "air_quality"
    assert sensor._attr_unique_id == "ent1_unknown"
    assert sensor._attr_device_info["identifiers"] == {_IDENT}


def test_air_quality_handle_coordinator_update():
    sensor = _aq("Alexa.AirQuality.Humidity", value=40)
    with (
        patch(_AQ, return_value=55),
        patch.object(CoordinatorEntity, "_handle_coordinator_update"),
    ):
        sensor._handle_coordinator_update()
    assert sensor.native_value == 55


# --------------------------------------------------------------------------- #
# async_unload_entry (plain + nested air-quality)
# --------------------------------------------------------------------------- #


async def test_unload_entry_removes_plain_and_nested_sensors():
    plain = AsyncMock()
    nested = AsyncMock()
    account = {"entities": {"sensor": {"k1": {"s1": plain, "s2": {"n1": nested}}}}}
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}
    entry = MagicMock()
    entry.data = {CONF_EMAIL: _EMAIL}
    result = await async_unload_entry(hass, entry)
    assert result is True
    plain.async_remove.assert_awaited_once()
    nested.async_remove.assert_awaited_once()
    # empty bucket pruned
    assert account["entities"]["sensor"] == {}
