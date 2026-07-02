"""Tests for media_player.py - AlexaClient properties, state, commands, unload.

Focuses on the synchronous property/getter logic and the simple transport
commands (which delegate to ``alexa_api``); the heavy ``refresh``/``async_update``
/ websocket-event paths are out of scope here.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.media_player import (
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
    RepeatMode,
)
from homeassistant.components.media_player.const import ATTR_MEDIA_ANNOUNCE
from homeassistant.const import STATE_UNAVAILABLE
import pytest

from custom_components.alexa_media.const import (
    CONF_PUBLIC_URL,
    CONF_QUEUE_DELAY,
    DATA_ALEXAMEDIA,
    PUBLIC_URL_ERROR_MESSAGE,
    STREAMING_ERROR_MESSAGE,
)
from custom_components.alexa_media.media_player import AlexaClient

_HTTP2 = "custom_components.alexa_media.media_player.is_http2_enabled"
_CALL_LATER = "custom_components.alexa_media.media_player.async_call_later"
_SLEEP = "custom_components.alexa_media.media_player.asyncio.sleep"
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


def test_assumed_state_and_device_family_and_should_poll():
    client = _make_client()
    client._assumed_state = True
    client._device_family = "ECHO"
    client._should_poll = False
    assert client.assumed_state is True
    assert client.device_family == "ECHO"
    assert client.should_poll is False


def test_media_content_type_paused():
    client = _make_client()
    client._available = True
    client._media_player_state = "PAUSED"
    assert client.media_content_type == MediaType.MUSIC


def test_media_title_and_remotely_accessible():
    client = _make_client()
    client._media_title = "Track"
    client._media_image_url = "http://art"
    assert client.media_title == "Track"
    assert client.media_image_remotely_accessible is True
    client._media_image_url = None
    assert client.media_image_remotely_accessible is False


def test_media_artist_generic_streaming_string():
    client = _make_client()
    client._connected_bluetooth = "Phone"
    client._source = "Speaker"
    client._media_artist = "Streaming"
    # the generic Amazon "Streaming" boot string is replaced
    assert client.media_artist == "Streaming from Speaker"


def test_media_image_url_escapes_parentheses():
    client = _make_client()
    client._connected_bluetooth = None
    client._media_image_url = "http://x/a(b)c.jpg"
    assert client.media_image_url == "http://x/a%28b%29c.jpg"
    client._media_image_url = None
    assert client.media_image_url is None


def test_media_image_url_none_when_bluetooth_session():
    client = _make_client()
    client._connected_bluetooth = "Phone"
    client._session = {"mediaId": "BluetoothMediaId"}
    client._media_image_url = "http://x"
    assert client.media_image_url is None


def test_icon_bluetooth_and_default():
    client = _make_client()
    client._connected_bluetooth = "Phone"
    client._session = {"mediaId": "BluetoothMediaId"}
    assert client.icon == "mdi:music-note-bluetooth"
    client._connected_bluetooth = None
    client._session = None
    # falls back to MediaPlayerEntity default (None)
    assert client.icon is None


def test_media_position_updated_at_variants():
    client = _make_client()
    # synthesized bluetooth session -> None (no linear timeline)
    client._session = {"mediaId": "BluetoothMediaId"}
    assert client.media_position_updated_at is None
    # player_info supplies its own last_update
    client._session = None
    client._player_info = {"last_update": "TS"}
    assert client.media_position_updated_at == "TS"
    # fallback to _last_update
    client._player_info = None
    assert client.media_position_updated_at == client._last_update


def test_dnd_state_getter_setter():
    client = _make_client()
    client.dnd_state = True
    assert client.dnd_state is True
    assert client._dnd is True


def test_shuffle_setter_schedules_update():
    client = _make_client()
    client.schedule_update_ha_state = MagicMock()
    client.shuffle = True
    assert client.shuffle is True
    client.schedule_update_ha_state.assert_called_once()


def test_repeat_state_getter_setter():
    client = _make_client()
    client.schedule_update_ha_state = MagicMock()
    client.repeat_state = True
    assert client.repeat_state is True
    client.schedule_update_ha_state.assert_called_once()


def test_extra_state_attributes_shape():
    client = _make_client()
    client._available = True
    client._last_called = True
    client._connected_bluetooth = "Phone"
    client._bluetooth_list = ["Phone"]
    client._previous_volume = 0.3
    attr = client.extra_state_attributes
    assert attr["available"] is True
    assert attr["last_called"] is True
    assert attr["connected_bluetooth"] == "Phone"
    assert attr["bluetooth_list"] == ["Phone"]
    assert attr["previous_volume"] == 0.3


def test_device_info_shape():
    client = _make_client()
    client._device_name = "Bedroom"
    client._device_serial_number = "SN9"
    client._software_version = "9.9"
    info = client.device_info
    assert info["name"] == "Bedroom"
    assert info["manufacturer"] == "Amazon"
    assert info["serial_number"] == "SN9"
    assert info["sw_version"] == "9.9"
    assert info["identifiers"] == {("alexa_media", "SN9")}


# --------------------------------------------------------------------------- #
# _get_last_called
# --------------------------------------------------------------------------- #


def test_get_last_called_self_match():
    client = _make_client()
    client._device_serial_number = "SN1"
    client._app_device_list = []
    client.hass = MagicMock()
    client.hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {_EMAIL: {"last_called": {"serialNumber": "SN1"}}}
        }
    }
    assert client._get_last_called() is True


def test_get_last_called_via_app_device_list():
    client = _make_client()
    client._device_serial_number = "OTHER"
    client._app_device_list = [{"serialNumber": "APP1"}]
    client.hass = MagicMock()
    client.hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {_EMAIL: {"last_called": {"serialNumber": "APP1"}}}
        }
    }
    assert client._get_last_called() is True


def test_get_last_called_no_hass_is_false():
    client = _make_client()
    client.hass = None
    client._device_serial_number = "SN1"
    client._app_device_list = []
    assert client._get_last_called() is False


# --------------------------------------------------------------------------- #
# async_select_source
# --------------------------------------------------------------------------- #


@patch(_HTTP2, return_value=True)
async def test_select_source_local_speaker(_mock_http2):
    client, api = _ready_client()
    api.disconnect_bluetooth = MagicMock()
    await client.async_select_source("Local Speaker")
    api.disconnect_bluetooth.assert_called_once()
    assert client._source == "Local Speaker"


@patch(_HTTP2, return_value=True)
async def test_select_source_bluetooth_device(_mock_http2):
    client, api = _ready_client()
    api.set_bluetooth = MagicMock()
    client._bluetooth_state = {
        "pairedDeviceList": [{"friendlyName": "Phone", "address": "AA"}]
    }
    await client.async_select_source("Phone")
    api.set_bluetooth.assert_called_once_with("AA")
    assert client._source == "Phone"


# --------------------------------------------------------------------------- #
# turn_on / send_* notification helpers
# --------------------------------------------------------------------------- #


@patch(_HTTP2, return_value=True)
async def test_turn_on_enables_polling_and_pauses(_mock_http2):
    client, api = _ready_client()
    await client.async_turn_on()
    assert client._should_poll is True
    api.pause.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_send_tts_announcement_mobilepush_dropin(_mock_http2):
    client, api = _ready_client()
    await client.async_send_tts("hi")
    await client.async_send_announcement("ann")
    await client.async_send_mobilepush("push")
    await client.async_send_dropin_notification("drop")
    api.send_tts.assert_called_once_with("hi", customer_id="CID")
    api.send_announcement.assert_called_once_with("ann", customer_id="CID")
    api.send_mobilepush.assert_called_once_with("push", customer_id="CID")
    api.send_dropin_notification.assert_called_once_with("drop", customer_id="CID")


# --------------------------------------------------------------------------- #
# async_play_tts_cloud_say
# --------------------------------------------------------------------------- #


@patch(_HTTP2, return_value=True)
async def test_play_tts_cloud_say_no_announce_warns(_mock_http2):
    client, api = _ready_client()
    await client.async_play_tts_cloud_say("http://pub/", "hello.mp3")
    api.send_tts.assert_called_once_with(STREAMING_ERROR_MESSAGE, customer_id="CID")


@patch("custom_components.alexa_media.media_player.os.path.exists", return_value=True)
@patch(_HTTP2, return_value=True)
async def test_play_tts_cloud_say_announce_sends_audio(_mock_http2, _mock_exists):
    client, api = _ready_client()
    await client.async_play_tts_cloud_say(
        "http://pub/", "hello.mp3", **{ATTR_MEDIA_ANNOUNCE: True}
    )
    # cached output already exists -> straight to send_tts with an <audio> src
    api.send_tts.assert_called_once()
    sent = api.send_tts.call_args.args[0]
    assert sent.startswith("<audio src='http://pub/local/alexa_tts")


# --------------------------------------------------------------------------- #
# async_play_media (per media_type branch)
# --------------------------------------------------------------------------- #


@patch(_HTTP2, return_value=True)
async def test_play_media_music_no_public_url(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("music", "hello.mp3")
    api.send_tts.assert_called_once_with(PUBLIC_URL_ERROR_MESSAGE, customer_id="CID")


@patch(_HTTP2, return_value=True)
async def test_play_media_music_with_public_url(_mock_http2):
    client, api = _ready_client()
    client.hass.data[DATA_ALEXAMEDIA]["accounts"][_EMAIL]["options"][
        CONF_PUBLIC_URL
    ] = "http://pub/"
    await client.async_play_media("music", "hello.mp3")
    # routed to tts cloud say; non-announce path warns via send_tts
    api.send_tts.assert_called_once_with(STREAMING_ERROR_MESSAGE, customer_id="CID")


@patch(_HTTP2, return_value=True)
async def test_play_media_sequence(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("sequence", "seqid")
    api.send_sequence.assert_called_once_with(
        "seqid", customer_id="CID", queue_delay=1.5
    )


@patch(_HTTP2, return_value=True)
async def test_play_media_routine(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("routine", "routid")
    api.run_routine.assert_called_once_with("routid", queue_delay=1.5)


@patch(_HTTP2, return_value=True)
async def test_play_media_sound(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("sound", "soundid")
    api.play_sound.assert_called_once_with(
        "soundid", customer_id="CID", queue_delay=1.5
    )


@patch(_HTTP2, return_value=True)
async def test_play_media_skill(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("skill", "skillid")
    api.run_skill.assert_called_once_with("skillid", queue_delay=1.5)


@patch(_HTTP2, return_value=True)
async def test_play_media_image(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("image", "imgid")
    api.set_background.assert_called_once_with("imgid")


@patch(_HTTP2, return_value=True)
async def test_play_media_custom(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("custom", "turn on lights")
    api.run_custom.assert_called_once_with(
        "turn on lights", customer_id="CID", queue_delay=1.5
    )


@patch(_HTTP2, return_value=True)
async def test_play_media_default_plays_music_with_timer(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("TUNEIN", "station", extra={"timer": "30"})
    api.play_music.assert_called_once()
    assert api.play_music.call_args.args == ("TUNEIN", "station")
    assert api.play_music.call_args.kwargs["timer"] == 30
    assert api.play_music.call_args.kwargs["customer_id"] == "CID"


@patch(_HTTP2, return_value=True)
async def test_play_media_default_invalid_timer_is_none(_mock_http2):
    client, api = _ready_client()
    await client.async_play_media("TUNEIN", "station", extra={"timer": "abc"})
    assert api.play_music.call_args.kwargs["timer"] is None


# --------------------------------------------------------------------------- #
# async_update
# --------------------------------------------------------------------------- #


def _update_client():
    client = _make_client()
    client._device_serial_number = "SN1"
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {}}}}
    client._login.session.closed = False
    client.schedule_update_ha_state = MagicMock()
    client.refresh = AsyncMock()
    return client


async def test_async_update_guard_uninitialized_entity():
    client = _update_client()
    client.entity_id = None
    await client.async_update()
    assert client._assumed_state is True
    assert client.available is False
    client.refresh.assert_not_awaited()


async def test_async_update_device_not_found_sets_unavailable():
    client = _update_client()
    client.entity_id = "media_player.echo"
    # no devices entry for the serial
    await client.async_update()
    assert client.available is False
    client.refresh.assert_not_awaited()


@patch(_HTTP2, return_value=True)
async def test_async_update_http2_disables_polling(_mock_http2):
    client = _update_client()
    client.entity_id = "media_player.echo"
    client.hass.data[DATA_ALEXAMEDIA]["accounts"][_EMAIL]["devices"] = {
        "media_player": {"SN1": {"online": True}}
    }
    await client.async_update()
    client.refresh.assert_awaited_once()
    assert client._should_poll is False
    client.schedule_update_ha_state.assert_called()


@patch(_CALL_LATER)
@patch(_HTTP2, return_value=False)
async def test_async_update_not_playing_one_last_poll(_mock_http2, mock_later):
    client = _update_client()
    client.entity_id = "media_player.echo"
    client._available = True
    client._media_player_state = "IDLE"
    client._should_poll = True
    client.hass.data[DATA_ALEXAMEDIA]["accounts"][_EMAIL]["devices"] = {
        "media_player": {"SN1": {"online": True}}
    }
    await client.async_update()
    assert client._should_poll is False
    mock_later.assert_called_once()


@patch(_CALL_LATER)
@patch(_HTTP2, return_value=False)
async def test_async_update_playing_schedules_scan(_mock_http2, mock_later):
    client = _update_client()
    client.entity_id = "media_player.echo"
    client._available = True
    client._media_player_state = "PLAYING"
    client._last_update = 0
    client.hass.data[DATA_ALEXAMEDIA]["accounts"][_EMAIL]["devices"] = {
        "media_player": {"SN1": {"online": True}}
    }
    await client.async_update()
    assert client._should_poll is False
    mock_later.assert_called_once()


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #


def _device_dict(online=True, capabilities=None, **extra):
    device = {
        "accountName": "Office",
        "deviceFamily": "ECHO",
        "deviceType": "TYPE1",
        "serialNumber": "SN1",
        "appDeviceList": [],
        "deviceOwnerCustomerId": "owner",
        "softwareVersion": "1.2.3",
        "online": online,
        "capabilities": capabilities if capabilities is not None else ["MUSIC_SKILL"],
        "clusterMembers": [],
        "parentClusters": [],
        "auth_info": {
            "authenticated": True,
            "canAccessPrimeMusicContent": True,
            "customerEmail": "e@x",
            "customerId": "CID",
            "customerName": "Owner",
        },
    }
    device.update(extra)
    return device


async def test_refresh_sets_device_fields_and_skip_api():
    client = _make_client()
    client._last_called = True  # avoid notify path
    client.hass = MagicMock()
    client.hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {_EMAIL: {"last_called": {"serialNumber": "SN1"}}}
        }
    }
    client.schedule_update_ha_state = MagicMock()
    await client.refresh(_device_dict(), skip_api=True)
    assert client._device_name == "Office"
    assert client._device_serial_number == "SN1"
    assert client._available is True
    assert client._customer_id == "CID"
    assert client._capabilities == ["MUSIC_SKILL"]
    client.schedule_update_ha_state.assert_called()


async def test_refresh_offline_clears_media():
    client = _make_client()
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {}}}}
    client.schedule_update_ha_state = MagicMock()
    client._media_title = "stale"
    await client.refresh(_device_dict(online=False, capabilities=[]))
    assert client._available is False
    assert client._media_player_state == "IDLE"
    assert client._session is None


@patch(_HTTP2, return_value=True)
async def test_refresh_builds_session_from_player_info(_mock_http2):
    client = _make_client()
    client._available = True
    client._capabilities = ["MUSIC_SKILL"]
    client._parent_clusters = None
    client._cluster_members = []
    client._session = None
    client._player_info = {
        "state": "PLAYING",
        "infoText": {"title": "T", "subText1": "A", "subText2": "Alb"},
        "mainArt": {"url": "http://x"},
        "progress": {"mediaProgress": 10, "mediaLength": 100},
        "volume": {"volume": 50, "muted": False},
        "transport": {
            "shuffle": "SELECTED",
            "repeat": "HIDDEN",
            "next": "ENABLED",
            "previous": "ENABLED",
            "seekForward": "ENABLED",
            "seekBackward": "ENABLED",
        },
    }
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {}}}}
    client.schedule_update_ha_state = MagicMock()
    await client.refresh()
    assert client._media_title == "T"
    assert client._media_artist == "A"
    assert client._media_album_name == "Alb"
    assert client._media_vol_level == pytest.approx(0.5)
    assert client._shuffle is True
    assert client._repeat is None
    assert client._media_player_state == "PLAYING"
    # repeat was HIDDEN -> REPEAT_SET feature removed
    assert not client._attr_supported_features & MediaPlayerEntityFeature.REPEAT_SET


# --------------------------------------------------------------------------- #
# _handle_event
# --------------------------------------------------------------------------- #


def _event_client(serial="SN1"):
    client = _make_client(serial=serial)
    client._device_serial_number = serial
    client._app_device_list = []
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {}}}}
    client.schedule_update_ha_state = MagicMock()
    client.async_schedule_update_ha_state = MagicMock()
    return client


@patch(_HTTP2, return_value=True)
async def test_handle_event_last_called_change(_mock_http2):
    client = _event_client()
    client._update_notify_targets = AsyncMock()
    event = {
        "last_called_change": {
            "serialNumber": "SN1",
            "timestamp": 123,
            "summary": "sum",
            "response": "resp",
        }
    }
    await client._handle_event(event)
    assert client._last_called is True
    assert client._last_called_timestamp == 123
    assert client._last_called_summary == "sum"
    client._update_notify_targets.assert_awaited_once()
    client.async_schedule_update_ha_state.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_handle_event_last_called_change_other_device(_mock_http2):
    client = _event_client()
    event = {"last_called_change": {"serialNumber": "OTHER", "timestamp": 1}}
    await client._handle_event(event)
    assert client._last_called is False


async def test_handle_event_bluetooth_connected_synthesizes_session():
    client = _event_client()
    client._source_list = ["Local Speaker", "Phone"]
    event = {
        "bluetooth_change": {
            "deviceSerialNumber": "SN1",
            "streamingState": "MUSIC",
            "pairedDeviceList": [
                {
                    "friendlyName": "Phone",
                    "connected": True,
                    "profiles": ["A2DP-SOURCE"],
                    "address": "AA",
                }
            ],
        }
    }
    await client._handle_event(event)
    assert client._connected_bluetooth == "Phone"
    assert client._session["mediaId"] == "BluetoothMediaId"
    assert client._media_player_state == "PLAYING"
    assert client._media_title == "Bluetooth"
    assert client._media_artist == "Streaming from Phone"
    assert not client._attr_supported_features & MediaPlayerEntityFeature.SEEK


async def test_handle_event_bluetooth_disconnected_clears():
    client = _event_client()
    client._connected_bluetooth = "Phone"
    client._session = {"mediaId": "BluetoothMediaId"}
    event = {
        "bluetooth_change": {
            "deviceSerialNumber": "SN1",
            "streamingState": "NONE",
            "pairedDeviceList": [
                {"friendlyName": "Phone", "connected": False, "profiles": []}
            ],
        }
    }
    await client._handle_event(event)
    assert client._connected_bluetooth is None
    assert client._session is None
    assert client._media_player_state == "IDLE"


async def test_handle_event_player_state_volume():
    client = _event_client()
    client._session = {"volume": {}}
    event = {
        "player_state": {
            "dopplerId": {"deviceSerialNumber": "SN1"},
            "volumeSetting": 60,
            "isMuted": True,
        }
    }
    await client._handle_event(event)
    assert client._media_vol_level == pytest.approx(0.6)
    assert client._media_is_muted is True


async def test_handle_event_player_state_connection_offline():
    client = _event_client()
    event = {
        "player_state": {
            "dopplerId": {"deviceSerialNumber": "SN1"},
            "dopplerConnectionState": "OFFLINE",
        }
    }
    await client._handle_event(event)
    assert client.available is False


async def test_handle_event_queue_state_repeat():
    client = _event_client()
    event = {
        "queue_state": {
            "dopplerId": {"deviceSerialNumber": "SN1"},
            "trackOrderChanged": False,
            "loopMode": "LOOP_QUEUE",
        }
    }
    await client._handle_event(event)
    assert client._repeat is True
    assert client._attr_supported_features & MediaPlayerEntityFeature.REPEAT_SET


async def test_handle_event_queue_state_shuffle():
    client = _event_client()
    event = {
        "queue_state": {
            "dopplerId": {"deviceSerialNumber": "SN1"},
            "playBackOrder": "SHUFFLE_ALL",
        }
    }
    await client._handle_event(event)
    assert client._shuffle is True
    assert client._attr_supported_features & MediaPlayerEntityFeature.SHUFFLE_SET


async def test_handle_event_push_activity_triggers_update():
    client = _event_client()
    client._available = True
    client._media_player_state = "IDLE"
    client.async_update = AsyncMock()
    event = {"push_activity": {"key": {"serialNumber": "SN1"}}}
    with patch(_SLEEP, new=AsyncMock()):
        await client._handle_event(event)
    client.async_update.assert_awaited_once()


async def test_handle_event_now_playing_matches_waiting_media():
    client = _event_client()
    client._waiting_media_id = "MID"
    event = {
        "now_playing": {
            "update": {
                "update": {
                    "nowPlayingData": {
                        "mediaId": "MID",
                        "playerState": "PLAYING",
                        "progress": {"mediaLength": 200000, "mediaProgress": 5000},
                        "mainArt": {"fullUrl": "http://art"},
                    }
                }
            }
        }
    }
    await client._handle_event(event)
    assert client._player_info["state"] == "PLAYING"
    assert client._player_info["progress"]["mediaLength"] == 200
    assert client._player_info["progress"]["mediaProgress"] == 5
    assert client._player_info["mainArt"]["url"] == "http://art"


async def test_handle_event_parent_state_sets_playing_parent():
    client = _event_client()
    parent_obj = MagicMock()
    client.hass.data[DATA_ALEXAMEDIA]["accounts"][_EMAIL]["entities"] = {
        "media_player": {"PARENT": parent_obj}
    }
    event = {
        "parent_state": {
            "dopplerId": {
                "deviceSerialNumber": "SN1",
                "parentSerialNumber": "PARENT",
            },
            "state": "PLAYING",
            "volume": {"muted": False, "volume": 30},
            "infoText": {"title": "PT"},
        }
    }
    await client._handle_event(event)
    assert client._media_title == "PT"
    assert client._playing_parent is parent_obj
    assert client._media_player_state == "PLAYING"
    assert client._media_vol_level == pytest.approx(0.3)


# --------------------------------------------------------------------------- #
# _update_notify_targets
# --------------------------------------------------------------------------- #


async def test_update_notify_targets_no_service():
    client = _make_client()
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {}}
    # returns early; no exception
    await client._update_notify_targets()


async def test_update_notify_targets_not_ready():
    client = _make_client()
    client.hass = MagicMock()
    # notify without registered_targets attribute -> early return
    client.hass.data = {DATA_ALEXAMEDIA: {"notify_service": SimpleNamespace()}}
    await client._update_notify_targets()


def _notify_mock(last_called, registered_targets):
    notify = MagicMock()
    notify.targets = {"last_called": "uid1"}
    notify.registered_targets = registered_targets
    notify.last_called = last_called
    notify._target_service_name_prefix = "alexa_media"
    notify.async_register_services = AsyncMock()
    return notify


@patch(_CALL_LATER)
async def test_update_notify_targets_no_remap(mock_later):
    client = _make_client()
    client._device_serial_number = "SN1"
    client.entity_id = "media_player.echo"
    notify = _notify_mock(last_called=False, registered_targets={})
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"notify_service": notify}}
    await client._update_notify_targets()
    notify.async_register_services.assert_awaited_once()
    mock_later.assert_called_once()


@patch(_CALL_LATER)
async def test_update_notify_targets_remaps_stale_last_called(mock_later):
    client = _make_client()
    client._device_serial_number = "SN1"
    client.entity_id = "media_player.echo"
    # last_called True + mapping mismatch -> toggle re-registers (3 total)
    notify = _notify_mock(last_called=True, registered_targets={})
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"notify_service": notify}}
    await client._update_notify_targets()
    assert notify.async_register_services.await_count == 3
    mock_later.assert_called_once()


# --------------------------------------------------------------------------- #
# async_added_to_hass / async_will_remove_from_hass
# --------------------------------------------------------------------------- #


async def test_async_added_to_hass_connects_dispatcher():
    client = _make_client()
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {}}}}
    client.refresh = AsyncMock()
    listener = MagicMock()
    with patch(
        "custom_components.alexa_media.media_player.async_dispatcher_connect",
        return_value=listener,
    ) as mock_connect:
        await client.async_added_to_hass()
    client.refresh.assert_awaited_once()
    mock_connect.assert_called_once()
    assert client._listener is listener


async def test_async_will_remove_from_hass_disconnects():
    client = _make_client()
    listener = MagicMock()
    client._listener = listener
    client.hass = MagicMock()
    client.hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {}}}}
    await client.async_will_remove_from_hass()
    listener.assert_called_once()


async def test_init_calls_refresh_skip_api():
    client = _make_client()
    client.refresh = AsyncMock()
    device = {"accountName": "Echo", "serialNumber": "SN1"}
    await client.init(device)
    client.refresh.assert_awaited_once_with(device, skip_api=True)


def test_make_dispatcher_data_rewrites_doppler():
    client = _make_client()
    client._device_serial_number = "SN1"
    payload = client._make_dispatcher_data({"a": 1}, "DEV2")
    assert payload["dopplerId"]["deviceSerialNumber"] == "DEV2"
    assert payload["dopplerId"]["parentSerialNumber"] == "SN1"
    assert payload["isPlayingInLemur"] is False
    assert payload["volume"] is None


def test_set_attrs_lemur_composite_volume():
    client = _make_client()
    client._set_attrs(
        {
            "state": "PLAYING",
            "lemurVolume": {"compositeVolume": {"muted": True, "volume": 80}},
        }
    )
    assert client._media_is_muted is True
    assert client._media_vol_level == pytest.approx(0.8)


async def test_will_remove_handles_missing_listener():
    client = _make_client()
    client._listener = MagicMock()
    coordinator = MagicMock()
    coordinator.async_remove_listener.side_effect = AttributeError
    client.hass = MagicMock()
    client.hass.data = {
        DATA_ALEXAMEDIA: {"accounts": {_EMAIL: {"coordinator": coordinator}}}
    }
    # AttributeError from async_remove_listener is swallowed
    await client.async_will_remove_from_hass()
    client._listener.assert_called_once()


@patch(_HTTP2, return_value=True)
async def test_mute_volume_unmute_restores_saved(_mock_http2):
    client, api = _ready_client()
    client._saved_volume = 0.7
    await client.async_mute_volume(False)
    assert client._media_is_muted is False
    api.set_volume.assert_called_once_with(0.7)


@patch(_HTTP2, return_value=True)
async def test_mute_volume_unmute_default_when_no_saved(_mock_http2):
    client, api = _ready_client()
    client._saved_volume = None
    await client.async_mute_volume(False)
    api.set_volume.assert_called_once_with(50)


@patch(_HTTP2, return_value=True)
async def test_transport_commands_without_hass_await(_mock_http2):
    client, api = _ready_client()
    client.hass = None
    await client.async_media_next_track()
    await client.async_media_previous_track()
    api.next.assert_awaited_once()
    api.previous.assert_awaited_once()


@patch(_SLEEP, new=AsyncMock())
async def test_handle_event_player_state_audio_playing():
    client = _event_client()
    client._player_info = {}  # non-None skips the pre-refresh sleep
    client.async_update = AsyncMock()
    event = {
        "player_state": {
            "dopplerId": {"deviceSerialNumber": "SN1"},
            "audioPlayerState": "PLAYING",
        }
    }
    await client._handle_event(event)
    assert client._media_player_state == "PLAYING"
    client.async_update.assert_awaited_once()


@patch(_SLEEP, new=AsyncMock())
async def test_handle_event_player_state_interrupted_clears():
    client = _event_client()
    client._media_title = "stale"
    client.async_update = AsyncMock()
    event = {
        "player_state": {
            "dopplerId": {"deviceSerialNumber": "SN1"},
            "audioPlayerState": "INTERRUPTED",
        }
    }
    await client._handle_event(event)
    assert client._media_player_state == "IDLE"
    client.async_update.assert_awaited_once()


async def test_handle_event_now_playing_dispatches_to_cluster():
    client = _event_client()
    client._waiting_media_id = "MID"
    client._cluster_members = ["M1"]
    event = {
        "now_playing": {
            "update": {
                "update": {
                    "nowPlayingData": {
                        "mediaId": "MID",
                        "playerState": "PLAYING",
                        "progress": {"mediaLength": 100000, "mediaProgress": 1000},
                        "mainArt": {"url": "http://art"},
                    }
                }
            }
        }
    }
    with patch(
        "custom_components.alexa_media.media_player.async_dispatcher_send"
    ) as mock_send:
        await client._handle_event(event)
    mock_send.assert_called_once()
