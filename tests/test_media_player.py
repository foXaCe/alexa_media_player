"""Tests for media_player.py - AlexaClient properties, state, commands, unload.

Focuses on the synchronous property/getter logic and the simple transport
commands (which delegate to ``alexa_api``); the heavy ``refresh``/``async_update``
/ websocket-event paths are out of scope here.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.media_player import (
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.const import CONF_EMAIL, STATE_UNAVAILABLE
import pytest

from custom_components.alexa_media.const import CONF_QUEUE_DELAY, DATA_ALEXAMEDIA
from custom_components.alexa_media.media_player import AlexaClient, async_unload_entry

_HTTP2 = "custom_components.alexa_media.media_player.is_http2_enabled"
_EMAIL = "test@example.com"


def _make_client(serial="SN1", name="Echo", second=0):
    login = MagicMock()
    login.email = _EMAIL
    client = AlexaClient({"accountName": name, "serialNumber": serial}, login, second)
    client.alexa_api = MagicMock()
    return client


# --------------------------------------------------------------------------- #
# Identity / simple getters
# --------------------------------------------------------------------------- #


def test_name_serial_unique_id():
    client = _make_client()
    client._device_name = "Living Room"
    client._device_serial_number = "SN123"
    # has_entity_name: the entity name is None and the friendly name comes from
    # the device, so the display name is unchanged from the old `name` behaviour.
    assert client.has_entity_name is True
    assert client.name is None
    assert client.device_info["name"] == "Living Room"
    assert client.device_serial_number == "SN123"
    assert client.unique_id == "SN123"  # second_account_index == 0


def test_unique_id_second_account_is_slugified():
    client = _make_client(second=1)
    client._device_serial_number = "SN123"
    uid = client.unique_id
    assert "sn123" in uid
    assert "test" in uid


def test_hidden_depends_on_music_skill():
    client = _make_client()
    client._capabilities = []
    assert client.hidden is True
    client._capabilities = ["MUSIC_SKILL"]
    assert client.hidden is False


def test_available_getter_setter():
    client = _make_client()
    client.available = True
    assert client.available is True


def test_source_and_session_getters():
    client = _make_client()
    client._source = "Bluetooth"
    client._source_list = ["Bluetooth", "Local Speaker"]
    client._session = {"x": 1}
    assert client.source == "Bluetooth"
    assert client.source_list == ["Bluetooth", "Local Speaker"]
    assert client.session == {"x": 1}


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


def test_state_unavailable():
    client = _make_client()
    client._available = False
    assert client.state == STATE_UNAVAILABLE


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PLAYING", MediaPlayerState.PLAYING),
        ("PAUSED", MediaPlayerState.PAUSED),
        ("IDLE", MediaPlayerState.IDLE),
        ("SOMETHING", MediaPlayerState.IDLE),
    ],
)
def test_state_from_media_player_state(raw, expected):
    client = _make_client()
    client._available = True
    client._media_player_state = raw
    assert client.state == expected


def test_state_bluetooth_streaming():
    client = _make_client()
    client._available = True
    client._connected_bluetooth = True
    client._bluetooth_state = {"streamingState": "MUSIC"}
    assert client.state == MediaPlayerState.PLAYING
    client._bluetooth_state = {"streamingState": "PAUSED"}
    assert client.state == MediaPlayerState.PAUSED


# --------------------------------------------------------------------------- #
# media_* properties
# --------------------------------------------------------------------------- #


def test_media_content_type():
    client = _make_client()
    client._available = True
    client._media_player_state = "PLAYING"
    assert client.media_content_type == MediaType.MUSIC
    client._media_player_state = "IDLE"
    assert client.media_content_type == MediaPlayerState.IDLE


def test_media_artist_bluetooth_fallback():
    client = _make_client()
    client._connected_bluetooth = "MyPhone"
    client._source = None
    client._media_artist = None
    assert client.media_artist == "Streaming from MyPhone"
    client._media_artist = "Real Artist"
    assert client.media_artist == "Real Artist"


def test_media_album_name_bluetooth():
    client = _make_client()
    client._connected_bluetooth = "X"
    client._source = "X"
    client._media_album_name = "Album"
    assert client.media_album_name is None
    client._connected_bluetooth = None
    client._source = None
    assert client.media_album_name == "Album"


def test_media_duration_and_position_bluetooth():
    client = _make_client()
    client._media_duration = 100
    client._media_pos = 50
    client._connected_bluetooth = "X"
    assert client.media_duration is None
    assert client.media_position is None
    client._connected_bluetooth = None
    assert client.media_duration == 100
    assert client.media_position == 50


def test_volume_and_mute_getters():
    client = _make_client()
    client._media_vol_level = 0.4
    client._media_is_muted = True
    assert client.volume_level == 0.4
    assert client.is_volume_muted is True


# --------------------------------------------------------------------------- #
# helper methods
# --------------------------------------------------------------------------- #


def test_set_attrs_populates_media_fields():
    client = _make_client()
    client._set_attrs(
        {
            "state": "PLAYING",
            "infoText": {"title": "Song", "subText1": "Artist", "subText2": "Album"},
            "mainArt": {"url": "http://art"},
            "progress": {"mediaProgress": 10, "mediaLength": 200},
            "volume": {"muted": False, "volume": 40},
        }
    )
    assert client._media_title == "Song"
    assert client._media_artist == "Artist"
    assert client._media_album_name == "Album"
    assert client._media_image_url == "http://art"
    assert client._media_vol_level == pytest.approx(0.4)
    assert client._media_is_muted is False


def test_set_attrs_float_volume_below_one():
    client = _make_client()
    client._set_attrs({"state": "PLAYING", "volume": {"volume": 0.5}})
    assert client._media_vol_level == pytest.approx(0.5)


def test_get_connected_bluetooth_and_list():
    client = _make_client()
    client._bluetooth_state = {
        "pairedDeviceList": [
            {"friendlyName": "Phone", "connected": True, "profiles": ["A2DP-SOURCE"]},
            {"friendlyName": "Tablet", "connected": False, "profiles": []},
        ]
    }
    assert client._get_connected_bluetooth() == "Phone"
    assert client._get_bluetooth_list() == ["Phone", "Tablet"]


def test_get_source_list_builds_input_list():
    client = _make_client()
    client._bluetooth_state = {
        "pairedDeviceList": [
            {"friendlyName": "Phone", "connected": True, "profiles": ["A2DP-SOURCE"]},
            {"friendlyName": "Speaker", "connected": False, "profiles": ["OTHER"]},
        ]
    }
    # _get_source_list = Local Speaker + A2DP-SOURCE capable devices
    assert client._get_source_list() == ["Local Speaker", "Phone"]


def test_get_source_returns_connected_in_list_else_local():
    client = _make_client()
    client._source_list = ["Local Speaker", "Phone"]
    client._bluetooth_state = {
        "pairedDeviceList": [
            {"friendlyName": "Phone", "connected": True, "profiles": ["A2DP-SOURCE"]}
        ]
    }
    assert client._get_source() == "Phone"
    # connected device not in source_list -> default Local Speaker
    client._source_list = ["Local Speaker"]
    assert client._get_source() == "Local Speaker"


def test_get_connected_bluetooth_none_when_nothing_connected():
    client = _make_client()
    client._bluetooth_state = {"pairedDeviceList": []}
    assert client._get_connected_bluetooth() is None


# --------------------------------------------------------------------------- #
# transport commands
# --------------------------------------------------------------------------- #


def _ready_client():
    client = _make_client()
    client._available = True
    client._media_player_state = "PLAYING"
    client._playing_parent = None
    client._session = None
    client._customer_id = "CID"
    api = MagicMock()
    for method in (
        "play",
        "pause",
        "stop",
        "next",
        "previous",
        "shuffle",
        "repeat",
        "set_volume",
    ):
        setattr(api, method, AsyncMock())
    client.alexa_api = api
    client.hass = MagicMock()
    client.hass.data = {
        DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {"options": {CONF_QUEUE_DELAY: 1.5}}}}
    }

    def _consume(coro, *args, **kwargs):
        try:
            coro.close()
        except (AttributeError, RuntimeError):
            pass
        return MagicMock()

    client.hass.async_create_task.side_effect = _consume
    return client, api


@patch(_HTTP2, return_value=True)
async def test_media_play_awaits_api(_mock_http2):
    client, api = _ready_client()
    await client.async_media_play()
    api.play.assert_awaited_once()


@patch(_HTTP2, return_value=True)
async def test_media_play_guarded_when_unavailable(_mock_http2):
    client, api = _ready_client()
    client._available = False
    await client.async_media_play()
    api.play.assert_not_awaited()


@patch(_HTTP2, return_value=True)
async def test_media_pause(_mock_http2):
    client, api = _ready_client()
    await client.async_media_pause()
    api.pause.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_media_stop(_mock_http2):
    client, api = _ready_client()
    await client.async_media_stop()
    api.stop.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_media_next_and_previous(_mock_http2):
    client, api = _ready_client()
    await client.async_media_next_track()
    await client.async_media_previous_track()
    api.next.assert_called_once()
    api.previous.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_set_shuffle(_mock_http2):
    client, api = _ready_client()
    await client.async_set_shuffle(True)
    api.shuffle.assert_called_once()
    assert client._shuffle is True


@patch(_HTTP2, return_value=True)
async def test_set_repeat(_mock_http2):
    client, api = _ready_client()
    await client.async_set_repeat(RepeatMode.ALL)
    api.repeat.assert_called_once()
    assert client._repeat is True


@patch(_HTTP2, return_value=True)
async def test_set_volume_level(_mock_http2):
    client, api = _ready_client()
    client._media_vol_level = 0.5
    await client.async_set_volume_level(0.8)
    api.set_volume.assert_called_once()
    assert client._media_vol_level == pytest.approx(0.8)
    assert client._previous_volume == pytest.approx(0.5)


@patch(_HTTP2, return_value=True)
async def test_mute_volume_stores_and_zeroes(_mock_http2):
    client, api = _ready_client()
    client._media_vol_level = 0.6
    await client.async_mute_volume(True)
    assert client._media_is_muted is True
    assert client._saved_volume == pytest.approx(0.6)
    api.set_volume.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_turn_off_disables_polling(_mock_http2):
    client, _api = _ready_client()
    await client.async_turn_off()
    assert client._should_poll is False


# --------------------------------------------------------------------------- #
# async_unload_entry
# --------------------------------------------------------------------------- #


async def test_unload_entry_removes_media_players():
    device = AsyncMock()
    account = {"entities": {"media_player": {"SN1": device}}}
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}
    entry = MagicMock()
    entry.data = {CONF_EMAIL: _EMAIL}
    result = await async_unload_entry(hass, entry)
    assert result is True
    device.async_remove.assert_awaited_once()
