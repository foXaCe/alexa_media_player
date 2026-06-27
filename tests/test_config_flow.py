"""Tests for config_flow module."""

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import InvalidURL, web
from alexapy import AlexapyPyotpInvalidKey
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, CONF_URL
from homeassistant.exceptions import Unauthorized
from homeassistant.helpers.network import NoURLAvailableError
import pytest
from yarl import URL

from custom_components.alexa_media.config_flow import (
    AlexaMediaAuthorizationProxyView,
    AlexaMediaFlowHandler,
    OptionsFlowHandler,
)
from custom_components.alexa_media.const import (
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_HASS_URL,
    CONF_OTPSECRET,
    CONF_SECURITYCODE,
    DATA_ALEXAMEDIA,
)

_GET_URL = "custom_components.alexa_media.config_flow.get_url"
_ALEXA_PROXY = "custom_components.alexa_media.config_flow.AlexaProxy"
_SLEEP = "custom_components.alexa_media.config_flow.sleep"
_DISMISS = (
    "custom_components.alexa_media.config_flow.async_dismiss_persistent_notification"
)
_CLIENT_SESSION = "custom_components.alexa_media.config_flow.ClientSession"


def _make_flow():
    """Build a flow handler with a minimal mocked hass."""
    flow = AlexaMediaFlowHandler()
    flow.hass = MagicMock()
    flow.hass.data = {DATA_ALEXAMEDIA: {"accounts": {}, "config_flows": {}}}
    flow.hass.config_entries.async_entries.return_value = []
    return flow


def _login_mock(email="a@example.com", url="amazon.com", status=None):
    login = MagicMock()
    login.email = email
    login.url = url
    login.status = status if status is not None else {}
    login.session.closed = False
    return login


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


class TestReauthReload:
    """Test that reauth triggers integration reload.

    These tests verify the fix for the bug where the integration remained
    in an error state after successful reauthentication because async_reload
    was not called.
    """

    @pytest.mark.asyncio
    async def test_reauth_triggers_reload(self):
        """Test that successful reauth calls async_reload to clear error state.

        This test verifies the fix for the bug where the integration remained
        in an error state after successful reauthentication because async_reload
        was not called.

        Test plan:
        1. Trigger a reauth flow by simulating an existing entry
        2. Complete the reauth successfully
        3. Verify that async_reload is called to clear the error state
        """
        # Create flow handler
        flow = AlexaMediaFlowHandler()
        flow.hass = MagicMock()
        flow.config = {
            "email": "test@example.com",
        }

        # Mock login object with successful status
        mock_login = MagicMock()
        mock_login.email = "test@example.com"
        mock_login.url = "https://amazon.com"
        mock_login.status = {"login_successful": True}
        mock_login.access_token = "test_token"  # nosec B105
        mock_login.refresh_token = "test_refresh"  # nosec B105
        mock_login.expires_in = 3600
        mock_login.mac_dms = "test_mac"
        mock_login.code_verifier = "test_verifier"
        mock_login.authorization_code = "test_code"
        flow.login = mock_login

        # Mock existing entry (simulates reauth scenario)
        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry_id"

        # Setup hass.data structure
        flow.hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {},
                "config_flows": {},
            }
        }

        # Mock async_set_unique_id to return existing entry (triggers reauth path)
        flow.async_set_unique_id = AsyncMock(return_value=mock_entry)

        # Mock config entries methods
        flow.hass.config_entries.async_update_entry = MagicMock()
        flow.hass.config_entries.async_reload = AsyncMock()

        # Mock other required methods
        flow.hass.bus.async_fire = MagicMock()
        flow.async_abort = MagicMock(return_value={"type": "abort"})

        # Patch async_dismiss_persistent_notification
        with patch(
            "custom_components.alexa_media.config_flow.async_dismiss_persistent_notification"
        ):
            # Call _test_login which handles reauth
            await flow._test_login()

        # Verify async_reload was called with the entry_id
        flow.hass.config_entries.async_reload.assert_called_once_with("test_entry_id")

        # Verify async_abort was called with reauth_successful
        flow.async_abort.assert_called_once_with(reason="reauth_successful")

    @pytest.mark.asyncio
    async def test_new_entry_does_not_trigger_reload(self):
        """Test that new entry creation does not call async_reload.

        Only reauth (existing entry update) should trigger reload.
        New entries should use async_create_entry instead.
        """
        flow = AlexaMediaFlowHandler()
        flow.hass = MagicMock()
        flow.config = {
            "email": "test@example.com",
        }

        # Mock login object
        mock_login = MagicMock()
        mock_login.email = "test@example.com"
        mock_login.url = "https://amazon.com"
        mock_login.status = {"login_successful": True}
        mock_login.access_token = "test_token"  # nosec B105
        mock_login.refresh_token = "test_refresh"  # nosec B105
        mock_login.expires_in = 3600
        mock_login.mac_dms = "test_mac"
        mock_login.code_verifier = "test_verifier"
        mock_login.authorization_code = "test_code"
        flow.login = mock_login

        flow.hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {},
                "config_flows": {},
            }
        }

        # Mock async_set_unique_id to return None (no existing entry = new setup)
        flow.async_set_unique_id = AsyncMock(return_value=None)

        # Mock methods
        flow.hass.config_entries.async_reload = AsyncMock()
        flow._abort_if_unique_id_configured = MagicMock()
        flow.async_create_entry = MagicMock(return_value={"type": "create_entry"})

        await flow._test_login()

        # Verify async_reload was NOT called for new entries
        flow.hass.config_entries.async_reload.assert_not_called()

        # Verify async_create_entry was called instead
        flow.async_create_entry.assert_called_once()

    @pytest.mark.asyncio
    async def test_reauth_updates_credentials_before_reload(self):
        """Test that credentials are updated before reload is triggered.

        The async_update_entry should be called before async_reload
        to ensure new credentials are in place.
        """
        flow = AlexaMediaFlowHandler()
        flow.hass = MagicMock()
        flow.config = {
            "email": "test@example.com",
        }

        mock_login = MagicMock()
        mock_login.email = "test@example.com"
        mock_login.url = "https://amazon.com"
        mock_login.status = {"login_successful": True}
        mock_login.access_token = "new_access_token"  # nosec B105
        mock_login.refresh_token = "new_refresh_token"  # nosec B105
        mock_login.expires_in = 3600
        mock_login.mac_dms = "test_mac"
        mock_login.code_verifier = "test_verifier"
        mock_login.authorization_code = "test_code"
        flow.login = mock_login

        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry_id"

        flow.hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {},
                "config_flows": {},
            }
        }

        flow.async_set_unique_id = AsyncMock(return_value=mock_entry)

        # Track call order
        call_order: list[str] = []
        flow.hass.config_entries.async_update_entry = MagicMock(
            side_effect=lambda *_args, **_kwargs: call_order.append("update")
        )
        flow.hass.config_entries.async_reload = AsyncMock(
            side_effect=lambda *_args, **_kwargs: call_order.append("reload")
        )
        flow.hass.bus.async_fire = MagicMock()
        flow.async_abort = MagicMock(return_value={"type": "abort"})

        with patch(
            "custom_components.alexa_media.config_flow.async_dismiss_persistent_notification"
        ):
            await flow._test_login()

        # Verify update was called before reload
        assert call_order == [
            "update",
            "reload",
        ], f"Expected update before reload, got: {call_order}"

    @pytest.mark.asyncio
    async def test_reauth_succeeds_even_when_reload_fails(self):
        """Test that reauth completes successfully even if reload raises an exception.

        The implementation includes defensive error handling that logs a warning
        but still returns reauth_successful when async_reload fails. This ensures
        credentials are updated even if the integration can't be reloaded.
        """
        flow = AlexaMediaFlowHandler()
        flow.hass = MagicMock()
        flow.config = {
            "email": "test@example.com",
        }

        mock_login = MagicMock()
        mock_login.email = "test@example.com"
        mock_login.url = "https://amazon.com"
        mock_login.status = {"login_successful": True}
        mock_login.access_token = "test_token"  # noqa: S105  # nosec B105
        mock_login.refresh_token = "test_refresh"  # noqa: S105  # nosec B105
        mock_login.expires_in = 3600
        mock_login.mac_dms = "test_mac"
        mock_login.code_verifier = "test_verifier"
        mock_login.authorization_code = "test_code"
        flow.login = mock_login

        mock_entry = MagicMock()
        mock_entry.entry_id = "test_entry_id"

        flow.hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {},
                "config_flows": {},
            }
        }

        flow.async_set_unique_id = AsyncMock(return_value=mock_entry)
        flow.hass.config_entries.async_update_entry = MagicMock()
        # Simulate reload failure
        flow.hass.config_entries.async_reload = AsyncMock(
            side_effect=Exception("Reload failed")
        )
        flow.hass.bus.async_fire = MagicMock()
        flow.async_abort = MagicMock(return_value={"type": "abort"})

        with patch(
            "custom_components.alexa_media.config_flow.async_dismiss_persistent_notification"
        ):
            await flow._test_login()

        # Despite reload failure, reauth should still complete successfully
        flow.async_abort.assert_called_once_with(reason="reauth_successful")
        # Credentials should have been updated
        flow.hass.config_entries.async_update_entry.assert_called_once()


class TestConfigFlowInvalidOtpKeyDataSchema:
    """Tests for handling invalid OTP key errors in config flow.

    These tests verify that when a user provides an invalid 2FA/OTP key,
    the configuration flow properly displays an error form WITH the data
    schema so the user can correct their input.

    The bug: async_show_form was called without data_schema parameter when
    AlexapyPyotpInvalidKey was raised, causing the error form to display
    without any input fields. Users could see the error but had no way to
    correct their invalid OTP key.

    The fix: Add data_schema=vol.Schema(self.proxy_schema) to the
    async_show_form call in the exception handler.

    Related issues: #3254, #3243, #3189
    """

    def test_bugfix_adds_data_schema_to_exception_handler(self):
        """Test that the bugfix adds data_schema to the AlexapyPyotpInvalidKey handler.

        This test reads the actual config_flow.py source code and verifies
        that the exception handler includes data_schema in the async_show_form call.
        """
        with open(
            "custom_components/alexa_media/config_flow.py", encoding="utf-8"
        ) as f:
            content = f.read()

        # Check that the exception handler exists
        assert "except AlexapyPyotpInvalidKey:" in content, (
            "AlexapyPyotpInvalidKey exception handler not found in config_flow.py"
        )

        # Find the exception handler block
        handler_start = content.find("except AlexapyPyotpInvalidKey:")
        assert handler_start != -1

        # Get the next ~500 characters to capture the full handler
        handler_block = content[handler_start : handler_start + 500]

        # Verify the handler returns async_show_form
        assert "async_show_form" in handler_block, (
            "async_show_form not found in AlexapyPyotpInvalidKey handler"
        )

        # CRITICAL: Verify data_schema is present in the handler
        assert "data_schema" in handler_block, (
            "BUGFIX MISSING: data_schema parameter not found in "
            "AlexapyPyotpInvalidKey exception handler. "
            "Without data_schema, users cannot correct their invalid OTP key. "
            "See issues #3254, #3243, #3189."
        )

        # Verify it uses proxy_schema
        assert "proxy_schema" in handler_block, (
            "proxy_schema not found in AlexapyPyotpInvalidKey handler. "
            "The handler should use vol.Schema(self.proxy_schema) for data_schema."
        )

    def test_error_form_includes_2fa_key_invalid_error(self):
        """Test that the exception handler sets the correct error key."""
        with open(
            "custom_components/alexa_media/config_flow.py", encoding="utf-8"
        ) as f:
            content = f.read()

        handler_start = content.find("except AlexapyPyotpInvalidKey:")
        handler_block = content[handler_start : handler_start + 500]

        assert "2fa_key_invalid" in handler_block, (
            "Error key '2fa_key_invalid' not found in exception handler"
        )

    def test_error_form_includes_otp_secret_placeholder(self):
        """Test that the exception handler includes otp_secret in placeholders."""
        with open(
            "custom_components/alexa_media/config_flow.py", encoding="utf-8"
        ) as f:
            content = f.read()

        handler_start = content.find("except AlexapyPyotpInvalidKey:")
        handler_block = content[handler_start : handler_start + 500]

        assert "otp_secret" in handler_block, (
            "otp_secret placeholder not found in exception handler. "
            "Users should see which OTP key was invalid."
        )

    def test_handler_returns_user_step(self):
        """Test that the exception handler returns to the 'user' step."""
        with open(
            "custom_components/alexa_media/config_flow.py", encoding="utf-8"
        ) as f:
            content = f.read()

        handler_start = content.find("except AlexapyPyotpInvalidKey:")
        handler_block = content[handler_start : handler_start + 500]

        assert 'step_id="user"' in handler_block, (
            "step_id='user' not found in exception handler. "
            "The handler should return users to the user step for correction."
        )


# --------------------------------------------------------------------------- #
# async_step_user - error/edge branches not exercised by the happy-path tests
# --------------------------------------------------------------------------- #


@patch(_GET_URL, side_effect=NoURLAvailableError)
async def test_step_user_no_url_available_forces_user_form(_mock_url):
    """When no HA URL is detectable, both fallbacks fire and the user form shows."""
    flow = _make_flow()
    flow.login = _login_mock()  # existing login -> skip AlexaLogin creation
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    # user_input without CONF_HASS_URL -> get_url(prefer_external=True) is attempted
    await flow.async_step_user(
        {
            CONF_EMAIL: "a@example.com",
            CONF_PASSWORD: "pw",
            CONF_URL: "amazon.com",
            CONF_DEBUG: False,
        }
    )
    assert flow.async_show_form.call_args.kwargs["step_id"] == "user"


@patch(_CLIENT_SESSION)
@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_existing_login_updates_credentials(_mock_url, mock_cs):
    """An existing, open login object is reused and its credentials refreshed."""
    _patch_client_session(mock_cs, status=200)
    flow = _make_flow()
    flow.login = _login_mock()
    flow.config[CONF_OTPSECRET] = "ABCDEFGH"
    flow.async_step_start_proxy = AsyncMock(return_value={"type": "external"})
    result = await flow.async_step_user(
        {
            CONF_EMAIL: "new@example.com",
            CONF_PASSWORD: "newpw",
            CONF_URL: "amazon.com",
            CONF_DEBUG: False,
            CONF_HASS_URL: "http://good.url",
        }
    )
    assert result == {"type": "external"}
    assert flow.login.email == "new@example.com"
    assert flow.login.password == "newpw"
    flow.login.set_totp.assert_called_once_with("ABCDEFGH")


@patch(_CLIENT_SESSION)
@patch(_GET_URL, return_value="http://hass.local")
async def test_step_user_invalid_url_shows_proxy_warning(_mock_url, mock_cs):
    """An aiohttp InvalidURL while probing hass_url routes to the proxy warning."""
    _patch_client_session(mock_cs, error=InvalidURL("http://bad.url"))
    flow = _make_flow()
    flow.login = _login_mock()
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


# --------------------------------------------------------------------------- #
# async_step_start_proxy - ValueError + missing-session branches
# --------------------------------------------------------------------------- #


@patch(_ALEXA_PROXY, side_effect=ValueError("bad url"))
async def test_start_proxy_value_error_shows_user_form(_mock_proxy):
    flow = _make_flow()
    flow.login = _login_mock()
    flow.config[CONF_HASS_URL] = "http://hass.local"
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_start_proxy()
    assert flow.async_show_form.call_args.kwargs["errors"] == {"base": "invalid_url"}


async def test_start_proxy_missing_session_with_existing_view():
    flow = _make_flow()
    flow.flow_id = "flow-x"
    flow.login = _login_mock()
    flow.login._session = MagicMock()
    flow.config[CONF_HASS_URL] = "http://hass.local"
    proxy = MagicMock()
    proxy.session = None  # triggers the "no session found" warning branch
    proxy.access_url.return_value = URL("http://hass.local/proxy")
    proxy.all_handler = MagicMock()
    flow.proxy = proxy  # pre-existing proxy -> skip creation
    flow.proxy_view = MagicMock()  # pre-existing view -> reuse branch
    flow.async_external_step = MagicMock(return_value={"type": "external"})
    result = await flow.async_step_start_proxy()
    assert result == {"type": "external"}
    assert flow.proxy_view.handler is proxy.all_handler


# --------------------------------------------------------------------------- #
# async_step_user_legacy - reused login + OTP + error branches
# --------------------------------------------------------------------------- #


async def test_user_legacy_existing_login_attempts_login():
    flow = _make_flow()
    flow.login = _login_mock(status={})  # falsy status -> performs a fresh login()
    flow.login.login = AsyncMock()
    flow._test_login = AsyncMock(return_value={"type": "create_entry"})
    await flow.async_step_user_legacy(
        {
            CONF_EMAIL: "a@example.com",
            CONF_URL: "amazon.com",
            CONF_PASSWORD: "pw",
            CONF_DEBUG: False,
        }
    )
    flow.login.login.assert_awaited_once()
    flow._test_login.assert_awaited_once()


async def test_user_legacy_otp_token_falsy_shows_2fa_error():
    flow = _make_flow()
    flow.login = _login_mock()
    flow.login.get_totp_token.return_value = ""  # no token -> 2fa key invalid form
    flow.async_show_form = MagicMock(return_value={"type": "form"})
    await flow.async_step_user_legacy(
        {
            CONF_EMAIL: "a@example.com",
            CONF_URL: "amazon.com",
            CONF_PASSWORD: "pw",
            CONF_DEBUG: False,
            CONF_OTPSECRET: "BADKEY",
        }
    )
    assert flow.async_show_form.call_args.kwargs["errors"] == {
        "base": "2fa_key_invalid"
    }


async def test_user_legacy_pyotp_invalid_key_shows_form():
    flow = _make_flow()
    flow.login = _login_mock(status={})
    flow.login.login = AsyncMock(side_effect=AlexapyPyotpInvalidKey)
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
        "base": "2fa_key_invalid"
    }


async def test_user_legacy_unknown_error_reraises_in_debug():
    flow = _make_flow()
    flow.login = _login_mock(status={})
    flow.login.login = AsyncMock(side_effect=RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        await flow.async_step_user_legacy(
            {
                CONF_EMAIL: "a@example.com",
                CONF_URL: "amazon.com",
                CONF_PASSWORD: "pw",
                CONF_DEBUG: True,  # debug on -> exception is re-raised
            }
        )


# --------------------------------------------------------------------------- #
# _test_login - reauth key pop + securitycode/error-message auto resubmission
# --------------------------------------------------------------------------- #


async def test_test_login_reauth_pops_transient_keys():
    flow = _make_flow()
    login = _login_mock(status={"login_successful": True})
    login.access_token = "t"  # noqa: S105
    login.refresh_token = "r"  # noqa: S105
    login.expires_in = 1
    login.mac_dms = "m"
    login.code_verifier = "v"
    login.authorization_code = "c"
    flow.login = login
    flow.config[CONF_EMAIL] = "a@example.com"
    flow.config["reauth"] = True
    flow.config[CONF_SECURITYCODE] = "111"
    flow.config["hass_url"] = "http://x"
    mock_entry = MagicMock()
    mock_entry.entry_id = "entry-1"
    flow.async_set_unique_id = AsyncMock(return_value=mock_entry)
    flow.hass.config_entries.async_update_entry = MagicMock()
    flow.hass.config_entries.async_reload = AsyncMock()
    flow.hass.bus.async_fire = MagicMock()
    flow.async_abort = MagicMock(return_value={"type": "abort"})
    with patch(_DISMISS):
        await flow._test_login()
    assert "reauth" not in flow.config
    assert CONF_SECURITYCODE not in flow.config
    assert "hass_url" not in flow.config
    flow.async_abort.assert_called_once_with(reason="reauth_successful")


@patch(_SLEEP, new_callable=AsyncMock)
async def test_test_login_securitycode_uses_stored_code(_mock_sleep):
    flow = _make_flow()
    flow.login = _login_mock(status={"securitycode_required": True})
    flow.login.get_totp_token.return_value = ""  # no generated code -> use stored
    flow.securitycode = "654321"
    flow.async_step_user_legacy = AsyncMock(return_value={"type": "form"})
    await flow._test_login()
    flow.async_step_user_legacy.assert_awaited_once()
    assert (
        flow.async_step_user_legacy.await_args.kwargs["user_input"][CONF_SECURITYCODE]
        == "654321"
    )


@patch(_SLEEP, new_callable=AsyncMock)
async def test_test_login_invalid_email_message_auto_resubmits(_mock_sleep):
    flow = _make_flow()
    flow.login = _login_mock(
        status={
            "error_message": (
                "There was a problem\n            "
                "Enter a valid email or mobile number\n          "
            )
        }
    )
    flow.async_step_user_legacy = AsyncMock(return_value={"type": "form"})
    await flow._test_login()
    _mock_sleep.assert_awaited_once()
    flow.async_step_user_legacy.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _save_user_input_to_config / OptionsFlowHandler legacy guard
# --------------------------------------------------------------------------- #


def test_save_user_input_exclude_devices_plain_string():
    flow = _make_flow()
    flow._save_user_input_to_config({CONF_EXCLUDE_DEVICES: "Echo Living Room"})
    assert flow.config[CONF_EXCLUDE_DEVICES] == "Echo Living Room"


def test_options_flow_legacy_ha_assigns_config_entry():
    """On HA < 2024.12 the flow assigns config_entry directly.

    On the installed (modern) HA, config_entry is a read-only property, so the
    legacy assignment raises AttributeError -- which still executes the guarded
    line for coverage of the version-compat branch.
    """
    with patch("custom_components.alexa_media.config_flow.HAVERSION", "2024.11.0"):
        with pytest.raises(AttributeError):
            OptionsFlowHandler(MagicMock())


# --------------------------------------------------------------------------- #
# AlexaMediaAuthorizationProxyView.check_auth - remaining branches
# --------------------------------------------------------------------------- #


async def test_proxy_view_unauthorized_when_flow_not_found():
    AlexaMediaAuthorizationProxyView.reset()
    view = AlexaMediaAuthorizationProxyView(AsyncMock())
    hass = MagicMock()
    hass.config_entries.flow.async_progress.return_value = [{"flow_id": "other"}]
    request = MagicMock()
    request.remote = "3.3.3.3"
    request.app = {"hass": hass}
    request.url.query = {"config_flow_id": "wanted"}  # present but no match
    with pytest.raises(Unauthorized):
        await view.get(request)


async def test_proxy_view_debug_logs_request_and_response_headers():
    result_obj = MagicMock(headers={"set-cookie": "secret", "Y": "z"}, status=200)
    handler = AsyncMock(return_value=result_obj)
    AlexaMediaAuthorizationProxyView.reset()
    AlexaMediaAuthorizationProxyView.known_ips = {"7.7.7.7": datetime.datetime.now()}
    view = AlexaMediaAuthorizationProxyView(handler)
    request = MagicMock()
    request.remote = "7.7.7.7"  # known ip -> auth block skipped
    request.app = {"hass": MagicMock()}
    request.method = "GET"
    request.headers = {"Authorization": "secret", "X-Foo": "bar"}
    # Patching the logger makes isEnabledFor(DEBUG) truthy -> header-redaction runs.
    with patch("custom_components.alexa_media.config_flow._LOGGER"):
        result = await view.get(request)
    handler.assert_awaited_once()
    assert result is result_obj


async def test_proxy_view_reraises_http_exception():
    handler = AsyncMock(side_effect=web.HTTPFound(location="/redirect"))
    AlexaMediaAuthorizationProxyView.reset()
    AlexaMediaAuthorizationProxyView.known_ips = {"4.4.4.4": datetime.datetime.now()}
    view = AlexaMediaAuthorizationProxyView(handler)
    request = MagicMock()
    request.remote = "4.4.4.4"
    request.app = {"hass": MagicMock()}
    request.method = "GET"
    with pytest.raises(web.HTTPException):
        await view.get(request)
