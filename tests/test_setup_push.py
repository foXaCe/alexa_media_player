"""Tests for the simpler HTTP/2 push handlers in setup.push."""

from unittest.mock import MagicMock, patch

from alexapy import AlexapyLoginError

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.push import (
    http2_close_handler,
    http2_error_handler,
    http2_open_handler,
)

EMAIL = "user@example.com"
_CONNECT = "custom_components.alexa_media.setup.push.http2_connect"


def _push_ctx(account):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}}
    login = MagicMock()
    login.email = EMAIL
    login.url = "amazon.fr"
    login.close_requested = False
    login.status = {"login_successful": True}
    login.session.closed = False
    ctx = SetupContext(
        hass=hass, config_entry=MagicMock(), email=EMAIL, login_obj=login
    )
    return ctx, hass, login


# --- http2_open_handler -----------------------------------------------------


async def test_http2_open_handler_resets_error_state():
    account = {"http2error": 5, "http2_lastattempt": 0.0}
    ctx, _, _ = _push_ctx(account)
    await http2_open_handler(ctx)
    assert account["http2error"] == 0
    assert account["http2_lastattempt"] > 0


# --- http2_close_handler early returns --------------------------------------


async def test_http2_close_handler_no_reconnect_when_close_requested():
    account = {"http2": "client", "http2error": 0, "http2_lastattempt": 0.0}
    ctx, _, login = _push_ctx(account)
    login.close_requested = True
    with patch(_CONNECT) as connect:
        await http2_close_handler(ctx)
    assert account["http2"] is None
    connect.assert_not_called()


async def test_http2_close_handler_no_reconnect_when_login_failed():
    account = {"http2": "client", "http2error": 0, "http2_lastattempt": 0.0}
    ctx, _, login = _push_ctx(account)
    login.status = {"login_successful": False}
    with patch(_CONNECT) as connect:
        await http2_close_handler(ctx)
    assert account["http2"] is None
    connect.assert_not_called()


# --- http2_error_handler ----------------------------------------------------


async def test_http2_error_handler_fires_relogin_on_login_error():
    account = {"http2error": 0, "http2": "client"}
    ctx, hass, _ = _push_ctx(account)
    await http2_error_handler(ctx, AlexapyLoginError("bad"))
    assert account["http2error"] == 5
    assert account["http2"] is None
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" in fired


async def test_http2_error_handler_increments_on_benign_error():
    account = {"http2error": 1, "http2": "client"}
    ctx, hass, _ = _push_ctx(account)
    await http2_error_handler(ctx, "transient blip")
    assert account["http2error"] == 2
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" not in fired
