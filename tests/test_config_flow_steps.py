"""Step-by-step coverage tests for config_flow.py.

These complement tests/test_config_flow.py (which focuses on the reauth/_test_login
bugfixes) by exercising every flow step, the options flow and the auth views.
The framework methods (async_show_form/async_abort/...) are mocked so each test
asserts the *branch logic* of a step rather than the FlowManager plumbing.
"""

from collections import OrderedDict
import datetime
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import ClientConnectionError
from aiohttp.web_exceptions import HTTPBadRequest
from alexapy import AlexapyConnectionError, AlexapyPyotpInvalidKey
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_SCAN_INTERVAL,
    CONF_URL,
)
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import Unauthorized
import httpx
import pytest
from yarl import URL

from custom_components.alexa_media.config_flow import (
    AlexaMediaAuthorizationCallbackView,
    AlexaMediaAuthorizationProxyView,
    AlexaMediaFlowHandler,
    OptionsFlowHandler,
    configured_instances,
    in_progress_instances,
)
from custom_components.alexa_media.const import (
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_EXTENDED_ENTITY_DISCOVERY,
    CONF_HASS_URL,
    CONF_INCLUDE_DEVICES,
    CONF_OAUTH,
    CONF_OTPSECRET,
    CONF_PROXY_WARNING,
    CONF_PUBLIC_URL,
    CONF_QUEUE_DELAY,
    CONF_SECURITYCODE,
    CONF_TOTP_REGISTER,
    DATA_ALEXAMEDIA,
    DOMAIN,
)

_GET_URL = "custom_components.alexa_media.config_flow.get_url"
_CALC_UUID = "custom_components.alexa_media.config_flow.calculate_uuid"
_ALEXA_LOGIN = "custom_components.alexa_media.config_flow.AlexaLogin"
_ALEXA_PROXY = "custom_components.alexa_media.config_flow.AlexaProxy"
_SLEEP = "custom_components.alexa_media.config_flow.sleep"
_DISMISS = (
    "custom_components.alexa_media.config_flow.async_dismiss_persistent_notification"
)


def _make_flow():
    flow = AlexaMediaFlowHandler()
    flow.hass = MagicMock()
    flow.hass.data = {DATA_ALEXAMEDIA: {"accounts": {}, "config_flows": {}}}
    # configured_instances() iterates this; default to no existing entries.
    flow.hass.config_entries.async_entries.return_value = []
    return flow


def _login_mock(email="a@example.com", url="amazon.com", status=None):
    login = MagicMock()
    login.email = email
    login.url = url
    login.status = status if status is not None else {}
    return login


# --------------------------------------------------------------------------- #
# Module-level helpers
# --------------------------------------------------------------------------- #


def test_configured_instances():
    hass = MagicMock()
    entry = MagicMock()
    entry.title = "a@example.com - amazon.com"
    hass.config_entries.async_entries.return_value = [entry]
    assert configured_instances(hass) == {"a@example.com - amazon.com"}


def test_in_progress_instances_filters_by_handler():
    hass = MagicMock()
    hass.config_entries.flow.async_progress.return_value = [
        {"flow_id": "f1", "handler": DOMAIN},
        {"flow_id": "f2", "handler": "other_domain"},
    ]
    assert in_progress_instances(hass) == {"f1"}


# --------------------------------------------------------------------------- #
# _update_ord_dict / _save_user_input_to_config / _update_schema_defaults
# --------------------------------------------------------------------------- #


def test_update_ord_dict_overrides_existing_keys():
    flow = _make_flow()
    old = OrderedDict([("a", 1), ("b", 2)])
    result = flow._update_ord_dict(old, {"b": 20})
    assert result["a"] == 1
    assert result["b"] == 20


def test_save_user_input_none_is_noop():
    flow = _make_flow()
    flow._save_user_input_to_config(None)
    assert flow.config == OrderedDict()


def test_save_user_input_normalizes_values():
    flow = _make_flow()
    flow._save_user_input_to_config(
        {
            CONF_EMAIL: "a@example.com",
            CONF_PASSWORD: "pw",
            CONF_URL: "amazon.com",
            CONF_PUBLIC_URL: "http://pub",
            CONF_SCAN_INTERVAL: 30,
            CONF_QUEUE_DELAY: 2.0,
            CONF_INCLUDE_DEVICES: ["Echo1", "Echo2"],
            CONF_EXCLUDE_DEVICES: [],
            CONF_OTPSECRET: "ABCD EFGH",
            CONF_SECURITYCODE: "123456",
            CONF_EXTENDED_ENTITY_DISCOVERY: True,
            CONF_DEBUG: True,
        }
    )
    assert flow.config[CONF_EMAIL] == "a@example.com"
    assert flow.config[CONF_PUBLIC_URL] == "http://pub/"  # trailing slash added
    assert flow.config[CONF_OTPSECRET] == "ABCDEFGH"  # spaces stripped
    assert flow.config[CONF_INCLUDE_DEVICES] == "Echo1,Echo2"  # list joined
    assert flow.config[CONF_EXCLUDE_DEVICES] == ""  # empty list -> ""
    assert flow.config[CONF_SECURITYCODE] == "123456"
    assert flow.config[CONF_EXTENDED_ENTITY_DISCOVERY] is True


def test_save_user_input_timedelta_and_securitycode_removal():
    flow = _make_flow()
    flow.config[CONF_SECURITYCODE] = "old"
    flow._save_user_input_to_config({CONF_SCAN_INTERVAL: timedelta(seconds=45)})
    assert CONF_SECURITYCODE not in flow.config  # popped when absent from input
    assert flow.config[CONF_SCAN_INTERVAL] == 45.0


def test_update_schema_defaults_returns_schema():
    flow = _make_flow()
    assert flow._update_schema_defaults() is not None


# --------------------------------------------------------------------------- #
# async_step_import / async_step_user
# --------------------------------------------------------------------------- #


async def test_step_import_delegates_to_legacy():
    flow = _make_flow()
    flow.async_step_user_legacy = AsyncMock(return_value={"type": "form"})
    await flow.async_step_import({CONF_EMAIL: "a@example.com"})
    flow.async_step_user_legacy.assert_awaited_once()


@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_no_input_shows_form(_mock_url):
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user(None)
    assert flow.async_show_form.call_args.kwargs["step_id"] == "user"


@patch(_ALEXA_LOGIN, side_effect=AlexapyPyotpInvalidKey)
@patch(_CALC_UUID, new_callable=AsyncMock)
@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_invalid_otp_key(_mock_url, mock_uuid, _mock_login):
    mock_uuid.return_value = {"uuid": "uuid-1"}
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    user_input = {
        CONF_EMAIL: "a@example.com",
        CONF_PASSWORD: "pw",
        CONF_URL: "amazon.com",
        CONF_DEBUG: False,
    }
    await flow.async_step_user(user_input)
    assert flow.async_show_form.call_args.kwargs["errors"] == {
        "base": "2fa_key_invalid"
    }


# --------------------------------------------------------------------------- #
# async_step_user_legacy
# --------------------------------------------------------------------------- #


async def test_step_user_legacy_no_input_shows_form():
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user_legacy(None)
    assert flow.async_show_form.call_args.kwargs["step_id"] == "user"


async def test_step_user_legacy_identifier_exists():
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    entry = MagicMock()
    entry.title = "a@example.com - amazon.com"
    flow.hass.config_entries.async_entries.return_value = [entry]
    user_input = {
        CONF_EMAIL: "a@example.com",
        CONF_URL: "amazon.com",
        CONF_PASSWORD: "pw",
    }
    await flow.async_step_user_legacy(user_input)
    assert flow.async_show_form.call_args.kwargs["errors"] == {
        CONF_EMAIL: "identifier_exists"
    }


# --------------------------------------------------------------------------- #
# async_step_proxy_warning / totp_register / process
# --------------------------------------------------------------------------- #


async def test_proxy_warning_rejected_returns_to_user():
    flow = _make_flow()
    flow.proxy_schema = OrderedDict()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_proxy_warning({CONF_PROXY_WARNING: False})
    assert flow.async_show_form.call_args.kwargs["step_id"] == "user"


async def test_proxy_warning_accepted_starts_proxy():
    flow = _make_flow()
    flow.async_step_start_proxy = AsyncMock(return_value={"type": "external"})
    result = await flow.async_step_proxy_warning({CONF_PROXY_WARNING: True})
    assert result == {"type": "external"}
    flow.async_step_start_proxy.assert_awaited_once()


async def test_totp_register_regenerates_token():
    flow = _make_flow()
    flow.login = _login_mock()
    flow.login.get_totp_token.return_value = "123456"
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_totp_register({CONF_TOTP_REGISTER: False})
    assert flow.async_show_form.call_args.kwargs["step_id"] == "totp_register"


async def test_totp_register_proceeds_to_proxy():
    flow = _make_flow()
    flow.async_step_start_proxy = AsyncMock(return_value={"type": "external"})
    result = await flow.async_step_totp_register({CONF_TOTP_REGISTER: True})
    assert result == {"type": "external"}


async def test_step_process_with_input_restarts_user():
    flow = _make_flow()
    flow.async_step_user = AsyncMock(return_value={"type": "form"})
    await flow.async_step_process("user", {CONF_EMAIL: "a@example.com"})
    flow.async_step_user.assert_awaited_once_with(user_input=None)


async def test_step_process_without_input_tests_login():
    flow = _make_flow()
    flow._test_login = AsyncMock(return_value={"type": "abort"})
    await flow.async_step_process("user", {})
    flow._test_login.assert_awaited_once()


# --------------------------------------------------------------------------- #
# async_step_reauth
# --------------------------------------------------------------------------- #


async def test_reauth_recent_login_requires_manual():
    flow = _make_flow()
    flow.login = _login_mock()
    flow.login.stats = {"login_timestamp": datetime.datetime.now()}
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_reauth({CONF_EMAIL: "a@example.com", CONF_URL: "amazon.com"})
    assert (
        flow.async_show_form.call_args.kwargs["description_placeholders"]["message"]
        == "REAUTH"
    )


@patch(_SLEEP, new_callable=AsyncMock)
async def test_reauth_old_login_attempts_automatic(_mock_sleep):
    flow = _make_flow()
    flow.async_step_user_legacy = AsyncMock(return_value={"type": "form"})
    # login is None -> seconds_since_login defaults to 60 -> automatic path
    await flow.async_step_reauth({CONF_EMAIL: "a@example.com", CONF_URL: "amazon.com"})
    _mock_sleep.assert_awaited_once()
    flow.async_step_user_legacy.assert_awaited_once()


# --------------------------------------------------------------------------- #
# proxy steps: start / check / finish
# --------------------------------------------------------------------------- #


@patch(_ALEXA_PROXY)
async def test_start_proxy_registers_views_and_external_step(mock_proxy_cls):
    flow = _make_flow()
    flow.flow_id = "flow-1"
    flow.login = _login_mock()
    flow.login._session = MagicMock()
    flow.config[CONF_HASS_URL] = "http://hass.local"
    proxy = mock_proxy_cls.return_value
    proxy.access_url.return_value = URL("http://hass.local/proxy")
    proxy.all_handler = MagicMock()
    proxy.session = MagicMock()
    flow.async_external_step = MagicMock(return_value={"type": "external"})
    result = await flow.async_step_start_proxy()
    assert result == {"type": "external"}
    assert flow.hass.http.register_view.call_count >= 1
    flow.async_external_step.assert_called_once()


async def test_check_proxy_resets_and_finishes():
    flow = _make_flow()
    flow.login = _login_mock()
    flow.proxy_view = MagicMock()
    flow.async_external_step_done = MagicMock(return_value={"type": "external_done"})
    result = await flow.async_step_check_proxy()
    flow.proxy_view.reset.assert_called_once()
    assert result == {"type": "external_done"}


async def test_finish_proxy_logged_in_tests_login():
    flow = _make_flow()
    flow.login = _login_mock()
    flow.login.password = "pw"
    flow.login.test_loggedin = AsyncMock(return_value=True)
    flow.login.finalize_login = AsyncMock()
    flow._test_login = AsyncMock(return_value={"type": "create_entry"})
    await flow.async_step_finish_proxy()
    flow.login.finalize_login.assert_awaited_once()
    flow._test_login.assert_awaited_once()


async def test_finish_proxy_not_logged_in_aborts():
    flow = _make_flow()
    flow.login = _login_mock()
    flow.login.test_loggedin = AsyncMock(return_value=False)
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    await flow.async_step_finish_proxy()
    flow.async_abort.assert_called_once_with(reason="login_failed")


# --------------------------------------------------------------------------- #
# _test_login branches (reauth-success/new-entry live in test_config_flow.py)
# --------------------------------------------------------------------------- #


@patch(_SLEEP, new_callable=AsyncMock)
async def test_test_login_securitycode_required_auto_submits(_mock_sleep):
    flow = _make_flow()
    flow.login = _login_mock(status={"securitycode_required": True})
    flow.login.get_totp_token.return_value = "123456"
    flow.async_step_user_legacy = AsyncMock(return_value={"type": "form"})
    await flow._test_login()
    flow.async_step_user_legacy.assert_awaited_once()


async def test_test_login_login_failed_aborts():
    flow = _make_flow()
    flow.login = _login_mock(status={"login_failed": True})
    flow.login.close = AsyncMock()
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    with patch(_DISMISS):
        await flow._test_login()
    flow.async_abort.assert_called_once_with(reason="login_failed")


async def test_test_login_error_message_shows_form():
    flow = _make_flow()
    flow.login = _login_mock(status={"error_message": "Some other error"})
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow._test_login()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "user"


async def test_test_login_empty_status_shows_form():
    flow = _make_flow()
    flow.login = _login_mock(status={})
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow._test_login()
    assert flow.async_show_form.call_args.kwargs["step_id"] == "user"


# --------------------------------------------------------------------------- #
# OptionsFlowHandler
# --------------------------------------------------------------------------- #


def _make_options_flow(entry_data=None):
    entry = MagicMock()
    entry.data = entry_data or {}
    entry.options = {}
    flow = OptionsFlowHandler(entry)
    flow.hass = MagicMock()
    # _config_entry_id is a property returning self.handler; config_entry resolves
    # via hass.config_entries.async_get_known_entry(self._config_entry_id).
    flow.handler = "entry-1"
    flow.hass.config_entries.async_get_known_entry.return_value = entry
    return flow, entry


async def test_options_flow_shows_form():
    flow, _entry = _make_options_flow({CONF_URL: "amazon.com"})
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_init(None)
    assert flow.async_show_form.call_args.kwargs["step_id"] == "init"


async def test_options_flow_submit_updates_entry():
    flow, _entry = _make_options_flow(
        {
            CONF_URL: "amazon.com",
            CONF_EMAIL: "a@example.com",
            CONF_PUBLIC_URL: "http://x",
            CONF_INCLUDE_DEVICES: "Echo",
        }
    )
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    user_input = {
        CONF_PUBLIC_URL: "http://x",
        CONF_INCLUDE_DEVICES: " Echo ",
        CONF_EXCLUDE_DEVICES: "",
        CONF_SCAN_INTERVAL: 120,
        CONF_QUEUE_DELAY: 1.5,
        CONF_EXTENDED_ENTITY_DISCOVERY: False,
        CONF_DEBUG: False,
    }
    result = await flow.async_step_init(user_input)
    assert result == {"type": "create_entry"}
    flow.hass.config_entries.async_update_entry.assert_called_once()
    # public_url got a trailing slash and include_devices was stripped
    assert user_input[CONF_PUBLIC_URL] == "http://x/"
    assert user_input[CONF_INCLUDE_DEVICES] == "Echo"


# --------------------------------------------------------------------------- #
# Auth views
# --------------------------------------------------------------------------- #


async def test_callback_view_success():
    view = AlexaMediaAuthorizationCallbackView()
    hass = MagicMock()
    hass.config_entries.flow.async_configure = AsyncMock()
    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"flow_id": "flow-1"}
    await view.get(request)
    hass.config_entries.flow.async_configure.assert_awaited_once()


async def test_callback_view_invalid_flow_raises_bad_request():
    view = AlexaMediaAuthorizationCallbackView()
    hass = MagicMock()
    hass.config_entries.flow.async_configure = AsyncMock(side_effect=UnknownFlow)
    request = MagicMock()
    request.app = {"hass": hass}
    request.query = {"flow_id": "flow-1"}
    with pytest.raises(HTTPBadRequest):
        await view.get(request)


def test_proxy_view_init_sets_methods_and_reset():
    handler = MagicMock()
    view = AlexaMediaAuthorizationProxyView(handler)
    assert AlexaMediaAuthorizationProxyView.handler is handler
    for method in ("get", "post", "delete", "put", "patch", "head", "options"):
        assert hasattr(view, method)
    AlexaMediaAuthorizationProxyView.known_ips = {"1.2.3.4": datetime.datetime.now()}
    AlexaMediaAuthorizationProxyView.reset()
    assert AlexaMediaAuthorizationProxyView.known_ips == {}


async def test_proxy_view_wrapped_unauthorized_without_flow_id():
    AlexaMediaAuthorizationProxyView.reset()
    view = AlexaMediaAuthorizationProxyView(AsyncMock())
    request = MagicMock()
    request.remote = "9.9.9.9"
    request.app = {"hass": MagicMock()}
    request.url.query = {}  # missing config_flow_id -> Unauthorized
    with pytest.raises(Unauthorized):
        await view.get(request)


async def test_proxy_view_wrapped_success_calls_handler():
    handler = AsyncMock(return_value=MagicMock(headers={}, status=200))
    AlexaMediaAuthorizationProxyView.reset()
    view = AlexaMediaAuthorizationProxyView(handler)
    hass = MagicMock()
    hass.config_entries.flow.async_progress.return_value = [{"flow_id": "flow-1"}]
    request = MagicMock()
    request.remote = "8.8.8.8"
    request.app = {"hass": hass}
    request.url.query = {"config_flow_id": "flow-1"}
    request.method = "GET"
    await view.get(request)
    handler.assert_awaited_once()


async def test_proxy_view_known_ip_skips_auth():
    handler = AsyncMock(return_value=MagicMock(headers={}, status=200))
    AlexaMediaAuthorizationProxyView.reset()
    AlexaMediaAuthorizationProxyView.known_ips = {"5.5.5.5": datetime.datetime.now()}
    view = AlexaMediaAuthorizationProxyView(handler)
    request = MagicMock()
    request.remote = "5.5.5.5"
    request.app = {"hass": MagicMock()}
    request.method = "GET"
    await view.get(request)
    handler.assert_awaited_once()


async def test_proxy_view_handler_connect_error_returns_response():
    handler = AsyncMock(side_effect=httpx.ConnectError("down"))
    AlexaMediaAuthorizationProxyView.reset()
    view = AlexaMediaAuthorizationProxyView(handler)
    hass = MagicMock()
    hass.config_entries.flow.async_progress.return_value = [{"flow_id": "flow-1"}]
    request = MagicMock()
    request.remote = "8.8.8.8"
    request.app = {"hass": hass}
    request.url.query = {"config_flow_id": "flow-1"}
    request.method = "GET"
    result = await view.get(request)
    assert result is not None  # a web Response, not a raised exception


async def test_proxy_view_handler_generic_error_returns_response():
    handler = AsyncMock(side_effect=RuntimeError("boom"))
    AlexaMediaAuthorizationProxyView.reset()
    view = AlexaMediaAuthorizationProxyView(handler)
    hass = MagicMock()
    hass.config_entries.flow.async_progress.return_value = [{"flow_id": "flow-1"}]
    request = MagicMock()
    request.remote = "8.8.8.8"
    request.app = {"hass": hass}
    request.url.query = {"config_flow_id": "flow-1"}
    request.method = "GET"
    result = await view.get(request)
    assert result is not None


# --------------------------------------------------------------------------- #
# async_step_user_legacy - login creation branches
# --------------------------------------------------------------------------- #


@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
async def test_user_legacy_resumes_when_status_present(mock_uuid, mock_login_cls):
    mock_uuid.return_value = {"uuid": "u"}
    login = _login_mock(status={"login_successful": True})
    mock_login_cls.return_value = login
    flow = _make_flow()
    flow._test_login = AsyncMock(return_value={"type": "create_entry"})
    await flow.async_step_user_legacy(
        {
            CONF_EMAIL: "a@example.com",
            CONF_URL: "amazon.com",
            CONF_PASSWORD: "pw",
            CONF_DEBUG: False,
        }
    )
    flow._test_login.assert_awaited_once()


@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
async def test_user_legacy_otp_register(mock_uuid, mock_login_cls):
    mock_uuid.return_value = {"uuid": "u"}
    login = _login_mock(status={})
    login.get_totp_token.return_value = "123456"
    mock_login_cls.return_value = login
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user_legacy(
        {
            CONF_EMAIL: "a@example.com",
            CONF_URL: "amazon.com",
            CONF_PASSWORD: "pw",
            CONF_DEBUG: False,
            CONF_OTPSECRET: "ABCDEFGH",
        }
    )
    assert flow.async_show_form.call_args.kwargs["step_id"] == "totp_register"


@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
async def test_user_legacy_connection_error(mock_uuid, mock_login_cls):
    mock_uuid.return_value = {"uuid": "u"}
    login = _login_mock(status={})
    login.login = AsyncMock(side_effect=AlexapyConnectionError)
    mock_login_cls.return_value = login
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user_legacy(
        {
            CONF_EMAIL: "a@example.com",
            CONF_URL: "amazon.com",
            CONF_PASSWORD: "pw",
            CONF_DEBUG: False,
        }
    )
    assert flow.async_show_form.call_args.kwargs["errors"] == {
        "base": "connection_error"
    }


@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
async def test_user_legacy_unknown_error(mock_uuid, mock_login_cls):
    mock_uuid.return_value = {"uuid": "u"}
    login = _login_mock(status={})
    login.login = AsyncMock(side_effect=RuntimeError("boom"))
    mock_login_cls.return_value = login
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user_legacy(
        {
            CONF_EMAIL: "a@example.com",
            CONF_URL: "amazon.com",
            CONF_PASSWORD: "pw",
            CONF_DEBUG: False,
        }
    )
    assert flow.async_show_form.call_args.kwargs["errors"] == {"base": "unknown_error"}


# --------------------------------------------------------------------------- #
# async_step_user - hass_url validation + OTP register
# --------------------------------------------------------------------------- #


def _patch_client_session(mock_cs, *, status=None, error=None):
    """Configure a mocked aiohttp ClientSession async context manager."""
    session = MagicMock()
    if error is not None:
        session.get.side_effect = error
    else:
        resp = MagicMock()
        resp.status = status
        get_cm = MagicMock()
        get_cm.__aenter__ = AsyncMock(return_value=resp)
        get_cm.__aexit__ = AsyncMock(return_value=False)
        session.get.return_value = get_cm
    mock_cs.return_value.__aenter__ = AsyncMock(return_value=session)
    mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)


@patch("custom_components.alexa_media.config_flow.ClientSession")
@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_hass_url_invalid_shows_proxy_warning(
    _mock_url, mock_uuid, mock_login_cls, mock_cs
):
    mock_uuid.return_value = {"uuid": "u"}
    mock_login_cls.return_value = _login_mock()
    _patch_client_session(mock_cs, error=ClientConnectionError("nope"))
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user(
        {
            CONF_EMAIL: "a@example.com",
            CONF_PASSWORD: "pw",
            CONF_URL: "amazon.com",
            CONF_DEBUG: False,
            CONF_HASS_URL: "http://bad.url",
        }
    )
    assert flow.async_show_form.call_args.kwargs["step_id"] == "proxy_warning"


@patch("custom_components.alexa_media.config_flow.ClientSession")
@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_hass_url_valid_starts_proxy(
    _mock_url, mock_uuid, mock_login_cls, mock_cs
):
    mock_uuid.return_value = {"uuid": "u"}
    mock_login_cls.return_value = _login_mock()
    _patch_client_session(mock_cs, status=200)
    flow = _make_flow()
    flow.async_step_start_proxy = AsyncMock(return_value={"type": "external"})
    await flow.async_step_user(
        {
            CONF_EMAIL: "a@example.com",
            CONF_PASSWORD: "pw",
            CONF_URL: "amazon.com",
            CONF_DEBUG: False,
            CONF_HASS_URL: "http://good.url",
        }
    )
    flow.async_step_start_proxy.assert_awaited_once()


@patch("custom_components.alexa_media.config_flow.ClientSession")
@patch(_ALEXA_LOGIN)
@patch(_CALC_UUID, new_callable=AsyncMock)
@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_with_otp_shows_totp_register(
    _mock_url, mock_uuid, mock_login_cls, mock_cs
):
    mock_uuid.return_value = {"uuid": "u"}
    login = _login_mock()
    login.get_totp_token.return_value = "123456"
    mock_login_cls.return_value = login
    _patch_client_session(mock_cs, status=200)
    flow = _make_flow()
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user(
        {
            CONF_EMAIL: "a@example.com",
            CONF_PASSWORD: "pw",
            CONF_URL: "amazon.com",
            CONF_DEBUG: False,
            CONF_HASS_URL: "http://good.url",
            CONF_OTPSECRET: "ABCDEFGH",
        }
    )
    assert flow.async_show_form.call_args.kwargs["step_id"] == "totp_register"


# --------------------------------------------------------------------------- #
# _save_user_input extra branches / _test_login new-entry pops / options secrets
# --------------------------------------------------------------------------- #


def test_save_user_input_extra_branches():
    flow = _make_flow()
    flow.config[CONF_OTPSECRET] = "existing"
    flow._save_user_input_to_config(
        {
            CONF_HASS_URL: "http://hass.local",
            CONF_OTPSECRET: "   ",  # blank after strip -> popped
            CONF_INCLUDE_DEVICES: "Echo1",  # plain string branch
            CONF_EXCLUDE_DEVICES: ["X", "Y"],  # list join branch
        }
    )
    assert flow.config[CONF_HASS_URL] == "http://hass.local"
    assert CONF_OTPSECRET not in flow.config
    assert flow.config[CONF_INCLUDE_DEVICES] == "Echo1"
    assert flow.config[CONF_EXCLUDE_DEVICES] == "X,Y"


def test_async_get_options_flow_returns_handler():
    result = AlexaMediaFlowHandler.async_get_options_flow(MagicMock())
    assert isinstance(result, OptionsFlowHandler)


async def test_test_login_new_entry_pops_transient_config():
    flow = _make_flow()
    login = _login_mock(status={"login_successful": True})
    login.access_token = "t"
    login.refresh_token = "r"
    login.expires_in = 1
    login.mac_dms = "m"
    login.code_verifier = "v"
    login.authorization_code = "c"
    flow.login = login
    flow.config[CONF_EMAIL] = "a@example.com"
    flow.config[CONF_SECURITYCODE] = "111"
    flow.config["hass_url"] = "http://x"
    flow.async_set_unique_id = AsyncMock(return_value=None)
    flow._abort_if_unique_id_configured = MagicMock()
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    await flow._test_login()
    assert CONF_SECURITYCODE not in flow.config
    assert "hass_url" not in flow.config
    flow.async_create_entry.assert_called_once()


async def test_options_flow_preserves_secrets_and_strips_devices():
    flow, _entry = _make_options_flow(
        {
            CONF_URL: "amazon.com",
            CONF_EMAIL: "a@example.com",
            CONF_PASSWORD: "pw",
            CONF_SECURITYCODE: "111",
            CONF_OTPSECRET: "otp",
            CONF_OAUTH: {"access_token": "x"},
            CONF_PUBLIC_URL: "http://x",
            CONF_INCLUDE_DEVICES: "i",
            CONF_EXCLUDE_DEVICES: "e",
        }
    )
    flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})
    user_input = {
        CONF_PUBLIC_URL: "http://x/",
        CONF_INCLUDE_DEVICES: " i ",
        CONF_EXCLUDE_DEVICES: " e ",
        CONF_SCAN_INTERVAL: 120,
        CONF_QUEUE_DELAY: 1.5,
        CONF_EXTENDED_ENTITY_DISCOVERY: False,
        CONF_DEBUG: False,
    }
    await flow.async_step_init(user_input)
    assert user_input[CONF_PASSWORD] == "pw"
    assert user_input[CONF_SECURITYCODE] == "111"
    assert user_input[CONF_OTPSECRET] == "otp"
    assert user_input[CONF_OAUTH] == {"access_token": "x"}
    assert user_input[CONF_INCLUDE_DEVICES] == "i"  # stripped
    assert user_input[CONF_EXCLUDE_DEVICES] == "e"  # stripped
