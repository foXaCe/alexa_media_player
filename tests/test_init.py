"""Tests for the integration entry module ``custom_components.alexa_media.__init__``.

These exercise the orchestration in ``__init__`` itself (domain setup, config
entry setup/unload/remove, the relogin/shutdown event callbacks, the options
update listener and the reauth ``test_login_status`` flow). The heavy lifting
lives in the well-tested ``setup/`` package, so every boundary it crosses
(``AlexaLogin``, ``AlexaMediaCoordinator``, the ``setup`` helpers, persistent
notifications, ...) is mocked; only ``__init__``'s own glue runs for real.

The module is imported as ``amp`` rather than ``from ... import test_login_status``
on purpose: a bare ``test_login_status`` name would be collected by pytest.
"""

import asyncio
import contextlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
from alexapy import AlexapyConnectionError
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
import pytest

import custom_components.alexa_media as amp
from custom_components.alexa_media.const import (
    ALEXA_COMPONENTS,
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_INCLUDE_DEVICES,
    CONF_OTPSECRET,
    CONF_SCAN_INTERVAL,
    DATA_ALEXAMEDIA,
    DEPENDENT_ALEXA_COMPONENTS,
    DOMAIN,
)
from custom_components.alexa_media.coordinator import AlexaMediaCoordinator

EMAIL = "user@example.com"
URL = "amazon.com"
_PKG = "custom_components.alexa_media"

# --------------------------------------------------------------------------- #
# helpers / fakes
# --------------------------------------------------------------------------- #


class _AsyncCM:
    """Minimal async context manager for ``async with session.get(...)``."""

    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *args):
        return False


class _FakeTask:
    """Awaitable stand-in for an asyncio.Task that raises on await/cancel."""

    def __init__(self, done=False):
        self._done = done
        self.cancel = MagicMock()

    def done(self):
        return self._done

    def __await__(self):
        async def _raise():
            raise asyncio.CancelledError

        return _raise().__await__()


@contextlib.contextmanager
def _applied(patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


def _make_hass(data=None):
    hass = MagicMock()
    hass.data = {} if data is None else data
    hass.config.path = MagicMock(side_effect=lambda *a: "/".join(str(x) for x in a))
    hass.async_add_executor_job = AsyncMock()

    def _eat(coro, name=None):
        # Background tasks receive real coroutines; close them so the loop does
        # not warn about a coroutine that was never awaited.
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    hass.async_create_background_task = MagicMock(side_effect=_eat)
    # async_create_task runs the coroutine as a real awaitable task so callers
    # that await the returned task (e.g. the overlapped http2_connect) work.
    hass.async_create_task = MagicMock(
        side_effect=lambda coro, *a, **k: asyncio.ensure_future(coro)
    )
    return hass


def _make_config_entry(data=None, entry_id="entry1", runtime_data=None):
    entry = MagicMock()
    entry.data = data if data is not None else {}
    entry.entry_id = entry_id
    entry.runtime_data = runtime_data
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    return entry


def _make_login(url=URL):
    login = MagicMock()
    login.email = EMAIL
    login.url = url
    login.status = {}
    login.stats = {}
    login._cookiefile = None
    login._ssl = None
    login.customer_id = None
    login.load_cookie = AsyncMock(return_value={})
    login.login = AsyncMock()
    login.check_domain = AsyncMock()
    login.finalize_login = AsyncMock()
    login.reset = AsyncMock()
    login._create_session = MagicMock()
    session = MagicMock()
    session.closed = False
    session.get = MagicMock(side_effect=aiohttp.ClientError("no cookie auth"))
    login._session = session
    return login


_BOOTSTRAP_OK = (
    '{"authentication": {"authenticated": true, '
    '"customerEmail": "USER@example.com", "customerId": "cid"}}'
)


def _setup_entry_patches(login, *, setup_alexa=None, test_login=None, index=0):
    """Standard patch set for ``async_setup_entry`` orchestration tests."""
    return [
        patch(f"{_PKG}.AlexaLogin", MagicMock(return_value=login)),
        patch(
            f"{_PKG}.calculate_uuid",
            AsyncMock(return_value={"uuid": "a" * 32, "index": index}),
        ),
        patch(f"{_PKG}.setup_alexa", setup_alexa or AsyncMock(return_value=True)),
        patch(f"{_PKG}.test_login_status", test_login or AsyncMock(return_value=True)),
    ]


# --------------------------------------------------------------------------- #
# async_setup
# --------------------------------------------------------------------------- #


async def test_async_setup_yaml_present_creates_issue_and_returns_false():
    hass = _make_hass()
    integration = MagicMock()
    integration.name = "Alexa Media Player"
    integration.version = "1.0.0"
    with (
        patch(f"{_PKG}.AlexaMetrics", MagicMock()),
        patch(f"{_PKG}.async_get_integration", AsyncMock(return_value=integration)),
        patch(f"{_PKG}.async_create_issue") as issue,
        patch(f"{_PKG}.AlexaMediaServices") as services,
    ):
        result = await amp.async_setup(hass, {DOMAIN: {"accounts": []}})

    assert result is False
    issue.assert_called_once()
    services.assert_not_called()


async def test_async_setup_registers_services_and_returns_true():
    hass = _make_hass()
    integration = MagicMock()
    integration.name = ""  # exercise the "<not available>" fallback
    integration.version = "1.0.0"
    svc_instance = MagicMock()
    svc_instance.register = AsyncMock()
    with (
        patch(f"{_PKG}.AlexaMetrics", MagicMock()),
        patch(f"{_PKG}.async_get_integration", AsyncMock(return_value=integration)),
        patch(f"{_PKG}.AlexaMediaServices", MagicMock(return_value=svc_instance)),
    ):
        result = await amp.async_setup(hass, {})

    assert result is True
    svc_instance.register.assert_awaited_once()
    assert hass.data[DOMAIN]["services"] is svc_instance
    assert hass.data[DOMAIN]["accounts"] == {}


async def test_async_setup_skips_service_registration_when_present():
    existing = MagicMock()
    hass = _make_hass(data={DOMAIN: {"services": existing}})
    integration = MagicMock()
    integration.name = "Alexa Media Player"
    integration.version = "1.0.0"
    with (
        patch(f"{_PKG}.AlexaMetrics", MagicMock()),
        patch(f"{_PKG}.async_get_integration", AsyncMock(return_value=integration)),
        patch(f"{_PKG}.AlexaMediaServices") as services,
    ):
        result = await amp.async_setup(hass, {})

    assert result is True
    # Services already registered -> async_setup must not build a new instance.
    services.assert_not_called()
    assert hass.data[DOMAIN]["services"] is existing


# --------------------------------------------------------------------------- #
# async_setup_entry
# --------------------------------------------------------------------------- #


async def test_async_setup_entry_cookie_path_fails_falls_back_to_login():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    setup_alexa = AsyncMock(return_value=True)
    with _applied(_setup_entry_patches(login, setup_alexa=setup_alexa)):
        result = await amp.async_setup_entry(hass, entry)

    assert result is True
    # Bootstrap probe raised -> a full credential login is performed.
    login.login.assert_awaited_once()
    setup_alexa.assert_awaited_once()
    account = hass.data[DATA_ALEXAMEDIA]["accounts"][EMAIL]
    assert account["login_obj"] is login
    assert account["last_push_activity"] == 0
    assert account["second_account_index"] == 0
    # Account dict is fully provisioned with the expected device/entity buckets.
    assert set(account["devices"]) >= {"media_player", "switch", "guard"}
    # Update listener registered and handed to HA for automatic cleanup.
    entry.add_update_listener.assert_called_once_with(amp.update_listener)
    assert entry.async_on_unload.called
    # runtime_data was created (it started as None).
    assert entry.runtime_data is not None
    # index 0 -> the HOMEASSISTANT_STOP/STARTED listeners are registered.
    once_events = [c.args[0] for c in hass.bus.async_listen_once.call_args_list]
    assert EVENT_HOMEASSISTANT_STOP in once_events
    assert EVENT_HOMEASSISTANT_STARTED in once_events


async def test_async_setup_entry_cookie_bootstrap_success_skips_login():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    # Cached cookies present -> the /api/bootstrap probe runs.
    login.load_cookie = AsyncMock(return_value={"session-id": "x"})
    response = MagicMock()
    response.status = 200
    response.text = AsyncMock(return_value=_BOOTSTRAP_OK)
    login._session.get = MagicMock(return_value=_AsyncCM(response))
    setup_alexa = AsyncMock(return_value=True)
    with _applied(_setup_entry_patches(login, setup_alexa=setup_alexa)):
        result = await amp.async_setup_entry(hass, entry)

    assert result is True
    # Cookie auth confirmed via /api/bootstrap -> no full login.
    login.login.assert_not_awaited()
    login.check_domain.assert_awaited_once()
    login.finalize_login.assert_awaited_once()
    assert login.status["login_successful"] is True
    assert login.customer_id == "cid"
    setup_alexa.assert_awaited_once()


async def test_async_setup_entry_recreates_closed_session():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    # Cached cookies present -> the probe path runs and recreates the closed session.
    login.load_cookie = AsyncMock(return_value={"session-id": "x"})
    login._session.closed = True  # forces login._create_session(True)
    with _applied(_setup_entry_patches(login)):
        result = await amp.async_setup_entry(hass, entry)

    assert result is True
    login._create_session.assert_called_once_with(True)


async def test_async_setup_entry_raises_auth_failed_when_login_status_false():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    setup_alexa = AsyncMock(return_value=True)
    with _applied(
        _setup_entry_patches(
            login,
            setup_alexa=setup_alexa,
            test_login=AsyncMock(return_value=False),
        )
    ):
        with pytest.raises(ConfigEntryAuthFailed):
            await amp.async_setup_entry(hass, entry)

    setup_alexa.assert_not_awaited()


async def test_async_setup_entry_connection_error_raises_not_ready():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    login.load_cookie = AsyncMock(side_effect=AlexapyConnectionError("boom"))
    with (
        _applied(_setup_entry_patches(login)),
        pytest.raises(ConfigEntryNotReady),
    ):
        await amp.async_setup_entry(hass, entry)


async def test_async_setup_entry_second_account_skips_stop_listener():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    with _applied(_setup_entry_patches(login, index=2)):
        result = await amp.async_setup_entry(hass, entry)

    assert result is True
    account = hass.data[DATA_ALEXAMEDIA]["accounts"][EMAIL]
    assert account["second_account_index"] == 2
    # Secondary accounts (index != 0) do not register the global stop/start hooks.
    once_events = [c.args[0] for c in hass.bus.async_listen_once.call_args_list]
    assert EVENT_HOMEASSISTANT_STOP not in once_events


async def test_async_setup_entry_listener_callbacks_execute():
    """Invoke the nested event callbacks registered during setup."""
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    setup_alexa = AsyncMock(return_value=True)
    with (
        _applied(_setup_entry_patches(login, setup_alexa=setup_alexa)),
        patch(f"{_PKG}.close_connections", AsyncMock()) as close_conns,
        patch("asyncio.sleep", AsyncMock()),
    ):
        result = await amp.async_setup_entry(hass, entry)
        assert result is True

        once = {c.args[0]: c.args[1] for c in hass.bus.async_listen_once.call_args_list}
        listen = {c.args[0]: c.args[1] for c in hass.bus.async_listen.call_args_list}

        # complete_startup refreshes notify targets when a service is present.
        notify = MagicMock()
        notify.async_register_services = AsyncMock()
        hass.data[DATA_ALEXAMEDIA]["notify_service"] = notify

        await once[EVENT_HOMEASSISTANT_STOP]()  # close_alexa_media
        await once[EVENT_HOMEASSISTANT_STARTED]()  # complete_startup

        event = MagicMock()
        event.data = {"email": amp.hide_email(EMAIL)}
        await listen["alexa_media_relogin_required"](event)  # relogin
        await listen["alexa_media_relogin_success"](event)  # login_success

    close_conns.assert_awaited()  # close_alexa_media iterated the accounts
    notify.async_register_services.assert_awaited_once()
    login.reset.assert_awaited_once()  # relogin reset the existing login_obj
    assert login.oauth_login is True
    # setup_alexa: initial entry setup + relogin + login_success.
    assert setup_alexa.await_count == 3


async def test_relogin_callback_rebuilds_missing_login_obj():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    setup_alexa = AsyncMock(return_value=True)
    with _applied(_setup_entry_patches(login, setup_alexa=setup_alexa)):
        await amp.async_setup_entry(hass, entry)
        listen = {c.args[0]: c.args[1] for c in hass.bus.async_listen.call_args_list}
        # Drop the stored login so relogin must construct a fresh AlexaLogin.
        hass.data[DATA_ALEXAMEDIA]["accounts"][EMAIL]["login_obj"] = None
        setup_alexa.reset_mock()
        event = MagicMock()
        event.data = {"email": amp.hide_email(EMAIL)}
        await listen["alexa_media_relogin_required"](event)

    assert hass.data[DATA_ALEXAMEDIA]["accounts"][EMAIL]["login_obj"] is login
    setup_alexa.assert_awaited_once()


async def test_relogin_callback_ignores_other_account():
    hass = _make_hass()
    entry = _make_config_entry(
        data={CONF_EMAIL: EMAIL, CONF_PASSWORD: "pw", CONF_URL: URL},
        runtime_data=None,
    )
    login = _make_login()
    setup_alexa = AsyncMock(return_value=True)
    with _applied(_setup_entry_patches(login, setup_alexa=setup_alexa)):
        await amp.async_setup_entry(hass, entry)
        listen = {c.args[0]: c.args[1] for c in hass.bus.async_listen.call_args_list}
        setup_alexa.reset_mock()
        login.reset.reset_mock()
        event = MagicMock()
        event.data = {"email": "someone-else"}
        await listen["alexa_media_relogin_required"](event)
        await listen["alexa_media_relogin_success"](event)

    # Email mismatch -> both callbacks are no-ops.
    login.reset.assert_not_awaited()
    setup_alexa.assert_not_awaited()


# --------------------------------------------------------------------------- #
# setup_alexa
# --------------------------------------------------------------------------- #


def _setup_alexa_env(coordinator):
    account = {"coordinator": coordinator}
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}})
    entry = _make_config_entry(
        data={
            CONF_EMAIL: EMAIL,
            CONF_DEBUG: False,
            CONF_INCLUDE_DEVICES: "",
            CONF_EXCLUDE_DEVICES: "",
            CONF_SCAN_INTERVAL: timedelta(seconds=60),
        },
        runtime_data=MagicMock(),
    )
    return hass, entry, account


def _setup_alexa_patches(http2_enabled=True):
    metrics = MagicMock()
    return [
        patch(f"{_PKG}.get_metrics", MagicMock(return_value=metrics)),
        patch(f"{_PKG}.setup_last_called._init_last_called_probe_worker", MagicMock()),
        patch(
            f"{_PKG}.setup_push.http2_connect",
            AsyncMock(return_value=http2_enabled),
        ),
        patch(
            f"{_PKG}._async_update_last_called_background",
            MagicMock(return_value=MagicMock()),
        ),
    ]


async def test_setup_alexa_creates_new_coordinator():
    hass, entry, account = _setup_alexa_env(coordinator=None)
    login = _make_login()
    coord_instance = MagicMock()
    coord_instance.async_config_entry_first_refresh = AsyncMock()
    coord_cls = MagicMock(return_value=coord_instance)
    patches = [
        *_setup_alexa_patches(http2_enabled=True),
        patch(f"{_PKG}.AlexaMediaCoordinator", coord_cls),
    ]
    with _applied(patches):
        result = await amp.setup_alexa(hass, entry, login)

    assert result is True
    assert account["coordinator"] is coord_instance
    assert account["http2"] is True
    coord_instance.set_http2_status.assert_called_once_with(True)
    coord_instance.async_config_entry_first_refresh.assert_awaited_once()
    # runtime_data mirrors the coordinator for type-safe access.
    assert entry.runtime_data.coordinator is coord_instance
    hass.async_create_background_task.assert_called_once()


async def test_setup_alexa_reuses_optimized_coordinator():
    coordinator = MagicMock(spec=AlexaMediaCoordinator)
    coordinator.async_config_entry_first_refresh = AsyncMock()
    hass, entry, _account = _setup_alexa_env(coordinator=coordinator)
    login = _make_login()
    with _applied(_setup_alexa_patches(http2_enabled=True)):
        result = await amp.setup_alexa(hass, entry, login)

    assert result is True
    coordinator.set_http2_status.assert_called_once_with(True)
    coordinator.async_config_entry_first_refresh.assert_awaited_once()


async def test_setup_alexa_reuses_legacy_coordinator_sets_interval():
    coordinator = MagicMock()  # not an AlexaMediaCoordinator instance
    coordinator.async_config_entry_first_refresh = AsyncMock()
    hass, entry, _account = _setup_alexa_env(coordinator=coordinator)
    login = _make_login()
    with _applied(_setup_alexa_patches(http2_enabled=False)):
        result = await amp.setup_alexa(hass, entry, login)

    assert result is True
    # http2 disabled -> interval is the raw scan_interval (no x10 push backoff).
    assert coordinator.update_interval == timedelta(seconds=60)
    coordinator.async_config_entry_first_refresh.assert_awaited_once()


# --------------------------------------------------------------------------- #
# async_unload_entry
# --------------------------------------------------------------------------- #


async def test_async_unload_entry_full_cleanup():
    account = {
        "notifications_refresh_task": None,
        "notifications_init_task": None,
        "last_called_init_task": _FakeTask(),
        "service_update_last_called_task": None,
        "last_called_probe_task": _FakeTask(),
        "confirm_refresh_debouncer": MagicMock(),
    }
    hass = _make_hass(
        data={
            DATA_ALEXAMEDIA: {
                "accounts": {EMAIL: account},
                "config_flows": {f"{EMAIL} - {URL}": {"flow_id": "fid"}},
            }
        }
    )
    hass.config_entries.async_forward_entry_unload = AsyncMock(return_value=True)
    # Aborting the in-progress flow raises UnknownFlow -> must be swallowed.
    hass.config_entries.flow.async_abort = MagicMock(side_effect=UnknownFlow)
    entry = MagicMock()
    entry.data = {"email": EMAIL, "url": "http://amazon.com"}
    # The unload nulls these out on the account dict, so capture them first.
    init_task = account["last_called_init_task"]
    probe_task = account["last_called_probe_task"]
    debouncer = account["confirm_refresh_debouncer"]
    with (
        patch(f"{_PKG}.close_connections", AsyncMock()) as close_conns,
        patch(f"{_PKG}.notify_async_unload_entry", AsyncMock()) as notify_unload,
        patch(f"{_PKG}.async_dismiss_persistent_notification") as dismiss,
    ):
        result = await amp.async_unload_entry(hass, entry)

    assert result is True
    init_task.cancel.assert_called_once()
    probe_task.cancel.assert_called_once()
    debouncer.async_cancel.assert_called_once()
    # 1 ALEXA component + 5 dependent (notify is dispatched separately).
    expected_forward = len(ALEXA_COMPONENTS) + len(DEPENDENT_ALEXA_COMPONENTS) - 1
    assert (
        hass.config_entries.async_forward_entry_unload.await_count == expected_forward
    )
    notify_unload.assert_awaited_once()
    close_conns.assert_awaited_once_with(hass, EMAIL)
    dismiss.assert_called_once()
    # The in-progress flow was aborted during config_flows cleanup.
    hass.config_entries.flow.async_abort.assert_called_once_with("fid")
    # accounts + config_flows are popped; the empty container itself remains
    # (line 674's truthiness guard means the final pop never runs).
    assert hass.data[DATA_ALEXAMEDIA] == {}


async def test_async_unload_entry_keeps_residual_data_structure():
    account = {
        "notifications_refresh_task": None,
        "notifications_init_task": None,
        "last_called_init_task": None,
        "service_update_last_called_task": None,
        "last_called_probe_task": None,
        "confirm_refresh_debouncer": None,
    }
    hass = _make_hass(
        data={
            DATA_ALEXAMEDIA: {
                "accounts": {EMAIL: account},
                # No "config_flows" key -> that cleanup branch is skipped and an
                # extra key remains, exercising the "unable to remove" else path.
                "metrics": MagicMock(),
            }
        }
    )
    hass.config_entries.async_forward_entry_unload = AsyncMock(return_value=True)
    entry = MagicMock()
    entry.data = {"email": EMAIL, "url": "http://amazon.com"}
    with (
        patch(f"{_PKG}.close_connections", AsyncMock()),
        patch(f"{_PKG}.notify_async_unload_entry", AsyncMock()),
        patch(f"{_PKG}.async_dismiss_persistent_notification"),
    ):
        result = await amp.async_unload_entry(hass, entry)

    assert result is True
    # accounts removed, but the surviving "metrics" key keeps the container alive.
    assert "accounts" not in hass.data[DATA_ALEXAMEDIA]
    assert "metrics" in hass.data[DATA_ALEXAMEDIA]


async def test_async_unload_entry_missing_account_returns_true():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"accounts": {}}})
    entry = MagicMock()
    entry.data = {"email": EMAIL, "url": "http://amazon.com"}
    result = await amp.async_unload_entry(hass, entry)
    assert result is True


async def test_async_unload_entry_component_error_is_swallowed():
    account = {
        "notifications_refresh_task": None,
        "notifications_init_task": None,
        "last_called_init_task": None,
        "service_update_last_called_task": None,
        "last_called_probe_task": None,
        "confirm_refresh_debouncer": None,
    }
    hass = _make_hass(
        data={DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}, "config_flows": {}}}
    )
    hass.config_entries.async_forward_entry_unload = AsyncMock(
        side_effect=RuntimeError("kaboom")
    )
    entry = MagicMock()
    entry.data = {"email": EMAIL, "url": "http://amazon.com"}
    with (
        patch(f"{_PKG}.close_connections", AsyncMock()),
        patch(f"{_PKG}.notify_async_unload_entry", AsyncMock(side_effect=ValueError)),
        patch(f"{_PKG}.async_dismiss_persistent_notification"),
    ):
        result = await amp.async_unload_entry(hass, entry)

    # Every component raised, but unload still completes successfully.
    assert result is True
    assert EMAIL not in hass.data[DATA_ALEXAMEDIA].get("accounts", {})


# --------------------------------------------------------------------------- #
# async_remove_entry
# --------------------------------------------------------------------------- #


async def test_async_remove_entry_uses_delete_cookiefile():
    hass = _make_hass()
    entry = MagicMock()
    entry.data = {"email": EMAIL}
    login = _make_login()
    login.delete_cookiefile = AsyncMock()
    login_cls = MagicMock(return_value=login)
    with patch(f"{_PKG}.AlexaLogin", login_cls):
        result = await amp.async_remove_entry(hass, entry)

    assert result is True
    login.delete_cookiefile.assert_awaited_once()


async def test_async_remove_entry_delete_cookiefile_error_logged():
    hass = _make_hass()
    entry = MagicMock()
    entry.data = {"email": EMAIL}
    login = _make_login()
    login.delete_cookiefile = AsyncMock(side_effect=OSError("locked"))
    with patch(f"{_PKG}.AlexaLogin", MagicMock(return_value=login)):
        result = await amp.async_remove_entry(hass, entry)

    # The exception is caught and logged; removal still reports success.
    assert result is True
    login.delete_cookiefile.assert_awaited_once()


async def test_async_remove_entry_fallback_deletes_existing_cookiefile():
    hass = _make_hass()
    entry = MagicMock()
    entry.data = {"email": EMAIL}
    login = _make_login()
    login_cls = MagicMock(return_value=login)
    login_cls.delete_cookiefile = None  # not callable -> legacy fallback path
    delete_cookie = AsyncMock()
    with (
        patch(f"{_PKG}.AlexaLogin", login_cls),
        patch(f"{_PKG}.os.path.exists", return_value=True),
        patch(f"{_PKG}.alexapy_delete_cookie", delete_cookie),
    ):
        result = await amp.async_remove_entry(hass, entry)

    assert result is True
    delete_cookie.assert_awaited_once()


async def test_async_remove_entry_fallback_delete_error_logged():
    hass = _make_hass()
    entry = MagicMock()
    entry.data = {"email": EMAIL}
    login = _make_login()
    login_cls = MagicMock(return_value=login)
    login_cls.delete_cookiefile = None
    delete_cookie = AsyncMock(side_effect=OSError("permission denied"))
    with (
        patch(f"{_PKG}.AlexaLogin", login_cls),
        patch(f"{_PKG}.os.path.exists", return_value=True),
        patch(f"{_PKG}.alexapy_delete_cookie", delete_cookie),
    ):
        result = await amp.async_remove_entry(hass, entry)

    # The deleter raised, the error was logged, removal still succeeds.
    assert result is True
    delete_cookie.assert_awaited_once()


async def test_async_remove_entry_fallback_missing_cookiefile():
    hass = _make_hass()
    entry = MagicMock()
    entry.data = {"email": EMAIL}
    login = _make_login()
    login_cls = MagicMock(return_value=login)
    login_cls.delete_cookiefile = None
    delete_cookie = AsyncMock()
    with (
        patch(f"{_PKG}.AlexaLogin", login_cls),
        patch(f"{_PKG}.os.path.exists", return_value=False),
        patch(f"{_PKG}.alexapy_delete_cookie", delete_cookie),
    ):
        result = await amp.async_remove_entry(hass, entry)

    assert result is True
    # Nothing to delete -> the legacy deleter is not invoked.
    delete_cookie.assert_not_awaited()


# --------------------------------------------------------------------------- #
# close_connections
# --------------------------------------------------------------------------- #


async def test_close_connections_saves_and_closes():
    login = MagicMock()
    login.save_cookiefile = AsyncMock()
    login.close = AsyncMock()
    login.session.closed = True
    hass = _make_hass(
        data={DATA_ALEXAMEDIA: {"accounts": {EMAIL: {"login_obj": login}}}}
    )
    await amp.close_connections(hass, EMAIL)
    login.save_cookiefile.assert_awaited_once()
    login.close.assert_awaited_once()


async def test_close_connections_noop_when_account_missing():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"accounts": {}}})
    # No account -> returns without raising.
    assert await amp.close_connections(hass, EMAIL) is None


async def test_close_connections_noop_when_no_login_obj():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"accounts": {EMAIL: {}}}})
    assert await amp.close_connections(hass, EMAIL) is None


# --------------------------------------------------------------------------- #
# update_listener
# --------------------------------------------------------------------------- #


async def test_update_listener_reloads_on_changed_option():
    account = {"options": {CONF_SCAN_INTERVAL: 60}}
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}})
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.data = {CONF_EMAIL: EMAIL, CONF_SCAN_INTERVAL: 120}
    await amp.update_listener(hass, entry)
    assert account["options"][CONF_SCAN_INTERVAL] == 120
    hass.config_entries.async_reload.assert_awaited_once_with("entry1")


async def test_update_listener_no_reload_when_unchanged():
    account = {"options": {CONF_SCAN_INTERVAL: 60}}
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}})
    hass.config_entries.async_reload = AsyncMock()
    entry = MagicMock()
    entry.entry_id = "entry1"
    entry.data = {CONF_EMAIL: EMAIL, CONF_SCAN_INTERVAL: 60}
    await amp.update_listener(hass, entry)
    hass.config_entries.async_reload.assert_not_awaited()


# --------------------------------------------------------------------------- #
# test_login_status
# --------------------------------------------------------------------------- #


def _login_status_account():
    return {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: "pw",
        CONF_URL: URL,
        CONF_DEBUG: False,
        CONF_INCLUDE_DEVICES: "",
        CONF_EXCLUDE_DEVICES: "",
        CONF_SCAN_INTERVAL: timedelta(seconds=60),
        CONF_OTPSECRET: "",
    }


def _login_for_status(successful):
    login = MagicMock()
    login.email = EMAIL
    login.url = "https://alexa.amazon.com"
    login.status = {"login_successful": successful}
    login.stats = {"login_timestamp": datetime.now(), "api_calls": 7}
    return login


async def test_test_login_status_returns_true_when_logged_in():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"config_flows": {}}})
    entry = MagicMock()
    entry.data = _login_status_account()
    result = await amp.test_login_status(hass, entry, _login_for_status(True))
    assert result is True


async def test_test_login_status_starts_reauth_when_not_logged_in():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"config_flows": {}}})
    entry = MagicMock()
    entry.data = _login_status_account()
    flow_obj = {"flow_id": "new-flow"}
    entry.async_get_active_flows = MagicMock(return_value=iter([flow_obj]))
    with (
        patch(f"{_PKG}.in_progress_instances", MagicMock(return_value=[])),
        patch(f"{_PKG}.async_create_persistent_notification") as notify,
    ):
        result = await amp.test_login_status(hass, entry, _login_for_status(False))

    assert result is False
    notify.assert_called_once()
    entry.async_start_reauth.assert_called_once()
    assert hass.data[DATA_ALEXAMEDIA]["config_flows"][f"{EMAIL} - {URL}"] is flow_obj


async def test_test_login_status_handles_no_active_flow():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"config_flows": {}}})
    entry = MagicMock()
    entry.data = _login_status_account()
    entry.async_get_active_flows = MagicMock(return_value=iter([]))
    with (
        patch(f"{_PKG}.in_progress_instances", MagicMock(return_value=[])),
        patch(f"{_PKG}.async_create_persistent_notification"),
    ):
        result = await amp.test_login_status(hass, entry, _login_for_status(False))

    # StopIteration on the active-flow lookup is handled gracefully.
    assert result is False
    assert hass.data[DATA_ALEXAMEDIA]["config_flows"].get(f"{EMAIL} - {URL}") is None


async def test_test_login_status_existing_in_progress_flow_returns_false():
    hass = _make_hass(
        data={
            DATA_ALEXAMEDIA: {"config_flows": {f"{EMAIL} - {URL}": {"flow_id": "fid"}}}
        }
    )
    entry = MagicMock()
    entry.data = _login_status_account()
    with (
        patch(f"{_PKG}.in_progress_instances", MagicMock(return_value=["fid"])),
        patch(f"{_PKG}.async_create_persistent_notification"),
    ):
        result = await amp.test_login_status(hass, entry, _login_for_status(False))

    assert result is False
    # An existing config flow is already in progress -> do not start a new reauth.
    entry.async_start_reauth.assert_not_called()


async def test_test_login_status_aborts_orphaned_flow_then_reauths():
    hass = _make_hass(
        data={
            DATA_ALEXAMEDIA: {
                "config_flows": {f"{EMAIL} - {URL}": {"flow_id": "orphan"}}
            }
        }
    )
    hass.config_entries.flow.async_abort = MagicMock(side_effect=UnknownFlow)
    entry = MagicMock()
    entry.data = _login_status_account()
    entry.async_get_active_flows = MagicMock(return_value=iter([{"flow_id": "n"}]))
    with (
        patch(f"{_PKG}.in_progress_instances", MagicMock(return_value=[])),
        patch(f"{_PKG}.async_create_persistent_notification"),
    ):
        result = await amp.test_login_status(hass, entry, _login_for_status(False))

    assert result is False
    # Orphaned flow id not in progress -> abort it (UnknownFlow swallowed) and reauth.
    hass.config_entries.flow.async_abort.assert_called_once_with("orphan")
    entry.async_start_reauth.assert_called_once()


async def test_test_login_status_omits_elapsed_when_timestamp_sentinel():
    hass = _make_hass(data={DATA_ALEXAMEDIA: {"config_flows": {}}})
    entry = MagicMock()
    entry.data = _login_status_account()
    entry.async_get_active_flows = MagicMock(return_value=iter([{"flow_id": "n"}]))
    login = _login_for_status(False)
    login.stats = {"login_timestamp": datetime(1, 1, 1), "api_calls": 0}
    with (
        patch(f"{_PKG}.in_progress_instances", MagicMock(return_value=[])),
        patch(f"{_PKG}.async_create_persistent_notification") as notify,
    ):
        result = await amp.test_login_status(hass, entry, login)

    assert result is False
    # Sentinel timestamp -> the "Relogin required after ..." suffix is omitted.
    message = notify.call_args.kwargs["message"]
    assert "Relogin required after" not in message
