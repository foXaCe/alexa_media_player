"""
Support to interface with Alexa Devices.

SPDX-License-Identifier: Apache-2.0

For more details about this platform, please refer to the documentation at
https://community.home-assistant.io/t/echo-devices-alexa-as-media-player-testers-needed/58639
"""

import asyncio
from datetime import datetime, timedelta
import functools
from http.cookies import Morsel
from json import JSONDecodeError, loads
import logging
import os
import time
from urllib.parse import urlparse

import aiohttp
from alexapy import (
    AlexaLogin,
    AlexapyConnectionError,
    __version__ as alexapy_version,
    hide_serial as hide_serial,  # re-exported for the platform modules
)
from alexapy.helpers import delete_cookie as alexapy_delete_cookie
from homeassistant.components.persistent_notification import (
    async_create as async_create_persistent_notification,
    async_dismiss as async_dismiss_persistent_notification,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.const import (
    CONF_EMAIL,
    CONF_PASSWORD,
    CONF_URL,
    EVENT_HOMEASSISTANT_STARTED,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.loader import async_get_integration
from homeassistant.util import slugify
import voluptuous as vol

from .config_flow import in_progress_instances
from .const import (
    ALEXA_COMPONENTS,
    CONF_ACCOUNTS,
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_EXTENDED_ENTITY_DISCOVERY,
    CONF_INCLUDE_DEVICES,
    CONF_OAUTH,
    CONF_OTPSECRET,
    CONF_PUBLIC_URL,
    CONF_QUEUE_DELAY,
    CONF_SCAN_INTERVAL,
    DATA_ALEXAMEDIA,
    DATA_LISTENER,
    DEFAULT_EXTENDED_ENTITY_DISCOVERY,
    DEFAULT_PUBLIC_URL,
    DEFAULT_QUEUE_DELAY,
    DEFAULT_SCAN_INTERVAL,
    DEPENDENT_ALEXA_COMPONENTS,
    DOMAIN,
    ISSUE_URL,
    SCAN_INTERVAL,
    STARTUP_MESSAGE,
)
from .coordinator import AlexaMediaCoordinator
from .helpers import (
    calculate_uuid,
    hide_email,
    redact_sensitive,
    safe_get,
)
from .metrics import AlexaMetrics, get_metrics
from .notify import async_unload_entry as notify_async_unload_entry
from .runtime_data import AlexaRuntimeData
from .services import AlexaMediaServices
from .setup import (
    SetupContext,
    coordinator_data as setup_coordinator_data,
    last_called as setup_last_called,
    push as setup_push,
)

# Re-exported so existing call sites in this module and external importers
# (e.g. services.py) keep resolving these names after they moved to setup/.
from .setup.last_called import (
    _async_update_last_called_background,
)

_LOGGER = logging.getLogger(__name__)


ACCOUNT_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_URL): cv.string,
        vol.Optional(CONF_INCLUDE_DEVICES, default=[]): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(CONF_EXCLUDE_DEVICES, default=[]): vol.All(
            cv.ensure_list, [cv.string]
        ),
        vol.Optional(CONF_SCAN_INTERVAL, default=SCAN_INTERVAL): cv.time_period,
        vol.Optional(CONF_QUEUE_DELAY, default=DEFAULT_QUEUE_DELAY): cv.positive_float,
        vol.Optional(CONF_EXTENDED_ENTITY_DISCOVERY, default=False): cv.boolean,
        vol.Optional(CONF_DEBUG, default=False): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ACCOUNTS): vol.All(
                    cv.ensure_list, [ACCOUNT_CONFIG_SCHEMA]
                )
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def _patch_morsel_partitioned() -> None:
    """Make ``http.cookies.Morsel`` tolerant of reserved keys missing on old instances.

    aiohttp >= 3.14 serialises cookies in ``CookieJar.save()`` by reading every
    reserved attribute with ``morsel[attr]``. Morsels restored from a cookie
    pickle written by an older Python/aiohttp -- before the ``partitioned`` key
    existed -- lack that key, so saving raises ``KeyError: 'partitioned'``.
    alexapy surfaces it as a connection error, which blocks setup forever on
    HA 2026.7 / Python 3.14. Returning the default ("") for any missing reserved
    key fixes the save without dropping the existing session. alexapy already
    adds ``partitioned`` to ``Morsel._reserved``; this complements it for cookie
    instances that predate the key.
    """
    if getattr(Morsel, "_alexa_media_partitioned_patch", False):
        return
    _orig_getitem = Morsel.__getitem__

    def _getitem(self, key):
        try:
            return _orig_getitem(self, key)
        except KeyError:
            if key in type(self)._reserved:
                return ""
            raise

    Morsel.__getitem__ = _getitem
    Morsel._alexa_media_partitioned_patch = True


_patch_morsel_partitioned()


def _sanitize_cookies(cookies):
    """Flatten cookies to a plain ``{name: value}`` mapping.

    ``AlexaLogin.load_cookie()`` may return a mapping of ``http.cookies.Morsel``
    objects (for example restored from a pickled aiohttp cookie jar). Reducing
    them to plain string values keeps ``AlexaLogin.login(cookies=...)`` on its
    documented input type and avoids passing stale Morsels around. (The actual
    ``KeyError: 'partitioned'`` crash is fixed by ``_patch_morsel_partitioned``.)
    """
    if not cookies or not hasattr(cookies, "items"):
        return cookies
    return {
        str(key): (value.value if isinstance(value, Morsel) else value)
        for key, value in cookies.items()
    }


async def async_setup(hass, config):
    """Set up the Alexa domain."""
    # Initialize metrics
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["metrics"] = AlexaMetrics(hass)
    metrics = hass.data[DOMAIN]["metrics"]
    metrics.start_boot_tracking()

    integration = await async_get_integration(hass, DOMAIN)
    integration_name = integration.name or "<not available>"
    _LOGGER.info(
        STARTUP_MESSAGE.format(
            name=integration_name,
            ISSUE_URL=ISSUE_URL,
            DOMAIN=DOMAIN,
            version=integration.version,
            alexapy_version=alexapy_version,
        )
    )
    metrics.record_boot_stage("domain_setup")
    if DOMAIN in config:
        async_create_issue(
            hass,
            DOMAIN,
            "deprecated_yaml_configuration",
            is_fixable=False,
            issue_domain=DOMAIN,
            severity=IssueSeverity.ERROR,
            translation_key="deprecated_yaml_configuration",
            learn_more_url="https://github.com/foXaCe/alexa_media_player/wiki/Configuration#configurationyaml",
        )
        _LOGGER.error(
            "YAML configuration of Alexa Media Player is no longer supported. "
            "Please remove `alexa_media` from your configuration, "
            "restart Home Assistant and use the UI to configure it instead. "
            "Settings > Devices & services > Integrations > ADD INTEGRATION"
        )
        return False

    # Register integration-level service actions once, independent of any config
    # entry, so they are always available and can surface clear errors
    # (Bronze quality-scale rule: action-setup).
    hass.data[DOMAIN].setdefault("accounts", {})
    if "services" not in hass.data[DOMAIN]:
        alexa_services = AlexaMediaServices(hass)
        await alexa_services.register()
        hass.data[DOMAIN]["services"] = alexa_services

    return True


# @retry_async(limit=5, delay=5, catch_exceptions=True)
async def async_setup_entry(hass, config_entry):
    """Set up Alexa Media Player as config entry.

    This function uses the new runtime_data pattern for type-safe data storage.
    Legacy hass.data[DATA_ALEXAMEDIA] is maintained for backward compatibility
    during the migration period.
    """
    _boot_start = time.monotonic()

    async def close_alexa_media(event=None) -> None:
        """Clean up Alexa connections."""
        _LOGGER.debug("Received shutdown request: %s", event)
        if accounts := safe_get(hass.data, [DATA_ALEXAMEDIA, "accounts"], {}):
            for email, _ in accounts.items():
                await close_connections(hass, email)

    async def complete_startup(event=None) -> None:
        # pylint: disable=unused-argument
        """Run final tasks after startup."""
        _LOGGER.debug("Completing remaining startup tasks.")
        await asyncio.sleep(10)
        if hass.data[DATA_ALEXAMEDIA].get("notify_service"):
            notify = hass.data[DATA_ALEXAMEDIA].get("notify_service")
            _LOGGER.debug("Refreshing notify targets")
            await notify.async_register_services()

    async def relogin(event=None) -> None:
        """Relogin to Alexa."""
        if hide_email(email) == event.data.get("email"):
            _LOGGER.debug("%s: Received relogin request: %s", hide_email(email), event)
            login_obj: AlexaLogin = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
                "login_obj"
            )
            uuid = (await calculate_uuid(hass, email, url))["uuid"]
            if login_obj is None:
                login_obj = AlexaLogin(
                    url=url,
                    email=email,
                    password=password,
                    outputpath=hass.config.path,
                    debug=account.get(CONF_DEBUG),
                    otp_secret=account.get(CONF_OTPSECRET, ""),
                    oauth=account.get(CONF_OAUTH, {}),
                    uuid=uuid,
                    oauth_login=True,
                )
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["login_obj"] = login_obj
            else:
                login_obj.oauth_login = True
            await login_obj.reset()
            # await login_obj.login()
            if await test_login_status(hass, config_entry, login_obj):
                await setup_alexa(hass, config_entry, login_obj)

    async def login_success(event=None) -> None:
        """Relogin to Alexa."""
        if hide_email(email) == event.data.get("email"):
            _LOGGER.debug("Received Login success: %s", event)
            login_obj: AlexaLogin = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
                "login_obj"
            )
            await setup_alexa(hass, config_entry, login_obj)

    hass.data.setdefault(DATA_ALEXAMEDIA, {})
    hass.data[DATA_ALEXAMEDIA].setdefault("accounts", {})
    hass.data[DATA_ALEXAMEDIA].setdefault("config_flows", {})
    hass.data[DATA_ALEXAMEDIA].setdefault("notify_service", None)
    account = config_entry.data
    email = account.get(CONF_EMAIL)
    password = account.get(CONF_PASSWORD)
    url = account.get(CONF_URL)
    hass.data[DATA_ALEXAMEDIA]["accounts"].setdefault(
        email,
        {
            "coordinator": None,
            "config_entry": config_entry,
            "setup_alexa": setup_alexa,
            "devices": {
                "media_player": {},
                "switch": {},
                "guard": [],
                "light": [],
                "binary_sensor": [],
                "temperature": [],
                "smart_switch": [],
            },
            "entities": {
                "media_player": {},
                "switch": {},
                "sensor": {},
                "light": [],
                "binary_sensor": [],
                "alarm_control_panel": {},
                "smart_switch": [],
            },
            "excluded": {},
            "new_devices": True,
            "http2_lastattempt": 0,
            "http2error": 0,
            "http2_commands": {},
            "http2_activity": {"serials": {}, "refreshed": {}},
            "http2": None,
            "auth_info": None,
            "second_account_index": 0,
            "should_get_network": True,
            "first_run": True,
            "notifications": {},  # already used for the raw notifications dict
            "notifications_pending": set(),  # doppler serials that need a refresh
            "notifications_refresh_task": None,  # running task or None
            "notifications_retry_count": 0,  # simple backoff counter
            "options": {
                CONF_INCLUDE_DEVICES: config_entry.data.get(CONF_INCLUDE_DEVICES, ""),
                CONF_EXCLUDE_DEVICES: config_entry.data.get(CONF_EXCLUDE_DEVICES, ""),
                CONF_QUEUE_DELAY: config_entry.data.get(
                    CONF_QUEUE_DELAY, DEFAULT_QUEUE_DELAY
                ),
                CONF_SCAN_INTERVAL: config_entry.data.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                ),
                CONF_PUBLIC_URL: config_entry.data.get(
                    CONF_PUBLIC_URL, DEFAULT_PUBLIC_URL
                ),
                CONF_EXTENDED_ENTITY_DISCOVERY: config_entry.data.get(
                    CONF_EXTENDED_ENTITY_DISCOVERY, DEFAULT_EXTENDED_ENTITY_DISCOVERY
                ),
                CONF_DEBUG: config_entry.data.get(CONF_DEBUG, False),
            },
            DATA_LISTENER: [config_entry.add_update_listener(update_listener)],
        },
    )
    uuid_dict = await calculate_uuid(hass, email, url)
    uuid = uuid_dict["uuid"]
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["second_account_index"] = uuid_dict[
        "index"
    ]
    login: AlexaLogin = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
        "login_obj",
        AlexaLogin(
            url=url,
            email=email,
            password=password,
            outputpath=hass.config.path,
            debug=account.get(CONF_DEBUG),
            otp_secret=account.get(CONF_OTPSECRET, ""),
            oauth=account.get(CONF_OAUTH, {}),
            uuid=uuid,
            oauth_login=True,
        ),
    )
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["login_obj"] = login
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["last_push_activity"] = 0

    # Create runtime_data for optimized architecture
    # This provides type-safe access to account data
    if not hasattr(config_entry, "runtime_data") or config_entry.runtime_data is None:
        config_entry.runtime_data = AlexaRuntimeData(
            login_obj=login,
            config_entry=config_entry,
            second_account_index=uuid_dict["index"],
        )
    if not hass.data[DATA_ALEXAMEDIA]["accounts"][email]["second_account_index"]:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_alexa_media)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, complete_startup)
    hass.bus.async_listen("alexa_media_relogin_required", relogin)
    hass.bus.async_listen("alexa_media_relogin_success", login_success)
    try:
        _t = time.monotonic()
        cookies = _sanitize_cookies(await login.load_cookie())
        cookie_login_ok = False
        if login._session is None or getattr(login._session, "closed", False):
            login._create_session(True)
        # aiohttp >= 3.14 saves cookies in a JSON format that AlexaLogin's
        # pickle-based load_cookie() cannot read, so `cookies` is empty there.
        # Reload the cache into the session jar with aiohttp's own (JSON-aware)
        # loader, off the event loop, so the regional bootstrap probe below can
        # reuse a cached session instead of forcing a full credential login.
        cookiefile = (
            login._cookiefile[0] if getattr(login, "_cookiefile", None) else None
        )
        if cookiefile and os.path.exists(cookiefile):
            try:
                await hass.async_add_executor_job(
                    login._session.cookie_jar.load, cookiefile
                )
            except Exception as ex:  # noqa: BLE001
                # Best-effort preload only. alexapy >= 1.29.24 persists cookies in
                # its own versioned JSON format, which aiohttp's CookieJar.load
                # cannot parse; alexapy's load_cookie() handles it, so any failure
                # here is non-fatal.
                _LOGGER.debug("[BOOT] Could not preload cookie jar: %s", ex)
        try:
            # Use the account's regional Alexa host (e.g. alexa.amazon.fr), not a
            # hardcoded alexa.amazon.com, so non-US accounts get the fast path too.
            async with login._session.get(
                f"https://alexa.{login.url}/api/bootstrap",
                cookies=cookies,
                ssl=login._ssl,
                allow_redirects=False,
            ) as response:
                if response.status == 200:
                    data = loads(await response.text())
                    auth = (data or {}).get("authentication") or {}
                    customer_email = (auth.get("customerEmail") or "").lower()
                    if auth.get("authenticated") and customer_email == email.lower():
                        _LOGGER.debug("[BOOT] Cookie auth confirmed via /api/bootstrap")
                        login.status["login_successful"] = True
                        login.customer_id = auth.get("customerId")
                        login.stats["login_timestamp"] = datetime.now()
                        login.stats["api_calls"] = 0
                        await login.check_domain()
                        await login.finalize_login()
                        cookie_login_ok = True
        except (JSONDecodeError, ValueError, aiohttp.ClientError, KeyError) as ex:
            _LOGGER.debug("[BOOT] Bootstrap cookie auth check failed: %s", ex)
        if not cookie_login_ok:
            await login.login(cookies=cookies)
        _LOGGER.debug("[BOOT] login completed in %.2fs", time.monotonic() - _t)
        _t = time.monotonic()
        if await test_login_status(hass, config_entry, login):
            _LOGGER.debug("[BOOT] test_login_status in %.2fs", time.monotonic() - _t)
            _t = time.monotonic()
            await setup_alexa(hass, config_entry, login)
            _LOGGER.debug(
                "[BOOT] setup_entry total: %.2fs", time.monotonic() - _boot_start
            )
            return True
        return False
    except AlexapyConnectionError as err:
        raise ConfigEntryNotReady(str(err) or "Connection Error during login") from err


async def setup_alexa(hass, config_entry, login_obj: AlexaLogin):
    # pylint: disable=too-many-statements,too-many-locals
    """Set up a alexa api based on host parameter."""

    debug = config_entry.data.get(CONF_DEBUG, False)

    # Record metrics
    metrics = get_metrics(hass)
    email = login_obj.email
    if metrics:
        metrics.record_boot_stage(f"setup_alexa_start_{hide_email(email)}")

    # Shared per-entry state for the extracted setup/ helpers (DND throttling, ...).
    # Recreated on every (re)login, matching the previous closure-based behaviour.
    ctx = SetupContext(
        hass=hass,
        config_entry=config_entry,
        email=email,
        debug=debug,
        metrics=metrics,
        login_obj=login_obj,
    )

    # ---------------------------------------------------------------------
    # Near-real-time last_called probing (customer history), single worker
    # Initialize ONCE per account (setup_alexa), not inside http2_handler.
    # ---------------------------------------------------------------------

    _LOGGER.debug("Setting up Alexa devices for %s", hide_email(login_obj.email))
    config = config_entry.data
    email = config.get(CONF_EMAIL)
    include = (
        cv.ensure_list_csv(config[CONF_INCLUDE_DEVICES])
        if config[CONF_INCLUDE_DEVICES]
        else ""
    )
    _LOGGER.debug("include: %s", include)
    exclude = (
        cv.ensure_list_csv(config[CONF_EXCLUDE_DEVICES])
        if config[CONF_EXCLUDE_DEVICES]
        else ""
    )
    _LOGGER.debug("exclude: %s", exclude)
    scan_interval: float = (
        config.get(CONF_SCAN_INTERVAL).total_seconds()
        if isinstance(config.get(CONF_SCAN_INTERVAL), timedelta)
        else config.get(CONF_SCAN_INTERVAL)
    )
    ctx.scan_interval = scan_interval
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["login_obj"] = login_obj

    # Initialize the per-account probe worker exactly once here (not per push message).
    setup_last_called._init_last_called_probe_worker(
        ctx, hass.data[DATA_ALEXAMEDIA]["accounts"][email]
    )

    _t = time.monotonic()
    http2_enabled = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
        "http2"
    ] = await setup_push.http2_connect(ctx)
    _LOGGER.debug("[BOOT] http2_connect in %.2fs", time.monotonic() - _t)
    coordinator = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get("coordinator")

    # Get runtime_data for optimized coordinator
    runtime_data = (
        config_entry.runtime_data if hasattr(config_entry, "runtime_data") else None
    )

    if coordinator is None:
        _LOGGER.debug("%s: Creating optimized coordinator", hide_email(email))

        # Use optimized coordinator with debouncing
        coordinator = AlexaMediaCoordinator(
            hass=hass,
            runtime_data=runtime_data,
            update_method=functools.partial(
                setup_coordinator_data.async_update_data, ctx
            ),
            scan_interval=scan_interval,
        )

        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["coordinator"] = coordinator
        # Set correct interval now that http2 status is known
        coordinator.set_http2_status(bool(http2_enabled))

        # Also store in runtime_data for type-safe access
        if runtime_data:
            runtime_data.coordinator = coordinator
    else:
        _LOGGER.debug("%s: setup_alexa: Reusing coordinator", hide_email(email))
        # Use the optimized set_http2_status method if available
        if isinstance(coordinator, AlexaMediaCoordinator):
            coordinator.set_http2_status(bool(http2_enabled))
        else:
            coordinator.update_interval = timedelta(
                seconds=scan_interval * 10 if http2_enabled else scan_interval
            )
    # Fetch initial data
    _LOGGER.debug("%s: setup_alexa: Starting coordinator refresh", hide_email(email))
    _t = time.monotonic()
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("[BOOT] first_refresh in %.2fs", time.monotonic() - _t)

    # Service actions are registered once in async_setup (action-setup), so there
    # is nothing to register per config entry here.

    # Update last_called in background to avoid blocking
    _LOGGER.debug("%s: setup_alexa: Scheduling last_called update", hide_email(email))
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["last_called_init_task"] = (
        hass.async_create_background_task(
            _async_update_last_called_background(hass, login_obj, email),
            f"{DOMAIN}_last_called_init",
        )
    )

    return True


async def async_unload_entry(hass, entry) -> bool:
    """Unload a config entry"""
    email = entry.data["email"]
    _LOGGER.debug("Unloading entry: %s", hide_email(email))
    for task_key in (
        "notifications_refresh_task",
        "notifications_init_task",
        "last_called_init_task",
        "service_update_last_called_task",
    ):
        accounts = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
        account = accounts.get(email)
        if not account:
            return True
        task = account.get(task_key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                # Expected during unload/shutdown
                pass
    last_called_task = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
        "last_called_probe_task"
    )
    if last_called_task and not last_called_task.done():
        last_called_task.cancel()
        try:
            await last_called_task
        except asyncio.CancelledError:
            # Expected during unload/shutdown
            pass
        except Exception as err:  # pragma: no cover
            _LOGGER.debug(
                "%s: Exception while cancelling last_called_probe_task: %s",
                hide_email(email),
                err,
            )
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["last_called_probe_task"] = None

    debouncer = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
        "confirm_refresh_debouncer"
    )
    if debouncer:
        debouncer.async_cancel()
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["confirm_refresh_debouncer"] = (
            None
        )

    for component in ALEXA_COMPONENTS + DEPENDENT_ALEXA_COMPONENTS:
        try:
            if component == "notify":
                await notify_async_unload_entry(hass, entry)
            else:
                _LOGGER.debug("Forwarding unload entry to %s", component)
                await hass.config_entries.async_forward_entry_unload(entry, component)
        except Exception:
            _LOGGER.error("Error unloading: %s", component)
    await close_connections(hass, email)
    for listener in hass.data[DATA_ALEXAMEDIA]["accounts"][email][DATA_LISTENER]:
        listener()
    hass.data[DATA_ALEXAMEDIA]["accounts"].pop(email)
    # Clean up config flows in progress
    flows_to_remove = []
    if hass.data[DATA_ALEXAMEDIA].get("config_flows"):
        for key, flow in hass.data[DATA_ALEXAMEDIA]["config_flows"].items():
            if key.startswith(email) and flow:
                _LOGGER.debug("Aborting flow %s %s", key, flow)
                flows_to_remove.append(key)
                try:
                    hass.config_entries.flow.async_abort(flow.get("flow_id"))
                except UnknownFlow:
                    pass
        for flow in flows_to_remove:
            hass.data[DATA_ALEXAMEDIA]["config_flows"].pop(flow)
    # Clean up hass.data
    if not hass.data[DATA_ALEXAMEDIA].get("accounts"):
        _LOGGER.debug("Removing accounts data")
        hass.data[DATA_ALEXAMEDIA].pop("accounts")
        # Service actions are owned by async_setup and intentionally remain
        # registered for the integration lifetime (action-setup); async_setup is
        # not re-run on entry reload, so unregistering here would drop them.
    if hass.data[DATA_ALEXAMEDIA].get("config_flows") == {}:
        _LOGGER.debug("Removing config_flows data")
        async_dismiss_persistent_notification(
            hass, f"alexa_media_{slugify(email)}{slugify((entry.data['url'])[7:])}"
        )
        hass.data[DATA_ALEXAMEDIA].pop("config_flows")
    if not hass.data[DATA_ALEXAMEDIA]:
        _LOGGER.debug("Removing alexa_media data structure")
        if hass.data.get(DATA_ALEXAMEDIA):
            hass.data.pop(DATA_ALEXAMEDIA)
    else:
        _LOGGER.debug(
            "Unable to remove alexa_media data structure: %s",
            hass.data.get(DATA_ALEXAMEDIA),
        )
    _LOGGER.debug("Unloaded entry for %s", hide_email(email))
    return True


async def async_remove_entry(hass, entry) -> bool:
    """Handle removal of an entry."""
    email = entry.data["email"]
    obfuscated_email = hide_email(email)
    _LOGGER.debug("Removing config entry: %s", hide_email(email))
    login_obj = AlexaLogin(
        url="",
        email=email,
        password="",  # nosec
        outputpath=hass.config.path,
    )
    # Delete cookiefile
    cookiefile = hass.config.path(f".storage/{DOMAIN}.{email}.pickle")
    obfuscated_cookiefile = hass.config.path(
        f".storage/{DOMAIN}.{obfuscated_email}.pickle"
    )
    if callable(getattr(AlexaLogin, "delete_cookiefile", None)):
        try:
            await login_obj.delete_cookiefile()
            _LOGGER.debug("Cookiefile %s deleted.", obfuscated_cookiefile)
        except Exception as ex:
            _LOGGER.error(
                "delete_cookiefile() exception: %s;"
                " Manually delete cookiefile before re-adding the integration: %s",
                ex,
                obfuscated_cookiefile,
            )
    else:
        if os.path.exists(cookiefile):
            try:
                await alexapy_delete_cookie(cookiefile)
                _LOGGER.debug(
                    "Successfully deleted cookiefile: %s", obfuscated_cookiefile
                )
            except (OSError, EOFError, TypeError, AttributeError) as ex:
                _LOGGER.error(
                    "alexapy_delete_cookie() exception: %s;"
                    " Manually delete cookiefile before re-adding the integration: %s",
                    ex,
                    obfuscated_cookiefile,
                )
        else:
            _LOGGER.error("Cookiefile not found: %s", obfuscated_cookiefile)
    _LOGGER.debug("Config entry %s removed.", obfuscated_email)
    return True


async def close_connections(hass, email: str) -> None:
    """Clear open aiohttp connections for email."""
    if (
        email not in hass.data[DATA_ALEXAMEDIA]["accounts"]
        or "login_obj" not in hass.data[DATA_ALEXAMEDIA]["accounts"][email]
    ):
        return
    account_dict = hass.data[DATA_ALEXAMEDIA]["accounts"][email]
    login_obj = account_dict["login_obj"]
    await login_obj.save_cookiefile()
    await login_obj.close()
    _LOGGER.debug(
        "%s: Connection closed: %s", hide_email(email), login_obj.session.closed
    )


async def update_listener(hass, config_entry):
    """Update when config_entry options update."""
    account = config_entry.data
    email = account.get(CONF_EMAIL)
    reload_needed: bool = False
    for key, old_value in hass.data[DATA_ALEXAMEDIA]["accounts"][email][
        "options"
    ].items():
        new_value = config_entry.data.get(key)
        if new_value is not None and new_value != old_value:
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["options"][key] = new_value
            _LOGGER.debug(
                "Option %s changed from %s to %s",
                key,
                old_value,
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["options"][key],
            )
            reload_needed = True
    if reload_needed:
        await hass.config_entries.async_reload(config_entry.entry_id)
        _LOGGER.debug(
            "%s options reloaded",
            hass.data[DATA_ALEXAMEDIA]["accounts"][email],
        )


async def test_login_status(hass, config_entry, login) -> bool:
    """Test the login status and spawn requests for info."""

    _LOGGER.debug("Testing login status: %s", login.status)
    if login.status and login.status.get("login_successful"):
        return True
    account = config_entry.data
    _LOGGER.debug(
        "Logging in: %s %s", redact_sensitive(account), in_progress_instances(hass)
    )
    _LOGGER.debug("Login stats: %s", login.stats)
    message: str = f"Reauthenticate {login.email} on the [Integrations](/config/integrations) page. "
    if login.stats.get("login_timestamp") != datetime(1, 1, 1):
        elaspsed_time: str = str(datetime.now() - login.stats.get("login_timestamp"))
        api_calls: int = login.stats.get("api_calls")
        message += f"Relogin required after {elaspsed_time} and {api_calls} api calls."
    host = urlparse(login.url).hostname or login.url
    async_create_persistent_notification(
        hass,
        title="Alexa Media Reauthentication Required",
        message=message,
        notification_id=f"alexa_media_{slugify(login.email)}_{slugify(host)}",
    )
    flow = hass.data[DATA_ALEXAMEDIA]["config_flows"].get(
        f"{account[CONF_EMAIL]} - {account[CONF_URL]}"
    )
    if flow:
        if flow.get("flow_id") in in_progress_instances(hass):
            _LOGGER.debug("Existing config flow detected")
            return False
        _LOGGER.debug("Stopping orphaned config flow %s", flow.get("flow_id"))
        try:
            hass.config_entries.flow.async_abort(flow.get("flow_id"))
        except UnknownFlow:
            pass
        hass.data[DATA_ALEXAMEDIA]["config_flows"][
            f"{account[CONF_EMAIL]} - {account[CONF_URL]}"
        ] = None
    _LOGGER.debug("Creating new config flow to login")
    config_entry.async_start_reauth(
        hass,
        context={"source": SOURCE_REAUTH},
        data={
            CONF_EMAIL: account[CONF_EMAIL],
            CONF_PASSWORD: account[CONF_PASSWORD],
            CONF_URL: account[CONF_URL],
            CONF_DEBUG: account[CONF_DEBUG],
            CONF_INCLUDE_DEVICES: account[CONF_INCLUDE_DEVICES],
            CONF_EXCLUDE_DEVICES: account[CONF_EXCLUDE_DEVICES],
            CONF_SCAN_INTERVAL: (
                account[CONF_SCAN_INTERVAL].total_seconds()
                if isinstance(account[CONF_SCAN_INTERVAL], timedelta)
                else account[CONF_SCAN_INTERVAL]
            ),
            CONF_OTPSECRET: account.get(CONF_OTPSECRET, ""),
        },
    )
    try:
        flow_obj = config_entry.async_get_active_flows(hass, {SOURCE_REAUTH}).__next__()
        hass.data[DATA_ALEXAMEDIA]["config_flows"][
            f"{account[CONF_EMAIL]} - {account[CONF_URL]}"
        ] = flow_obj
    except StopIteration:
        _LOGGER.debug("A new config flow could not be created.")
    return False
