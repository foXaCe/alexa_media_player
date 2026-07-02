"""Tests for light.py - color/brightness helpers, AlexaLight and platform setup."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
)
from homeassistant.const import CONF_EMAIL
from homeassistant.exceptions import ConfigEntryNotReady
import pytest

from custom_components.alexa_media.const import (
    CONF_EXTENDED_ENTITY_DISCOVERY,
    DATA_ALEXAMEDIA,
)
from custom_components.alexa_media.light import (
    ALEXA_COLORS,
    AlexaLight,
    alexa_brightness_to_ha,
    alexa_color_name_to_rgb,
    async_setup_entry,
    async_setup_platform,
    color_modes,
    ha_brightness_to_alexa,
    hs_to_alexa_color,
    hsb_to_alexa_color,
    kelvin_to_alexa,
    red_mean,
    rgb_to_alexa_color,
)

_POWER = "custom_components.alexa_media.light.parse_power_from_coordinator"
_BRIGHT = "custom_components.alexa_media.light.parse_brightness_from_coordinator"
_CTEMP = "custom_components.alexa_media.light.parse_color_temp_from_coordinator"
_COLOR = "custom_components.alexa_media.light.parse_color_from_coordinator"
_ADD = "custom_components.alexa_media.light.add_devices"
_API = "custom_components.alexa_media.light.AlexaAPI"


def _details(color=True, color_temperature=True, brightness=True, is_hue_v1=False):
    return {
        "id": "light-1",
        "name": "Lamp",
        "color": color,
        "color_temperature": color_temperature,
        "brightness": brightness,
        "is_hue_v1": is_hue_v1,
    }


def _light(details=None, login=None):
    coordinator = MagicMock()
    light = AlexaLight(coordinator, login or MagicMock(), details or _details())
    return light, coordinator


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_color_modes_combinations():
    assert color_modes(
        {"color": True, "color_temperature": True, "brightness": True}
    ) == [
        ColorMode.HS,
        ColorMode.COLOR_TEMP,
    ]
    assert color_modes(
        {"color": True, "color_temperature": False, "brightness": True}
    ) == [ColorMode.HS]
    assert color_modes(
        {"color": False, "color_temperature": True, "brightness": True}
    ) == [ColorMode.COLOR_TEMP]
    assert color_modes(
        {"color": False, "color_temperature": False, "brightness": True}
    ) == [ColorMode.BRIGHTNESS]
    assert color_modes(
        {"color": False, "color_temperature": False, "brightness": False}
    ) == [ColorMode.ONOFF]


@pytest.mark.parametrize(
    ("kelvin", "expected"),
    [
        (None, (None, None)),
        (2200, (2200, "warm_white")),
        (3000, (2700, "soft_white")),
        (4000, (4000, "white")),
        (5500, (5400, "daylight_white")),
        (6500, (6500, "cool_white")),
    ],
)
def test_kelvin_to_alexa(kelvin, expected):
    assert kelvin_to_alexa(kelvin) == expected


def test_brightness_conversions():
    assert ha_brightness_to_alexa(None) is None
    assert ha_brightness_to_alexa(255) == pytest.approx(100)
    assert alexa_brightness_to_ha(None) is None
    assert alexa_brightness_to_ha(100) == pytest.approx(255)


def test_red_mean_identity_is_zero():
    assert red_mean((10, 20, 30), (10, 20, 30)) == 0


def test_alexa_color_name_to_rgb_known_color():
    assert alexa_color_name_to_rgb("red") == (255, 0, 0)


def test_rgb_to_alexa_color_returns_known_name():
    _hs, name = rgb_to_alexa_color((255, 0, 0))
    assert name in ALEXA_COLORS
    assert name == "red"


def test_hs_to_alexa_color_none():
    assert hs_to_alexa_color(None) == (None, None)


def test_hs_to_alexa_color_value():
    hs, name = hs_to_alexa_color((0, 100))  # pure red
    assert name == "red"
    assert hs is not None


def test_hsb_to_alexa_color_none_and_value():
    assert hsb_to_alexa_color(None) == (None, None)
    _hs, name = hsb_to_alexa_color((0, 1.0, 1.0))  # hue=0, sat/bri normalized
    assert name in ALEXA_COLORS


# --------------------------------------------------------------------------- #
# AlexaLight properties
# --------------------------------------------------------------------------- #


def test_light_basic_properties():
    light, _ = _light()
    # has_entity_name: entity name is None; the device carries the name, so the
    # composed friendly name is unchanged.
    assert light.has_entity_name is True
    assert light.name is None
    assert light.device_info["name"] == "Lamp"
    assert light.unique_id == "light-1"
    assert light._attr_min_color_temp_kelvin == 2200
    assert light._attr_max_color_temp_kelvin == 6500


@patch(_POWER)
def test_light_is_on(mock_power):
    light, _ = _light()
    mock_power.return_value = "ON"
    assert light.is_on is True
    mock_power.return_value = "OFF"
    assert light.is_on is False
    mock_power.return_value = None
    assert light.is_on is False  # no requested power -> False
    light._requested_power = True
    assert light.is_on is True


@patch(_BRIGHT)
def test_light_brightness(mock_bright):
    light, _ = _light()
    mock_bright.return_value = 100  # alexa 0-100
    assert light.brightness == pytest.approx(255)
    mock_bright.return_value = None
    light._requested_ha_brightness = 42
    assert light.brightness == 42


@patch(_CTEMP)
def test_light_color_temp_kelvin(mock_ctemp):
    light, _ = _light()
    mock_ctemp.return_value = 3000
    assert light.color_temp_kelvin == 2700  # rounded to Alexa value
    mock_ctemp.return_value = None
    light._requested_kelvin = 4000
    assert light.color_temp_kelvin == 4000


@patch(_COLOR)
def test_light_hs_color(mock_color):
    light, _ = _light()
    mock_color.return_value = None
    light._requested_hs = (120, 50)
    assert light.hs_color == (120, 50)


@patch(_COLOR)
def test_light_hs_color_from_coordinator(mock_color):
    light, _ = _light()
    mock_color.return_value = (0, 1.0, 1.0)  # hsb -> mapped to nearest Alexa color
    assert light.hs_color is not None


@patch(_COLOR)
def test_light_color_mode(mock_color):
    light, _ = _light(_details(color=True, color_temperature=True))
    mock_color.return_value = None
    light._requested_hs = None
    assert light.color_mode == ColorMode.COLOR_TEMP  # white -> color temp
    light._requested_hs = (120, 50)
    assert light.color_mode == ColorMode.HS
    light._requested_hs = (0, 0)
    assert light.color_mode == ColorMode.COLOR_TEMP


def test_light_color_mode_single():
    light, _ = _light(_details(color=True, color_temperature=False))
    assert light.color_mode == ColorMode.HS


def test_light_assumed_state():
    light, coord = _light()
    coord.data = {"light-1": {}}
    assert light.assumed_state is False
    coord.data = {}
    assert light.assumed_state is True


# --------------------------------------------------------------------------- #
# AlexaLight commands
# --------------------------------------------------------------------------- #


def _light_with_hass():
    light, coord = _light()
    login = light._login
    login.email = "a@example.com"
    light.hass = MagicMock()
    debouncer = MagicMock()
    debouncer.async_call = AsyncMock()
    light.hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {"a@example.com": {"confirm_refresh_debouncer": debouncer}}
        }
    }
    light.schedule_update_ha_state = MagicMock()
    coord.async_request_refresh = AsyncMock()
    return light, coord, debouncer


@patch(_API)
async def test_set_state_success(mock_api):
    mock_api.set_light_state = AsyncMock(
        return_value={"controlResponses": [{"code": "SUCCESS"}]}
    )
    light, _coord, debouncer = _light_with_hass()
    await light._set_state(True, brightness=128, kelvin=3000, hs_color=(120, 50))
    assert light._requested_power is True
    assert light._requested_kelvin == 2700  # rounded
    mock_api.set_light_state.assert_awaited_once()
    light.schedule_update_ha_state.assert_called_once()
    debouncer.async_call.assert_awaited_once()


@patch(_API)
async def test_set_state_non_dict_triggers_refresh(mock_api):
    mock_api.set_light_state = AsyncMock(return_value=None)
    light, coord, _debouncer = _light_with_hass()
    await light._set_state(True)
    coord.async_request_refresh.assert_awaited_once()


@patch(_API)
async def test_set_state_failed_control_response_refreshes(mock_api):
    mock_api.set_light_state = AsyncMock(
        return_value={"controlResponses": [{"code": "FAILURE"}]}
    )
    light, coord, _debouncer = _light_with_hass()
    await light._set_state(True)
    coord.async_request_refresh.assert_awaited_once()


@patch(_CTEMP, return_value=None)
@patch(_BRIGHT, return_value=None)
@patch(_API)
async def test_set_state_with_hs_color_sets_requested_hs(mock_api, _mb, _mc):
    mock_api.set_light_state = AsyncMock(
        return_value={"controlResponses": [{"code": "SUCCESS"}]}
    )
    light, _coord, _deb = _light_with_hass()
    await light._set_state(True, brightness=128, kelvin=None, hs_color=(0, 100))
    assert light._requested_hs is not None  # adjusted_hs branch


@patch(_COLOR, return_value=None)
@patch(_CTEMP, return_value=None)
@patch(_BRIGHT, return_value=None)
@patch(_API)
async def test_set_state_on_off_only_keeps_existing_hs(mock_api, _mb, _mc, _mcol):
    mock_api.set_light_state = AsyncMock(
        return_value={"controlResponses": [{"code": "SUCCESS"}]}
    )
    light, _coord, _deb = _light_with_hass()
    await light._set_state(True, brightness=128, kelvin=None, hs_color=None)
    # neither color nor kelvin set -> _requested_hs falls back to current hs_color
    assert light._requested_hs is None


async def test_turn_on_extracts_attributes():
    light, _ = _light()
    light._set_state = AsyncMock()
    await light.async_turn_on(
        **{ATTR_BRIGHTNESS: 100, ATTR_COLOR_TEMP_KELVIN: 4000, ATTR_HS_COLOR: (120, 50)}
    )
    light._set_state.assert_awaited_once_with(True, 100, 4000, (120, 50))


async def test_turn_off_calls_set_state_false():
    light, _ = _light()
    light._set_state = AsyncMock()
    await light.async_turn_off()
    light._set_state.assert_awaited_once_with(False)


# --------------------------------------------------------------------------- #
# Platform setup / unload
# --------------------------------------------------------------------------- #


def _account(lights, extended=True, components=None):
    return {
        "coordinator": MagicMock(),
        "login_obj": MagicMock(),
        "devices": {"light": lights},
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: extended},
        "entities": {"light": []},
    }


def _hass(account, components=None):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {"a@example.com": account}}}
    hass.config.as_dict.return_value = {"components": components or set()}
    return hass


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_creates_lights(mock_add):
    mock_add.return_value = True
    account = _account([_details()])
    hass = _hass(account)
    result = await async_setup_platform(
        hass, {CONF_EMAIL: "a@example.com"}, MagicMock()
    )
    assert result is True
    assert len(account["entities"]["light"]) == 1


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_skips_hue_v1_when_emulated(mock_add):
    mock_add.return_value = True
    account = _account([_details(is_hue_v1=True)])
    hass = _hass(account, components={"emulated_hue"})
    await async_setup_platform(hass, {CONF_EMAIL: "a@example.com"}, MagicMock())
    assert account["entities"]["light"] == []  # filtered out


async def test_setup_platform_without_account_raises():
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {}}}
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_platform(hass, {}, MagicMock(), discovery_info=None)


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_entry_delegates(mock_add):
    mock_add.return_value = True
    account = _account([], extended=True)
    hass = _hass(account)
    entry = MagicMock()
    entry.data = {CONF_EMAIL: "a@example.com"}
    result = await async_setup_entry(hass, entry, MagicMock())
    assert result is True
