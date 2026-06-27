"""Tests for helpers module.

Tests the helper functions using pytest-homeassistant-custom-component.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyLoginCloseRequested, AlexapyLoginError
from alexapy.alexalogin import AlexaLogin
from homeassistant.const import CONF_EMAIL, CONF_URL
from homeassistant.exceptions import ConditionErrorMessage
import pytest

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.helpers import (
    _catch_login_errors,
    _coerce_filter,
    _entity_backed_device_identifiers,
    _entity_backed_serials,
    _existing_serials,
    _network_allowed,
    _norm_filter_token,
    add_devices,
    alarm_just_dismissed,
    calculate_uuid,
    is_http2_enabled,
    redact_sensitive,
    report_relogin_required,
    retry_async,
    safe_get,
)

# =============================================================================
# Tests for _existing_serials function
# =============================================================================


def test_existing_serials_no_accounts():
    """Test _existing_serials returns empty list when no accounts data."""
    hass = MagicMock()
    login_obj = MagicMock()
    login_obj.email = "test@example.com"
    hass.data = {}

    result = _existing_serials(hass, login_obj)
    assert result == []


def test_existing_serials_no_email():
    """Test _existing_serials returns empty list when email not in accounts."""
    hass = MagicMock()
    login_obj = MagicMock()
    login_obj.email = "test@example.com"
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {}}}

    result = _existing_serials(hass, login_obj)
    assert result == []


def test_existing_serials_with_devices():
    """Test _existing_serials returns device serials."""
    hass = MagicMock()
    login_obj = MagicMock()
    email = "test@example.com"
    login_obj.email = email

    hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {
                email: {
                    "entities": {"media_player": {"device1": {}, "device2": {}}},
                    "devices": {"media_player": {"device1": {}, "device2": {}}},
                }
            }
        }
    }

    result = _existing_serials(hass, login_obj)
    assert sorted(result) == ["device1", "device2"]


def test_existing_serials_with_app_devices():
    """Test _existing_serials includes app device serial numbers."""
    hass = MagicMock()
    login_obj = MagicMock()
    email = "test@example.com"
    login_obj.email = email

    hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {
                email: {
                    "entities": {"media_player": {"device1": {}}},
                    "devices": {
                        "media_player": {
                            "device1": {
                                "appDeviceList": [
                                    {"serialNumber": "app1"},
                                    {"serialNumber": "app2"},
                                    {"serialNumber": "device1"},
                                ]
                            }
                        }
                    },
                }
            }
        }
    }

    result = _existing_serials(hass, login_obj)
    assert sorted(result) == ["app1", "app2", "device1", "device1"]


def test_existing_serials_with_invalid_app_devices():
    """Test _existing_serials handles app devices without serialNumber."""
    hass = MagicMock()
    login_obj = MagicMock()
    email = "test@example.com"
    login_obj.email = email

    hass.data = {
        DATA_ALEXAMEDIA: {
            "accounts": {
                email: {
                    "entities": {"media_player": {"device1": {}}},
                    "devices": {
                        "media_player": {
                            "device1": {
                                "appDeviceList": [
                                    {"invalid": "data"},
                                    {"serialNumber": "app1"},
                                ]
                            }
                        }
                    },
                }
            }
        }
    }

    result = _existing_serials(hass, login_obj)
    assert sorted(result) == ["app1", "device1"]


# =============================================================================
# Tests for add_devices function
# =============================================================================


class TestAddDevices:
    """Test the add_devices function."""

    @pytest.mark.asyncio
    async def test_add_devices_success(self):
        """Test successful device addition."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        devices = [device1, device2]

        add_devices_callback = MagicMock()

        result = await add_devices("test_account", devices, add_devices_callback)

        assert result is True
        add_devices_callback.assert_called_once_with(devices, False)

    @pytest.mark.asyncio
    async def test_add_devices_empty_list(self):
        """Test adding empty device list returns True without calling callback."""
        devices = []
        add_devices_callback = MagicMock()

        result = await add_devices("test_account", devices, add_devices_callback)

        assert result is True
        add_devices_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_devices_with_include_filter(self):
        """Test device filtering with include filter."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        devices = [device1, device2]

        add_devices_callback = MagicMock()
        include_filter = ["Device 1"]

        result = await add_devices(
            "test_account", devices, add_devices_callback, include_filter=include_filter
        )

        assert result is True
        add_devices_callback.assert_called_once_with([device1], False)

    @pytest.mark.asyncio
    async def test_add_devices_with_exclude_filter(self):
        """Test device filtering with exclude filter."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        devices = [device1, device2]

        add_devices_callback = MagicMock()
        exclude_filter = ["Device 2"]

        result = await add_devices(
            "test_account", devices, add_devices_callback, exclude_filter=exclude_filter
        )

        assert result is True
        add_devices_callback.assert_called_once_with([device1], False)

    @pytest.mark.asyncio
    async def test_add_devices_filtered_to_empty(self):
        """Test when filtering results in no devices to add."""
        device1 = MagicMock()
        device1.name = "Device 1"
        devices = [device1]

        add_devices_callback = MagicMock()
        exclude_filter = ["Device 1"]

        result = await add_devices(
            "test_account", devices, add_devices_callback, exclude_filter=exclude_filter
        )

        assert result is True
        add_devices_callback.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_devices_include_and_exclude_filters(self):
        """Test device filtering with both include and exclude filters."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        device3 = MagicMock()
        device3.name = "Device 3"
        devices = [device1, device2, device3]

        add_devices_callback = MagicMock()
        include_filter = ["Device 1", "Device 2"]
        exclude_filter = ["Device 2"]

        result = await add_devices(
            "test_account",
            devices,
            add_devices_callback,
            include_filter=include_filter,
            exclude_filter=exclude_filter,
        )

        assert result is True
        add_devices_callback.assert_called_once_with([device1, device2], False)


class TestAddDevicesFilterDefaults:
    """Regression tests for add_devices filter default value handling.

    These tests verify the fix for the "[] or x" logic bug where the incorrect
    pattern "[] or include_filter" would return None when include_filter=None,
    instead of properly defaulting to an empty list.
    """

    @pytest.mark.asyncio
    async def test_add_devices_with_none_include_filter_adds_all_devices(self):
        """Test that None include_filter defaults to empty list and adds all devices."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        devices = [device1, device2]

        add_devices_callback = MagicMock()

        result = await add_devices(
            "test_account", devices, add_devices_callback, include_filter=None
        )

        assert result is True
        add_devices_callback.assert_called_once_with(devices, False)

    @pytest.mark.asyncio
    async def test_add_devices_with_none_exclude_filter_adds_all_devices(self):
        """Test that None exclude_filter defaults to empty list and adds all devices."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        devices = [device1, device2]

        add_devices_callback = MagicMock()

        result = await add_devices(
            "test_account", devices, add_devices_callback, exclude_filter=None
        )

        assert result is True
        add_devices_callback.assert_called_once_with(devices, False)

    @pytest.mark.asyncio
    async def test_add_devices_with_both_filters_none_adds_all_devices(self):
        """Test that both filters being None adds all devices without filtering."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        device3 = MagicMock()
        device3.name = "Device 3"
        devices = [device1, device2, device3]

        add_devices_callback = MagicMock()

        result = await add_devices(
            "test_account",
            devices,
            add_devices_callback,
            include_filter=None,
            exclude_filter=None,
        )

        assert result is True
        add_devices_callback.assert_called_once_with(devices, False)

    @pytest.mark.asyncio
    async def test_add_devices_none_include_with_explicit_exclude(self):
        """Test that None include_filter with explicit exclude_filter works."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        device3 = MagicMock()
        device3.name = "Device 3"
        devices = [device1, device2, device3]

        add_devices_callback = MagicMock()

        result = await add_devices(
            "test_account",
            devices,
            add_devices_callback,
            include_filter=None,
            exclude_filter=["Device 2"],
        )

        assert result is True
        add_devices_callback.assert_called_once_with([device1, device3], False)

    @pytest.mark.asyncio
    async def test_add_devices_explicit_include_with_none_exclude(self):
        """Test that explicit include_filter with None exclude_filter works."""
        device1 = MagicMock()
        device1.name = "Device 1"
        device2 = MagicMock()
        device2.name = "Device 2"
        device3 = MagicMock()
        device3.name = "Device 3"
        devices = [device1, device2, device3]

        add_devices_callback = MagicMock()

        result = await add_devices(
            "test_account",
            devices,
            add_devices_callback,
            include_filter=["Device 1", "Device 3"],
            exclude_filter=None,
        )

        assert result is True
        add_devices_callback.assert_called_once_with([device1, device3], False)


# =============================================================================
# Tests for is_http2_enabled function
# =============================================================================


def make_hass_data_http2(data: dict | None):
    """Return a hass-like mock object with a data attribute."""
    if data is None:
        return None
    hass = MagicMock()
    hass.data = data
    return hass


def test_is_http2_enabled_hass_none():
    """Test that is_http2_enabled returns False when hass is None."""
    assert is_http2_enabled(None, "test@example.com") is False


def test_is_http2_enabled_http2_none():
    """Test that http2 set to None results in a False return value."""
    hass = make_hass_data_http2(
        {DATA_ALEXAMEDIA: {"accounts": {"test@example.com": {"http2": None}}}}
    )

    assert is_http2_enabled(hass, "test@example.com") is False


def test_is_http2_enabled_http2_object():
    """Test that a non-None http2 object results in a True return value."""
    mock_http2_client = MagicMock()

    hass = make_hass_data_http2(
        {
            DATA_ALEXAMEDIA: {
                "accounts": {"test@example.com": {"http2": mock_http2_client}}
            }
        }
    )

    assert is_http2_enabled(hass, "test@example.com") is True


# =============================================================================
# Tests for safe_get function
# =============================================================================


def test_safe_get_simple_path():
    """Test that simple path list is correctly joined with dots."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = "test@example.com"

        result = safe_get({"config": {}}, ["config", "email"])

        mock_dictor.assert_called_once()
        args = mock_dictor.call_args[0]
        assert args[1] == "config.email"
        assert result == "test@example.com"


def test_safe_get_escapes_dots_in_keys():
    """Test that dots in key names are properly escaped."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = "test@example.com"

        result = safe_get({}, ["config", "user.email"])

        args = mock_dictor.call_args[0]
        assert args[1] == "config.user\\.email"
        assert result == "test@example.com"


def test_safe_get_multiple_dots_in_key():
    """Test that multiple dots in a single key are all escaped."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        safe_get({}, ["config", "user.email.primary"])

        args = mock_dictor.call_args[0]
        assert args[1] == "config.user\\.email\\.primary"


def test_safe_get_integer_path_segment():
    """Test that integer path segments are converted to strings."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        safe_get({}, ["items", 0, "name"])

        args = mock_dictor.call_args[0]
        assert args[1] == "items.0.name"


def test_safe_get_forwards_default_value():
    """Test that default value is forwarded as positional arg."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = "default@example.com"

        safe_get({}, ["config", "email"], "default@example.com")

        args = mock_dictor.call_args[0]
        assert len(args) == 3
        assert args[2] == "default@example.com"


def test_safe_get_forwards_kwargs():
    """Test that kwargs are forwarded to dictor."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        safe_get({}, ["config", "email"], ignorecase=True, checknone=False)

        kwargs = mock_dictor.call_args[1]
        assert kwargs["ignorecase"] is True
        assert kwargs["checknone"] is False


def test_safe_get_empty_path_raises():
    """Test that empty path_list raises ValueError."""
    with pytest.raises(ValueError) as exc:
        safe_get({}, [])
    assert "path_list cannot be empty" in str(exc.value)


def test_safe_get_pathsep_kwarg_removed():
    """Test that pathsep kwarg is removed before calling dictor."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        safe_get({}, ["key"], pathsep="/")

        kwargs = mock_dictor.call_args[1]
        assert "pathsep" not in kwargs


def test_safe_get_type_match_returns_value():
    """Test that matching types pass through correctly."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = "actual_value"
        result = safe_get({}, ["key"], "default")
        assert result == "actual_value"

        mock_dictor.return_value = [1, 2, 3]
        result = safe_get({}, ["key"], [])
        assert result == [1, 2, 3]

        mock_dictor.return_value = {"a": 1}
        result = safe_get({}, ["key"], {})
        assert result == {"a": 1}

        mock_dictor.return_value = 42
        result = safe_get({}, ["key"], 0)
        assert result == 42


def test_safe_get_type_mismatch_returns_default():
    """Test that type mismatches return the default value."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = 123
        result = safe_get({}, ["key"], "default")
        assert result == "default"

        mock_dictor.return_value = {"a": 1}
        result = safe_get({}, ["key"], [])
        assert result == []

        mock_dictor.return_value = "string"
        result = safe_get({}, ["key"], {})
        assert result == {}

        mock_dictor.return_value = "123"
        result = safe_get({}, ["key"], 0)
        assert result == 0


def test_safe_get_none_result_with_default():
    """Test that None results are returned as-is (no type check)."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = None

        result = safe_get({}, ["key"], "default")
        assert result is None

        result = safe_get({}, ["key"], [])
        assert result is None

        result = safe_get({}, ["key"], {})
        assert result is None


def test_safe_get_no_default_no_type_check():
    """Test that without a default, no type checking occurs."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = "string"
        result = safe_get({}, ["key"])
        assert result == "string"

        mock_dictor.return_value = 123
        result = safe_get({}, ["key"])
        assert result == 123

        mock_dictor.return_value = [1, 2, 3]
        result = safe_get({}, ["key"])
        assert result == [1, 2, 3]


def test_safe_get_none_default_no_type_check():
    """Test that None as default doesn't trigger type checking."""
    with patch("custom_components.alexa_media.helpers.dictor") as mock_dictor:
        mock_dictor.return_value = "string"
        result = safe_get({}, ["key"], None)
        assert result == "string"

        mock_dictor.return_value = 123
        result = safe_get({}, ["key"], None)
        assert result == 123


# =============================================================================
# Tests for redact_sensitive (credential hygiene in logs)
# =============================================================================


def _sample_account():
    """Return a config-entry-like mapping with OAuth secrets."""
    return {
        "email": "foxace66@gmail.com",
        "password": "hunter2hunter",
        "url": "amazon.fr",
        "oauth": {
            "access_token": "Atna|ACCESSTOKENVALUE1234567890",
            "refresh_token": "Atnr|REFRESHTOKENVALUE1234567890",
            "authorization_code": "ANZtjnmHetApoRIWCbPSDVut",
            "code_verifier": "Wx8UKjB5WDsQVTFSLxNeh0sOXCrlzjwDo4iz5gUz4nI",
            "mac_dms": {
                "adp_token": "{enc:SUPERSECRETADPTOKEN}",
                "device_private_key": "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKg",
            },
        },
    }


def test_redact_sensitive_masks_all_oauth_secrets():
    """No usable OAuth credential should survive redaction, even nested."""
    account = _sample_account()
    secrets = (
        "hunter2hunter",
        "Atna|ACCESSTOKENVALUE1234567890",
        "Atnr|REFRESHTOKENVALUE1234567890",
        "ANZtjnmHetApoRIWCbPSDVut",
        "Wx8UKjB5WDsQVTFSLxNeh0sOXCrlzjwDo4iz5gUz4nI",
        "{enc:SUPERSECRETADPTOKEN}",
        "MIIEvgIBADANBgkqhkiG9w0BAQEFAASCBKg",
    )

    rendered = str(redact_sensitive(account))

    for secret in secrets:
        assert secret not in rendered, f"leaked secret: {secret}"


def test_redact_sensitive_keeps_email_debuggable_and_no_mutation():
    """Email stays partially visible for debugging; the input is not mutated."""
    account = _sample_account()
    redacted = redact_sensitive(account)

    # Full email is never exposed, but it is not fully wiped either.
    assert account["email"] not in str(redacted)
    assert redacted["email"] != account["email"]
    assert redacted["email"]  # not empty

    # mac_dms (adp_token + RSA private key) is redacted wholesale.
    assert redacted["oauth"]["mac_dms"] == "**REDACTED**"

    # The original mapping is untouched.
    assert account["password"] == "hunter2hunter"
    assert account["oauth"]["mac_dms"]["device_private_key"].startswith("MIIEvg")


def test_redact_sensitive_handles_non_mapping():
    """Non-mapping inputs must not raise."""
    assert redact_sensitive(None) == ""
    assert redact_sensitive("just-a-string") == "just-a-string"


def test_redact_sensitive_falls_back_to_obfuscate_on_error():
    """When async_redact_data raises, fall back to plain obfuscate output."""
    with (
        patch(
            "custom_components.alexa_media.helpers.async_redact_data",
            side_effect=TypeError("bad structure"),
        ),
        patch(
            "custom_components.alexa_media.helpers.obfuscate",
            return_value="OBFUSCATED",
        ) as mock_obfuscate,
    ):
        result = redact_sensitive({"email": "a@b.com"})

    assert result == "OBFUSCATED"
    mock_obfuscate.assert_called()


# =============================================================================
# Tests for _norm_filter_token / _coerce_filter
# =============================================================================


def test_norm_filter_token_none():
    """None input normalizes to None."""
    assert _norm_filter_token(None) is None


def test_norm_filter_token_blank():
    """Whitespace-only input normalizes to None."""
    assert _norm_filter_token("   ") is None


def test_norm_filter_token_casefolds():
    """Tokens are stripped and casefolded."""
    assert _norm_filter_token("  Living Room  ") == "living room"


def test_coerce_filter_empty_inputs():
    """Falsy inputs coerce to an empty set."""
    assert _coerce_filter(None) == set()
    assert _coerce_filter("") == set()
    assert _coerce_filter([]) == set()


def test_coerce_filter_comma_separated_string():
    """A comma-separated string is split, trimmed and casefolded."""
    assert _coerce_filter("Alpha, beta ,, GAMMA") == {"alpha", "beta", "gamma"}


def test_coerce_filter_iterable():
    """Lists/sets/tuples normalize per item, dropping blanks."""
    assert _coerce_filter(["A", " b ", "", None]) == {"a", "b"}
    assert _coerce_filter(("X", "x")) == {"x"}


def test_coerce_filter_single_non_string_token():
    """A non-iterable, non-string value becomes a single token (best effort)."""
    assert _coerce_filter(5) == {"5"}


# =============================================================================
# Additional tests for add_devices (naming, labels, error handling)
# =============================================================================


class _FakeClient:
    """Plain object whose __dict__ carries an explicit name."""

    def __init__(self, name):
        self.__dict__["name"] = name


class _FakeSwitch:
    """Plain switch-like object with reconstructable legacy name attrs."""

    def __init__(self, client=None, suffix=None):
        if client is not None:
            self.__dict__["_client"] = client
        if suffix is not None:
            self.__dict__["_unique_id_suffix"] = suffix


class _NamedEntity:
    """Plain entity exposing an explicit name and optional entity_id."""

    def __init__(self, name=None, entity_id=None):
        if name is not None:
            self._attr_name = name
        self.entity_id = entity_id


class TestAddDevicesNaming:
    """Cover the name/label reconstruction and logging helpers."""

    @pytest.mark.asyncio
    async def test_reconstructed_switch_name_matches_include_filter(self):
        """A switch with _client + _unique_id_suffix reconstructs '<base> <suffix> switch'."""
        switch = _FakeSwitch(client=_FakeClient("Kitchen"), suffix="DND")
        add_devices_callback = MagicMock()

        result = await add_devices(
            "acct",
            [switch],
            add_devices_callback,
            include_filter=["Kitchen DND switch"],
        )

        assert result is True
        add_devices_callback.assert_called_once_with([switch], False)

    @pytest.mark.asyncio
    async def test_reconstruction_without_base_name_is_unnamed(self):
        """When the client has no usable base name, reconstruction yields no name."""
        # client without a name -> base is None -> _device_name returns None
        switch = _FakeSwitch(client=_FakeClient(""), suffix="DND")
        add_devices_callback = MagicMock()

        # Unnamed device is still added when no filter is active.
        result = await add_devices("acct", [switch], add_devices_callback)

        assert result is True
        add_devices_callback.assert_called_once_with([switch], False)

    @pytest.mark.asyncio
    async def test_labels_named_with_and_without_entity_id(self):
        """Devices with/without entity_id and unnamed devices are all added (label branches)."""
        with_id = _NamedEntity(name="Has Id", entity_id="switch.has_id")
        without_id = _NamedEntity(name="No Id", entity_id=None)
        unnamed = _NamedEntity()
        devices = [with_id, without_id, unnamed]
        add_devices_callback = MagicMock()

        result = await add_devices("acct", devices, add_devices_callback)

        assert result is True
        add_devices_callback.assert_called_once_with(devices, False)

    @pytest.mark.asyncio
    async def test_include_filter_skips_unnamed_device(self):
        """An unnamed device is skipped under an active include filter."""
        unnamed = _NamedEntity()
        add_devices_callback = MagicMock()

        result = await add_devices(
            "acct", [unnamed], add_devices_callback, include_filter=["anything"]
        )

        # Nothing matched -> filtered to empty -> True without callback.
        assert result is True
        add_devices_callback.assert_not_called()


class TestAddDevicesErrors:
    """Cover the callback error-handling branches."""

    @pytest.mark.asyncio
    async def test_condition_error_entity_already_exists(self):
        """'Entity id already exists' is swallowed and returns False."""
        device = MagicMock()
        device.name = "Dev"
        callback = MagicMock(
            side_effect=ConditionErrorMessage("test", "Entity id already exists: x")
        )

        result = await add_devices("acct", [device], callback)

        assert result is False

    @pytest.mark.asyncio
    async def test_condition_error_other_message(self):
        """A different ConditionErrorMessage is logged and returns False."""
        device = MagicMock()
        device.name = "Dev"
        callback = MagicMock(
            side_effect=ConditionErrorMessage("test", "Some other problem")
        )

        result = await add_devices("acct", [device], callback)

        assert result is False

    @pytest.mark.asyncio
    async def test_generic_exception(self):
        """Any other exception is caught and returns False."""
        device = MagicMock()
        device.name = "Dev"
        callback = MagicMock(side_effect=RuntimeError("boom"))

        result = await add_devices("acct", [device], callback)

        assert result is False


# =============================================================================
# Tests for retry_async decorator
# =============================================================================


class TestRetryAsync:
    """Cover retry_async success/retry/exhaust/exception paths."""

    @pytest.mark.asyncio
    async def test_succeeds_first_try_no_sleep(self):
        """A function that succeeds immediately returns True without sleeping."""
        with patch(
            "custom_components.alexa_media.helpers.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:

            @retry_async(limit=3, delay=1)
            async def func():
                return True

            result = await func()

        assert result is True
        mock_sleep.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_retries_then_succeeds(self):
        """A function that fails once then succeeds sleeps exactly once."""
        calls = []

        with patch(
            "custom_components.alexa_media.helpers.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:

            @retry_async(limit=3, delay=1)
            async def func():
                calls.append(1)
                return len(calls) >= 2

            result = await func()

        assert result is True
        assert len(calls) == 2
        mock_sleep.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exhausts_retries_returns_false(self):
        """A perpetually failing function exhausts the limit and returns False."""
        calls = []

        with patch(
            "custom_components.alexa_media.helpers.asyncio.sleep",
            new_callable=AsyncMock,
        ) as mock_sleep:

            @retry_async(limit=3, delay=1)
            async def func():
                calls.append(1)
                return False

            result = await func()

        assert result is False
        assert len(calls) == 3
        assert mock_sleep.await_count == 2

    @pytest.mark.asyncio
    async def test_exception_caught_returns_false(self):
        """With catch_exceptions=True a raising function is treated as failure."""
        with patch(
            "custom_components.alexa_media.helpers.asyncio.sleep",
            new_callable=AsyncMock,
        ):

            @retry_async(limit=2, delay=1, catch_exceptions=True)
            async def func():
                raise ValueError("boom")

            result = await func()

        assert result is False

    @pytest.mark.asyncio
    async def test_exception_propagates_when_not_caught(self):
        """With catch_exceptions=False the exception is raised."""
        with patch(
            "custom_components.alexa_media.helpers.asyncio.sleep",
            new_callable=AsyncMock,
        ):

            @retry_async(limit=2, delay=1, catch_exceptions=False)
            async def func():
                raise ValueError("boom")

            with pytest.raises(ValueError):
                await func()


# =============================================================================
# Tests for _catch_login_errors decorator
# =============================================================================


class TestCatchLoginErrors:
    """Cover the _catch_login_errors wrapt decorator branches."""

    @pytest.mark.asyncio
    async def test_passthrough_and_check_login_changes(self):
        """Successful calls return the result and trigger check_login_changes."""
        instance = MagicMock()
        instance.check_login_changes = MagicMock()

        @_catch_login_errors
        async def func(obj):
            return "OK"

        result = await func(instance)

        assert result == "OK"
        instance.check_login_changes.assert_called_once()

    @pytest.mark.asyncio
    async def test_close_requested_returns_none(self):
        """AlexapyLoginCloseRequested is swallowed and returns None."""

        @_catch_login_errors
        async def func(obj):
            raise AlexapyLoginCloseRequested()

        result = await func(MagicMock())

        assert result is None

    @pytest.mark.asyncio
    async def test_relogin_success_via_instance_login(self):
        """A successful re-login via instance._login returns None without reporting."""
        login = MagicMock()
        login.email = "user@example.com"
        login.test_loggedin = AsyncMock(return_value=True)
        instance = MagicMock()
        instance._login = login
        instance.hass = MagicMock()

        @_catch_login_errors
        async def func(obj):
            raise AlexapyLoginError("bad login")

        with patch(
            "custom_components.alexa_media.helpers.report_relogin_required"
        ) as mock_report:
            result = await func(instance)

        assert result is None
        login.test_loggedin.assert_awaited_once()
        mock_report.assert_not_called()

    @pytest.mark.asyncio
    async def test_relogin_failure_via_instance_login_reports(self):
        """A failed re-login via instance._login reports relogin required."""
        login = MagicMock()
        login.email = "user@example.com"
        login.test_loggedin = AsyncMock(return_value=False)
        instance = MagicMock()
        instance._login = login
        instance.hass = MagicMock()

        @_catch_login_errors
        async def func(obj):
            raise AlexapyLoginError("bad login")

        with patch(
            "custom_components.alexa_media.helpers.report_relogin_required"
        ) as mock_report:
            result = await func(instance)

        assert result is None
        mock_report.assert_called_once_with(instance.hass, login, "user@example.com")

    @pytest.mark.asyncio
    async def test_login_discovered_in_args(self):
        """When there is no instance, an AlexaLogin in args is used for relogin."""
        login = MagicMock(spec=AlexaLogin)
        login.email = "user@example.com"
        login.test_loggedin = AsyncMock(return_value=False)

        @_catch_login_errors
        async def func(first, lg):
            raise AlexapyLoginError("bad login")

        with patch(
            "custom_components.alexa_media.helpers.report_relogin_required"
        ) as mock_report:
            # First arg is falsy so instance stays falsy and the arg loop runs.
            result = await func(None, login)

        assert result is None
        login.test_loggedin.assert_awaited_once()
        # hass was never resolved -> reported as None.
        mock_report.assert_called_once_with(None, login, "user@example.com")

    @pytest.mark.asyncio
    async def test_login_error_without_login_reports_none(self):
        """A login error with no resolvable login reports with all-None args."""
        instance = MagicMock(spec=["check_login_changes"])

        @_catch_login_errors
        async def func(obj):
            raise AlexapyLoginError("bad login")

        with patch(
            "custom_components.alexa_media.helpers.report_relogin_required"
        ) as mock_report:
            result = await func(instance)

        assert result is None
        mock_report.assert_called_once_with(None, None, None)


# =============================================================================
# Tests for report_relogin_required
# =============================================================================


def test_report_relogin_required_fires_event():
    """All inputs present and login.status truthy fires the relogin event."""
    hass = MagicMock()
    login = MagicMock()
    login.status = {"login_successful": True}
    login.url = "amazon.com"
    login.stats = {"count": 3}

    result = report_relogin_required(hass, login, "user@example.com")

    assert result is True
    hass.bus.async_fire.assert_called_once()
    args, kwargs = hass.bus.async_fire.call_args
    assert args[0] == "alexa_media_relogin_required"
    event_data = kwargs["event_data"]
    assert event_data["url"] == "amazon.com"
    assert event_data["stats"] == {"count": 3}
    # Email is hidden, not raw.
    assert event_data["email"] != "user@example.com"


def test_report_relogin_required_no_status():
    """A falsy login.status does not fire and returns False."""
    hass = MagicMock()
    login = MagicMock()
    login.status = {}

    result = report_relogin_required(hass, login, "user@example.com")

    assert result is False
    hass.bus.async_fire.assert_not_called()


def test_report_relogin_required_missing_args():
    """Any missing required argument returns False."""
    assert report_relogin_required(None, MagicMock(), "user@example.com") is False
    assert report_relogin_required(MagicMock(), None, "user@example.com") is False
    assert report_relogin_required(MagicMock(), MagicMock(), None) is False


# =============================================================================
# Tests for calculate_uuid
# =============================================================================


class TestCalculateUuid:
    """Cover calculate_uuid index lookup and uuid composition."""

    @pytest.mark.asyncio
    async def test_no_entries_index_zero(self):
        """With no config entries the index is 0 and uuid is 32 hex chars."""
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = []

        with patch(
            "custom_components.alexa_media.helpers.async_get_instance_id",
            AsyncMock(return_value="0" * 32),
        ):
            result = await calculate_uuid(hass, "user@example.com", "amazon.com")

        assert result["index"] == 0
        assert len(result["uuid"]) == 32

    @pytest.mark.asyncio
    async def test_matching_entry_sets_index(self):
        """A matching email/url config entry sets the returned index."""
        entry0 = MagicMock()
        entry0.data = {CONF_EMAIL: "other@example.com", CONF_URL: "amazon.com"}
        entry1 = MagicMock()
        entry1.data = {CONF_EMAIL: "user@example.com", CONF_URL: "amazon.com"}
        hass = MagicMock()
        hass.config_entries.async_entries.return_value = [entry0, entry1]

        with patch(
            "custom_components.alexa_media.helpers.async_get_instance_id",
            AsyncMock(return_value="0" * 32),
        ):
            result = await calculate_uuid(hass, "user@example.com", "amazon.com")

        assert result["index"] == 1
        assert len(result["uuid"]) == 32


# =============================================================================
# Tests for alarm_just_dismissed
# =============================================================================


def test_alarm_just_dismissed_true():
    """A dismissed alarm (version +1, status off/on) is detected."""
    alarm = {"status": "OFF", "version": "2"}
    assert alarm_just_dismissed(alarm, "ON", "1") is True


@pytest.mark.parametrize(
    ("alarm", "previous_status", "previous_version"),
    [
        # previous_status not dismissable
        ({"status": "OFF", "version": "2"}, "OFF", "1"),
        # previous_version is None (just created)
        ({"status": "OFF", "version": "2"}, "ON", None),
        # alarm falsy (just deleted)
        ({}, "ON", "1"),
        # current status not OFF/ON
        ({"status": "DELETED", "version": "2"}, "ON", "1"),
        # version unchanged
        ({"status": "OFF", "version": "1"}, "ON", "1"),
        # version jumped by more than one (an edit, not a dismissal)
        ({"status": "OFF", "version": "5"}, "ON", "1"),
    ],
)
def test_alarm_just_dismissed_false(alarm, previous_status, previous_version):
    """Edge cases that are not dismissals all return False."""
    assert alarm_just_dismissed(alarm, previous_status, previous_version) is False


# =============================================================================
# Tests for _network_allowed
# =============================================================================


def _make_login(close_requested=False, session_closed=False, login_successful=True):
    """Build a login-like mock for _network_allowed."""
    login = MagicMock()
    login.close_requested = close_requested
    login.session.closed = session_closed
    login.status = {"login_successful": login_successful}
    return login


def test_network_allowed_true():
    """A healthy login is network-allowed."""
    assert _network_allowed(_make_login()) is True


def test_network_allowed_close_requested():
    """A close-requested login is not allowed."""
    assert _network_allowed(_make_login(close_requested=True)) is False


def test_network_allowed_session_closed():
    """A closed session is not allowed."""
    assert _network_allowed(_make_login(session_closed=True)) is False


def test_network_allowed_not_logged_in():
    """A login without login_successful is not allowed."""
    assert _network_allowed(_make_login(login_successful=False)) is False


# =============================================================================
# Tests for _entity_backed_serials
# =============================================================================


def test_entity_backed_serials_no_entities():
    """An account without a dict 'entities' returns an empty set."""
    assert _entity_backed_serials({}) == set()
    assert _entity_backed_serials({"entities": None}) == set()


def test_entity_backed_serials_no_sensor_dict():
    """Entities without a sensor dict returns an empty set."""
    assert _entity_backed_serials({"entities": {"sensor": None}}) == set()


def test_entity_backed_serials_collects_string_keys():
    """Only non-empty string sensor keys are collected."""
    account = {
        "entities": {
            "sensor": {
                "serial1": {},
                "serial2": {},
                "": {},
                123: {},
            }
        }
    }
    assert _entity_backed_serials(account) == {"serial1", "serial2"}


# =============================================================================
# Tests for _entity_backed_device_identifiers
# =============================================================================


class _FakeDeviceInfo:
    """DeviceInfo-like object exposing an identifiers attribute."""

    def __init__(self, identifiers):
        self.identifiers = identifiers


class _FakeEntityWithDeviceInfo:
    """Entity-like object carrying a device_info attribute."""

    def __init__(self, device_info):
        self.device_info = device_info


class _BadDeviceInfo:
    """DeviceInfo-like object whose identifiers access raises."""

    @property
    def identifiers(self):
        raise RuntimeError("cannot read identifiers")


def test_entity_backed_device_identifiers_empty():
    """No entities yields an empty identifier set."""
    assert _entity_backed_device_identifiers({}) == set()


def test_entity_backed_device_identifiers_object_and_dict_styles():
    """Both DeviceInfo objects and dict-style device_info are walked."""
    obj_style = _FakeEntityWithDeviceInfo(
        _FakeDeviceInfo({("alexa_media", "serial-A")})
    )
    dict_style = _FakeEntityWithDeviceInfo(
        {"identifiers": {("alexa_media", "serial-B")}}
    )
    account = {
        "entities": {
            "media_player": {"sn1": obj_style},
            "sensor": [dict_style],
            "noise": None,
            "label": "ignored-string",
        }
    }

    result = _entity_backed_device_identifiers(account)

    assert result == {"serial-A", "serial-B"}


def test_entity_backed_device_identifiers_skips_malformed_identifiers():
    """Identifiers that are not 2-tuples are ignored."""
    entity = _FakeEntityWithDeviceInfo(
        _FakeDeviceInfo({("alexa_media", "good"), "not-a-tuple", ("a", "b", "c")})
    )
    account = {"entities": {"media_player": (entity,)}}

    assert _entity_backed_device_identifiers(account) == {"good"}


def test_entity_backed_device_identifiers_handles_identifier_errors():
    """An identifiers accessor that raises is caught and contributes nothing."""
    entity = _FakeEntityWithDeviceInfo(_BadDeviceInfo())
    account = {"entities": {"media_player": {"sn1": entity}}}

    assert _entity_backed_device_identifiers(account) == set()
