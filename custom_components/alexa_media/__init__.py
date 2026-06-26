"""
Support to interface with Alexa Devices.

SPDX-License-Identifier: Apache-2.0

For more details about this platform, please refer to the documentation at
https://community.home-assistant.io/t/echo-devices-alexa-as-media-player-testers-needed/58639
"""

import asyncio
from datetime import datetime, timedelta
from http.cookies import Morsel
from json import JSONDecodeError, loads
import logging
import os
import time
from urllib.parse import urlparse

import aiohttp
from alexapy import (
    AlexaAPI,
    AlexaLogin,
    AlexapyConnectionError,
    AlexapyLoginError,
    HTTP2EchoClient,
    __version__ as alexapy_version,
    hide_serial,
    obfuscate,
)
from alexapy.errors import AlexapyTooManyRequestsError
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
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.loader import async_get_integration
from homeassistant.util import slugify
import voluptuous as vol

from .alexa_entity import AlexaEntityData, get_entity_data, parse_alexa_entities
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
    HTTP2_ERROR_THRESHOLD,
    ISSUE_URL,
    LAST_CALLED_COALESCE_WINDOW_MS,
    LAST_PING_MAX_AGE_SECONDS,
    LAST_PUSH_INACTIVITY_SECONDS,
    SCAN_INTERVAL,
    STARTUP_MESSAGE,
)
from .coordinator import AlexaMediaCoordinator
from .exceptions import TimeoutException
from .helpers import (
    _entity_backed_serials,
    _existing_serials,
    _network_allowed,
    calculate_uuid,
    hide_email,
    safe_get,
)
from .metrics import AlexaMetrics, get_metrics
from .notify import async_unload_entry as notify_async_unload_entry
from .runtime_data import AlexaRuntimeData
from .services import AlexaMediaServices
from .setup import (
    SetupContext,
    bluetooth as setup_bluetooth,
    dnd as setup_dnd,
    last_called as setup_last_called,
    notifications as setup_notifications,
)

# Re-exported so existing call sites in this module and external importers
# (e.g. services.py) keep resolving these names after they moved to setup/.
from .setup.last_called import (
    _async_update_last_called_background,
    _async_update_last_called_global,
    _queue_last_called_activity,
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


def _push_healthy(account: dict) -> bool:
    """Return True if HTTP2 push is likely usable (enough to skip polling last_called)."""
    http2 = account.get("http2")
    if not http2:
        return False

    # Hard negative: the underlying transport is closed.
    client = getattr(http2, "client", None)
    if client is not None and getattr(client, "is_closed", False):
        return False

    # If alexapy has already driven error count to "give up", treat as down.
    if int(account.get("http2error") or 0) >= HTTP2_ERROR_THRESHOLD:
        return False

    last_push = float(account.get("last_push_activity") or 0.0)
    if last_push and (time.time() - last_push) > LAST_PUSH_INACTIVITY_SECONDS:
        return False

    # If we have a recent ping, that's a strong positive.
    last_ping_dt = getattr(http2, "_last_ping", None)  # private, best-effort
    if last_ping_dt:
        try:
            age = time.time() - last_ping_dt.timestamp()
            # ping is ~299s; allow generous slack for scheduler jitter.
            if age <= LAST_PING_MAX_AGE_SECONDS:
                return True
            # If ping is *very* stale, treat as suspicious but not definitive.
            # Don't force False here unless you also have other negative signals.
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not evaluate http2 ping age: %s", exc)

    # Unknown state: object exists and client isn't closed -> assume usable.
    return True


def _entity_backed_device_identifiers(account_dict: dict) -> set[str]:
    """Collect device identifier strings for devices that are backed by entities.

    Alexa Media Player historically prunes stale HA Device Registry entries by comparing
    device identifiers against the current *media_player* serials. That works for Echoes,
    but fails for entity-only devices (e.g., Amazon Indoor Air Quality Monitor) which have
    no media_player entity. Those devices would get pruned unless we also consider the
    identifiers referenced by entities we created.
    """
    identifiers: set[str] = set()

    def _collect_from_device_info(device_info) -> None:
        if not device_info:
            return
        try:
            # dr.DeviceInfo
            di_idents = getattr(device_info, "identifiers", None)
            if di_idents:
                for ident in di_idents:
                    if isinstance(ident, tuple) and len(ident) == 2:
                        identifiers.add(ident[1])
                return
        except Exception as exc:  # pylint: disable=broad-except
            _LOGGER.debug("Could not extract identifiers from device_info: %s", exc)
        # dict-style device_info
        if isinstance(device_info, dict):
            di_idents = device_info.get("identifiers")
            if di_idents:
                for ident in di_idents:
                    if isinstance(ident, tuple) and len(ident) == 2:
                        identifiers.add(ident[1])

    # Recursively walk nested entity structures (dict/list/tuple/set) and collect any device_info found.
    def _walk(obj) -> None:
        if obj is None:
            return

        # Entity-ish object
        _collect_from_device_info(getattr(obj, "device_info", None))

        if isinstance(obj, dict):
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, (list, tuple, set)):
            for v in obj:
                _walk(v)

    _walk(account_dict.get("entities", {}))
    return identifiers


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
    )

    async def async_update_data() -> AlexaEntityData | None:
        # noqa pylint: disable=too-many-branches
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.

        This will ping Alexa API to identify all devices, bluetooth, and the last
        called device.

        If any guards, sensors, switches or lights are configured, their current state will be acquired.
        This data is returned directly so that it is available on the coordinator.

        This will add new devices and services when discovered. By default this
        runs every SCAN_INTERVAL seconds unless another method calls it. if
        push is connected, it will increase the delay 10-fold between updates.
        While throttled at MIN_TIME_BETWEEN_SCANS, care should be taken to
        reduce the number of runs to avoid flooding. Slow changing states
        should be checked here instead of in spawned components like
        media_player since this object is one per account.
        Each AlexaAPI call generally results in two webpage requests.
        """
        email = config.get(CONF_EMAIL)
        accounts = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
        account = accounts.get(email)
        if not account:
            return None

        login_obj = account.get("login_obj")
        if not login_obj or not _network_allowed(login_obj):
            return None
        account = hass.data[DATA_ALEXAMEDIA]["accounts"][email]
        existing_serials = set(_existing_serials(hass, login_obj))
        existing_serials |= _entity_backed_serials(account)
        existing_entities = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"][
            "media_player"
        ].values()
        auth_info = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get("auth_info")
        new_devices = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["new_devices"]
        extended_entity_discovery = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
            "options"
        ].get(CONF_EXTENDED_ENTITY_DISCOVERY)
        should_get_network = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
            "should_get_network"
        ]
        first_run = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["first_run"]
        devices = {}
        bluetooth = {}
        preferences = {}
        dnd = {}
        entity_state = {}

        # Try to get cached data for faster boot
        entry_id = config_entry.entry_id if config_entry else ""
        cache_key_prefix = f"{email}_{entry_id}"
        cached_devices = None
        _used_cached_devices = False
        if metrics:
            cached_devices = metrics.api_cache.get(f"{cache_key_prefix}_devices")

        if cached_devices and not new_devices:
            _LOGGER.debug("%s: Using cached devices data", hide_email(email))
            # NOTE: DataCache returns direct references. We intentionally enrich device dicts
            # in-place each refresh cycle (bluetooth_state/locale/dnd/etc.).
            devices = cached_devices
            _used_cached_devices = True
            # Still need fresh bluetooth, preferences, and DND
            tasks = [
                AlexaAPI.get_bluetooth(login_obj),
                AlexaAPI.get_device_preferences(login_obj),
                AlexaAPI.get_dnd_state(login_obj),
            ]
        else:
            tasks = [
                AlexaAPI.get_devices(login_obj),
                AlexaAPI.get_bluetooth(login_obj),
                AlexaAPI.get_device_preferences(login_obj),
                AlexaAPI.get_dnd_state(login_obj),
            ]
        if new_devices:
            tasks.append(AlexaAPI.get_authentication(login_obj))

        entities_to_monitor = set()

        # Temperature sensors (stored as entities["sensor"][serial]["Temperature"] = sensor)
        for per_serial in hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"][
            "sensor"
        ].values():
            if not isinstance(per_serial, dict):
                continue

            temp = per_serial.get("Temperature")
            if temp and temp.enabled:
                entities_to_monitor.add(temp.alexa_entity_id)

            # Air Quality sensors:
            # entities["sensor"][serial]["Air_Quality"][unique_id] = sensor
            airq = per_serial.get("Air_Quality")
            if isinstance(airq, dict):
                for aq_sensor in airq.values():
                    if aq_sensor and aq_sensor.enabled:
                        entities_to_monitor.add(aq_sensor.alexa_entity_id)
            elif airq and getattr(airq, "enabled", False):
                # Backwards compat if some installs still have a single sensor stored
                entities_to_monitor.add(airq.alexa_entity_id)

        for light in hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"].get(
            "light", []
        ):
            if light.enabled:
                entities_to_monitor.add(light.alexa_entity_id)

        for binary_sensor in hass.data[DATA_ALEXAMEDIA]["accounts"][email][
            "entities"
        ].get("binary_sensor", []):
            if binary_sensor.enabled:
                entities_to_monitor.add(binary_sensor.alexa_entity_id)

        for guard in (
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"]
            .get("alarm_control_panel", {})
            .values()
        ):
            if guard.enabled:
                entities_to_monitor.add(guard.unique_id)

        for smart_switch in hass.data[DATA_ALEXAMEDIA]["accounts"][email][
            "entities"
        ].get("smart_switch", []):
            if smart_switch.enabled:
                entities_to_monitor.add(smart_switch.alexa_entity_id)

        if entities_to_monitor:
            tasks.append(get_entity_data(login_obj, list(entities_to_monitor)))

        if should_get_network:
            tasks.append(AlexaAPI.get_network_details(login_obj))

        optional_task_results = []
        try:
            if tasks:
                # Note: asyncio.TimeoutError and aiohttp.ClientError are already
                # handled by the data update coordinator.
                # Increase timeout from 30s to 45s to permit
                # get_network_details() retries which could up to 30s.
                async with asyncio.timeout(45):
                    start_fetch = time.monotonic()
                    if _used_cached_devices:
                        (
                            bluetooth,
                            preferences,
                            dnd,
                            *optional_task_results,
                        ) = await asyncio.gather(*tasks)
                    else:
                        (
                            devices,
                            bluetooth,
                            preferences,
                            dnd,
                            *optional_task_results,
                        ) = await asyncio.gather(*tasks)

                    fetch_time = time.monotonic() - start_fetch
                    _LOGGER.debug(
                        "[BOOT] API fetch (%d tasks, cached=%s) in %.2fs",
                        len(tasks) if tasks else 0,
                        _used_cached_devices,
                        fetch_time,
                    )
                    # Record API call metrics
                    if metrics:
                        metrics.record_api_call("initial_fetch", fetch_time)
                        # Cache the devices for faster next boot (only freshly fetched)
                        if not _used_cached_devices:
                            metrics.api_cache.cache_set(
                                f"{cache_key_prefix}_devices", devices
                            )

                    _t_post = time.monotonic()
                    if should_get_network and optional_task_results:
                        # First run is a special case. Get the state of all entities(including disabled)
                        # This ensures all entities have state during startup without needing to request coordinator refresh

                        _LOGGER.info(
                            "%s: Network Discovery: Checking", hide_email(email)
                        )
                        api_devices = optional_task_results.pop()
                        if not api_devices:
                            _LOGGER.warning(
                                "%s: Network Discovery: AlexaAPI returned an unexpected response. Retrying on next polling cycle",
                                hide_email(email),
                            )
                        else:
                            _LOGGER.debug(
                                "%s: Network Discovery: Success, processing response",
                                hide_email(email),
                            )
                            # Only process this once after success
                            hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                                "should_get_network"
                            ] = False

                            # Discard the entities_to_monitor results since we now have full network details
                            if entities_to_monitor and optional_task_results:
                                optional_task_results.pop()
                                entities_to_monitor.clear()

                        alexa_entities = parse_alexa_entities(
                            api_devices,
                            debug=hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                                "options"
                            ].get(CONF_DEBUG, False),
                        )
                        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"].update(
                            alexa_entities
                        )

                        _entities_to_monitor = set()
                        for type_of_entity, entities in alexa_entities.items():
                            if (
                                type_of_entity
                                in {"guard", "temperature", "air_quality", "aiaqm"}
                                or extended_entity_discovery
                            ):
                                for entity in entities:
                                    _entities_to_monitor.add(entity.get("id"))
                                    _LOGGER.debug("Monitoring: %s", entity.get("name"))
                        _LOGGER.debug(
                            "%s: Network Discovery: %s entities will be monitored",
                            hide_email(email),
                            len(list(_entities_to_monitor)),
                        )
                        # Use shorter timeout for entity data to avoid blocking
                        _t_ed = time.monotonic()
                        try:
                            entity_state = await asyncio.wait_for(
                                get_entity_data(login_obj, list(_entities_to_monitor)),
                                timeout=10.0,
                            )
                        except TimeoutError:
                            _LOGGER.warning(
                                "%s: get_entity_data timed out after 10s, "
                                "entity states will be fetched on next cycle",
                                hide_email(email),
                            )
                        _LOGGER.debug(
                            "[BOOT] get_entity_data (network) in %.2fs",
                            time.monotonic() - _t_ed,
                        )

                if entities_to_monitor and optional_task_results:
                    entity_state = optional_task_results.pop()
                    _LOGGER.debug(
                        "%s: Processing %s entities to monitor",
                        hide_email(email),
                        len(list(entities_to_monitor)),
                    )

                if new_devices and optional_task_results:
                    auth_info = optional_task_results.pop()
                    _LOGGER.debug(
                        "%s: Found %s devices, %s bluetooth",
                        hide_email(email),
                        len(devices) if devices is not None else "",
                        (
                            len(bluetooth.get("bluetoothStates", []))
                            if bluetooth is not None
                            else ""
                        ),
                    )

                # Process notifications in background to avoid blocking boot
                # (process_notifications has a 4s sleep + API call)
                _LOGGER.debug(
                    "[BOOT] post-fetch processing in %.2fs", time.monotonic() - _t_post
                )

                existing_notif_task = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get(
                    "notifications_init_task"
                )
                if existing_notif_task and not existing_notif_task.done():
                    _LOGGER.debug(
                        "%s: Notifications background task already running, skipping",
                        hide_email(email),
                    )
                else:

                    async def _bg_process_notifications():
                        try:
                            await setup_notifications.process_notifications(
                                login_obj, ctx
                            )
                        except (
                            TimeoutError,
                            AlexapyConnectionError,
                            AlexapyLoginError,
                            JSONDecodeError,
                        ):
                            _LOGGER.debug(
                                "%s: Background notifications failed, retrying once",
                                hide_email(email),
                            )
                            try:
                                await asyncio.sleep(5)
                                await setup_notifications.process_notifications(
                                    login_obj, ctx
                                )
                            except (
                                TimeoutError,
                                AlexapyConnectionError,
                                AlexapyLoginError,
                                JSONDecodeError,
                            ):
                                _LOGGER.debug(
                                    "%s: Background notifications retry failed",
                                    hide_email(email),
                                )

                    hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                        "notifications_init_task"
                    ] = hass.async_create_background_task(
                        _bg_process_notifications(),
                        f"{DOMAIN}_notifications_init",
                    )

        except (AlexapyLoginError, JSONDecodeError):
            _LOGGER.debug(
                "%s: Alexa API disconnected; attempting to relogin : status %s",
                hide_email(email),
                login_obj.status,
            )
            if login_obj.status:
                hass.bus.async_fire(
                    "alexa_media_relogin_required",
                    event_data={"email": hide_email(email), "url": login_obj.url},
                )
            return None
        except asyncio.CancelledError:
            # Task cancelled during unload/shutdown; propagate cancellation.
            raise
        except (AlexapyConnectionError, AlexapyTooManyRequestsError) as err:
            # Surface transient cloud failures to the coordinator as UpdateFailed so
            # last_update_success flips to False (entities become unavailable) and
            # the first refresh raises ConfigEntryNotReady instead of logging an
            # unexpected error with a full traceback.
            raise UpdateFailed(f"Error communicating with Alexa API: {err}") from err

        _t_proc = time.monotonic()
        new_alexa_clients = []  # list of newly discovered device names
        exclude_filter = []
        include_filter = []

        for device in devices:
            serial = device["serialNumber"]
            dev_name = device["accountName"]
            if include and dev_name not in include:
                include_filter.append(dev_name)
                if "appDeviceList" in device:
                    for app in device["appDeviceList"]:
                        (
                            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"][
                                app["serialNumber"]
                            ]
                        ) = device
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"][serial] = (
                    device
                )
                continue
            if exclude and dev_name in exclude:
                exclude_filter.append(dev_name)
                if "appDeviceList" in device:
                    for app in device["appDeviceList"]:
                        (
                            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"][
                                app["serialNumber"]
                            ]
                        ) = device
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"][serial] = (
                    device
                )
                continue

            if (
                dev_name not in include_filter
                and device.get("capabilities")
                and not any(
                    x in device["capabilities"]
                    for x in ["MUSIC_SKILL", "TIMERS_AND_ALARMS", "REMINDERS"]
                )
            ):
                # skip devices without music or notification skill
                _LOGGER.debug("Excluding %s for lacking capability", dev_name)
                continue

            if bluetooth is not None and "bluetoothStates" in bluetooth:
                for b_state in bluetooth["bluetoothStates"]:
                    if serial == b_state["deviceSerialNumber"]:
                        device["bluetooth_state"] = b_state
                        break

            if preferences is not None and "devicePreferences" in preferences:
                for dev in preferences["devicePreferences"]:
                    if dev["deviceSerialNumber"] == serial:
                        device["locale"] = dev["locale"]
                        device["timeZoneId"] = dev["timeZoneId"]
                        _LOGGER.debug(
                            "%s: Locale %s timezone %s",
                            dev_name,
                            device["locale"],
                            device["timeZoneId"],
                        )
                        break

            if dnd is not None and "doNotDisturbDeviceStatusList" in dnd:
                for dev in dnd["doNotDisturbDeviceStatusList"]:
                    if dev["deviceSerialNumber"] == serial:
                        device["dnd"] = dev["enabled"]
                        _LOGGER.debug("%s: DND %s", dev_name, device["dnd"])
                        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"][
                            "switch"
                        ].setdefault(serial, {"dnd": True})
                        break

            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["auth_info"] = device[
                "auth_info"
            ] = auth_info
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"]["media_player"][
                serial
            ] = device

            if serial not in existing_serials:
                new_alexa_clients.append(dev_name)
            elif (
                serial in existing_serials
                and hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"][
                    "media_player"
                ].get(serial)
                and hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"][
                    "media_player"
                ]
                .get(serial)
                .enabled
            ):
                await (
                    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"][
                        "media_player"
                    ]
                    .get(serial)
                    .refresh(device, skip_api=True)
                )
        _LOGGER.debug(
            "%s: Existing: %s New: %s;"
            " Filtered out by not being in include: %s "
            "or in exclude: %s",
            hide_email(email),
            list(existing_entities),
            new_alexa_clients,
            include_filter,
            exclude_filter,
        )

        _LOGGER.debug("[BOOT] device processing in %.2fs", time.monotonic() - _t_proc)

        if new_alexa_clients:
            cleaned_config = config.copy()
            cleaned_config.pop(CONF_PASSWORD, None)
            # CONF_PASSWORD contains sensitive info which is no longer needed
            # Load multiple platforms in parallel using async_forward_entry_setups
            _LOGGER.debug("Loading platforms: %s", ", ".join(ALEXA_COMPONENTS))
            try:
                _t = time.monotonic()
                await hass.config_entries.async_forward_entry_setups(
                    config_entry, ALEXA_COMPONENTS
                )
                _LOGGER.debug("[BOOT] platform loading in %.2fs", time.monotonic() - _t)
                if metrics:
                    metrics.record_boot_stage(f"platforms_loaded_{hide_email(email)}")
            except (TimeoutError, TimeoutException) as ex:
                _LOGGER.error(f"Error while loading platforms: {ex}")
                raise ConfigEntryNotReady(
                    f"Timeout while loading platforms: {ex}"
                ) from ex

        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["new_devices"] = False
        # prune stale devices
        device_registry = dr.async_get(hass)
        entity_backed_ids = _entity_backed_device_identifiers(
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]
        )
        for device_entry in dr.async_entries_for_config_entry(
            device_registry, config_entry.entry_id
        ):
            for _, identifier in device_entry.identifiers:
                if (
                    identifier
                    in hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"][
                        "media_player"
                    ]
                    or identifier
                    in map(
                        lambda x: slugify(f"{x}_{email}"),
                        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"][
                            "media_player"
                        ].keys(),
                    )
                    or identifier in entity_backed_ids
                ):
                    break
            else:
                device_registry.async_remove_device(device_entry.id)
                _LOGGER.debug(
                    "%s: Removing stale device %s",
                    hide_email(email),
                    device_entry.name,
                )

        await login_obj.save_cookiefile()
        if login_obj.access_token:
            hass.config_entries.async_update_entry(
                config_entry,
                data={
                    **config_entry.data,
                    CONF_OAUTH: {
                        "access_token": login_obj.access_token,
                        "refresh_token": login_obj.refresh_token,
                        "expires_in": login_obj.expires_in,
                        "mac_dms": login_obj.mac_dms,
                        "code_verifier": login_obj.code_verifier,
                        "authorization_code": login_obj.authorization_code,
                    },
                },
            )

        if first_run or not _push_healthy(account):
            if _network_allowed(login_obj):
                trigger = account.get("last_called_probe_trigger")
                if callable(trigger):
                    trigger("POLL_REFRESH", None)
                else:
                    # fallback if probe not initialized for some reason
                    hass.async_create_background_task(
                        _async_update_last_called_global(hass, login_obj, email),
                        f"{DOMAIN}_last_called_poll_{hide_email(email)}",
                    )
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["first_run"] = False

        return entity_state

    # ---------------------------------------------------------------------
    # Near-real-time last_called probing (customer history), single worker
    # Initialize ONCE per account (setup_alexa), not inside http2_handler.
    # ---------------------------------------------------------------------

    async def http2_connect() -> HTTP2EchoClient:
        """Open HTTP2 Push connection.

        This will only attempt one login before failing.
        """
        http2: HTTP2EchoClient | None = None
        email = login_obj.email
        try:
            if login_obj.session.closed:
                _LOGGER.debug(
                    "%s: HTTP2 creation aborted. Session is closed.",
                    hide_email(email),
                )
                return
            http2 = HTTP2EchoClient(
                login_obj,
                msg_callback=http2_handler,
                open_callback=http2_open_handler,
                close_callback=http2_close_handler,
                error_callback=http2_error_handler,
                loop=hass.loop,
            )
            _LOGGER.debug("%s: Starting HTTP2: %s", hide_email(email), http2)
            await http2.async_run()
        except AlexapyLoginError as exception_:
            _LOGGER.debug(
                "%s: Login Error detected from http2: %s",
                hide_email(email),
                exception_,
            )
            hass.bus.async_fire(
                "alexa_media_relogin_required",
                event_data={"email": hide_email(email), "url": login_obj.url},
            )
            return
        except BaseException as exception_:  # pylint: disable=broad-except
            _LOGGER.debug(
                "%s: HTTP2 creation failed: %s", hide_email(email), exception_
            )
            return
        _LOGGER.debug("%s: HTTP2 created: %s", hide_email(email), http2)
        return http2

    @callback
    async def http2_handler(message_obj):
        # pylint: disable=too-many-branches,too-many-statements
        """Handle http2 push messages.

        This allows push notifications from Alexa to update last_called and media state.
        """

        coordinator = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get("coordinator")
        account = hass.data[DATA_ALEXAMEDIA]["accounts"][email]

        def _now_ms() -> int:
            return int(time.time() * 1000)

        def simulate_activity(
            device_serial: str,
            customer_id: str | None,
            trigger_command: str,
            trigger_ts_ms: int | None,
        ) -> None:
            _queue_last_called_activity(
                account,
                device_serial=device_serial,
                customer_id=customer_id,
                activity_ts=trigger_ts_ms,
                command=trigger_command,
            )
            account["last_called_probe_trigger_serial"] = device_serial
            trigger = account.get("last_called_probe_trigger")
            if callable(trigger):
                trigger(trigger_command, trigger_ts_ms)

        def _handle_volume_change_activity(
            serial: str,
            json_payload: dict,
            trigger_ts_ms: int | None,
        ) -> None:
            """Mirror alexa-remote.ts PUSH_VOLUME_CHANGE -> simulateActivity() conditions."""
            last_volumes: dict = account["last_volumes"]
            last_equalizer: dict = account["last_equalizer"]

            vol = json_payload.get("volumeSetting")
            muted = json_payload.get("isMuted")

            eq_prev = last_equalizer.get(serial)

            should_simulate = (
                eq_prev is not None
                and abs(_now_ms() - int(eq_prev.get("updated", 0)))
                < LAST_CALLED_COALESCE_WINDOW_MS
            )

            if should_simulate:
                _LOGGER.debug(
                    "[_handle_volume_change_activity] Simulating activity",
                )
                simulate_activity(
                    serial,
                    json_payload.get("destinationUserId"),
                    "PUSH_VOLUME_CHANGE",
                    trigger_ts_ms,
                )
            else:
                _LOGGER.debug(
                    "[_handle_volume_change_activity] Not simulating activity",
                )

            last_volumes[serial] = {
                "volumeSetting": vol,
                "isMuted": muted,
                "updated": _now_ms(),
            }

        def _handle_equalizer_change_activity(
            serial: str,
            json_payload: dict,
            trigger_ts_ms: int | None,
        ) -> None:
            """Mirror alexa-remote.ts PUSH_EQUALIZER_STATE_CHANGE -> simulateActivity() conditions."""
            last_volumes: dict = account["last_volumes"]
            last_equalizer: dict = account["last_equalizer"]

            bass = json_payload.get("bass")
            treble = json_payload.get("treble")
            midrange = json_payload.get("midrange")

            prev = last_equalizer.get(serial)
            vol_prev = last_volumes.get(serial)

            should_simulate = (
                prev is not None
                and prev.get("bass") == bass
                and prev.get("treble") == treble
                and prev.get("midrange") == midrange
            ) or (
                vol_prev is not None
                and abs(_now_ms() - int(vol_prev.get("updated", 0)))
                < LAST_CALLED_COALESCE_WINDOW_MS
            )

            if should_simulate:
                simulate_activity(
                    serial,
                    json_payload.get("destinationUserId"),
                    "PUSH_EQUALIZER_STATE_CHANGE",
                    trigger_ts_ms,
                )

            last_equalizer[serial] = {
                "bass": bass,
                "treble": treble,
                "midrange": midrange,
                "updated": _now_ms(),
            }

        # ---------------------------------------------------------------------
        # Main http2push parsing / dispatch
        # ---------------------------------------------------------------------
        updates = (
            message_obj.get("directive", {})
            .get("payload", {})
            .get("renderingUpdates", [])
        )
        existing_serials = set(_existing_serials(hass, login_obj))
        existing_serials |= _entity_backed_serials(account)
        for item in updates:
            try:
                resource = loads(item.get("resourceMetadata", ""))
            except JSONDecodeError:
                continue

            command = (
                resource["command"]
                if isinstance(resource, dict) and "command" in resource
                else None
            )
            try:
                json_payload = (
                    loads(resource["payload"])
                    if isinstance(resource, dict) and "payload" in resource
                    else None
                )
            except (JSONDecodeError, TypeError):
                _LOGGER.debug(
                    "%s: Skipping malformed push payload for command %s",
                    hide_email(email),
                    command,
                )
                continue
            seen_commands = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                "http2_commands"
            ]

            if command and json_payload:
                _LOGGER.debug(
                    "%s: Received http2push command: %s : %s",
                    hide_email(email),
                    command,
                    hide_serial(json_payload),
                )

                account["last_push_activity"] = time.time()
                serial = None
                command_time = time.time()
                if command not in seen_commands:
                    _LOGGER.debug(
                        "Adding %s to seen_commands: %s", command, seen_commands
                    )
                seen_commands[command] = command_time

                if (
                    "dopplerId" in json_payload
                    and "deviceSerialNumber" in json_payload["dopplerId"]
                ):
                    serial = json_payload["dopplerId"]["deviceSerialNumber"]
                elif (
                    "key" in json_payload
                    and "entryId" in json_payload["key"]
                    and json_payload["key"]["entryId"].find("#") != -1
                ):
                    serial = (json_payload["key"]["entryId"]).split("#")[2]
                    json_payload["key"]["serialNumber"] = serial
                else:
                    serial = None

                if command in (
                    "PUSH_AUDIO_PLAYER_STATE",
                    "PUSH_MEDIA_CHANGE",
                    "PUSH_MEDIA_PROGRESS_CHANGE",
                    "NotifyMediaSessionsUpdated",
                    "NotifyNowPlayingUpdated",
                ):
                    if serial and serial in existing_serials:
                        _LOGGER.debug(
                            "Updating media_player: %s", hide_serial(json_payload)
                        )
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"player_state": json_payload},
                        )
                    elif command == "NotifyNowPlayingUpdated":
                        _LOGGER.debug("Send NowPlaying: %s", hide_serial(json_payload))
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"now_playing": json_payload},
                        )

                elif command == "PUSH_VOLUME_CHANGE":
                    try:
                        ts = resource.get("timeStamp")
                        ts_ms = int(ts) if ts else None

                        if serial and isinstance(json_payload, dict):
                            _handle_volume_change_activity(serial, json_payload, ts_ms)

                    except Exception:
                        _LOGGER.exception(
                            "%s: http2_handler failed processing %s",
                            hide_email(email),
                            command,
                        )

                    if serial and serial in existing_serials:
                        _LOGGER.debug(
                            "Updating media_player volume: %s",
                            hide_serial(json_payload),
                        )
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"player_state": json_payload},
                        )

                elif command == "PUSH_DOPPLER_CONNECTION_CHANGE":
                    # Player availability update
                    if serial and serial in existing_serials:
                        _LOGGER.debug(
                            "Updating media_player availability %s",
                            hide_serial(json_payload),
                        )
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"player_state": json_payload},
                        )

                elif command == "PUSH_EQUALIZER_STATE_CHANGE":
                    # Player equalizer update
                    try:
                        ts = resource.get("timeStamp")
                        ts_ms = int(ts) if ts else None

                        if serial and isinstance(json_payload, dict):
                            _handle_equalizer_change_activity(
                                serial, json_payload, ts_ms
                            )

                    except Exception:
                        _LOGGER.exception(
                            "%s: http2_handler failed processing %s",
                            hide_email(email),
                            command,
                        )

                    if serial and serial in existing_serials:
                        _LOGGER.debug(
                            "Updating media_player equalizer state %s",
                            hide_serial(json_payload),
                        )
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"player_state": json_payload},
                        )

                elif command == "PUSH_BLUETOOTH_STATE_CHANGE":
                    # Player bluetooth update
                    bt_event = (
                        json_payload.get("bluetoothEvent")
                        if isinstance(json_payload, dict)
                        else None
                    )
                    _LOGGER.debug("bt_event: %s", bt_event)
                    bt_success = (
                        json_payload.get("bluetoothEventSuccess")
                        if isinstance(json_payload, dict)
                        else None
                    )
                    _LOGGER.debug("bt_success: %s", bt_success)
                    if (
                        serial
                        and serial in existing_serials
                        and bt_success
                        and bt_event
                        and bt_event
                        in {
                            "DEVICE_CONNECTED",
                            "DEVICE_DISCONNECTED",
                            "STREAMING_STATE_CHANGED",
                        }
                    ):
                        _LOGGER.debug(
                            "Updating media_player bluetooth %s",
                            hide_serial(json_payload),
                        )
                        bluetooth_state = await setup_bluetooth.update_bluetooth_state(
                            login_obj, ctx, serial
                        )
                        _LOGGER.debug(
                            "bluetooth_state %s", hide_serial(bluetooth_state)
                        )
                        if bluetooth_state:
                            async_dispatcher_send(
                                hass,
                                f"{DOMAIN}_{hide_email(email)}"[0:32],
                                {"bluetooth_change": bluetooth_state},
                            )

                elif command == "PUSH_MEDIA_QUEUE_CHANGE":
                    # Player availability update
                    if serial and serial in existing_serials:
                        _LOGGER.debug(
                            "Updating media_player queue %s",
                            hide_serial(json_payload),
                        )
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"queue_state": json_payload},
                        )

                elif command == "PUSH_NOTIFICATION_CHANGE":
                    # Notification/alarm state changed on this device.
                    # Queue a refresh with backoff to ride out alexa-side cooldowns.
                    setup_notifications.schedule_notifications_refresh(
                        ctx,
                        device_serial=serial,
                        reason="PUSH_NOTIFICATION_CHANGE",
                    )

                    if serial and serial in existing_serials:
                        _LOGGER.debug(
                            "Updating mediaplayer notifications: %s",
                            hide_serial(json_payload),
                        )
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"notification_update": json_payload},
                        )

                elif command in [
                    "PUSH_DELETE_DOPPLER_ACTIVITIES",  # Delete Alexa history,
                ]:
                    pass

                elif (
                    command
                    in [
                        "PUSH_TODO_CHANGE",  # Update To-Do List
                        "PUSH_LIST_CHANGE",  # Clear a shopping list https://github.com/alandtse/alexa_media_player/issues/1190
                        "PUSH_LIST_ITEM_CHANGE",  # Update shopping list
                    ]
                ):
                    # To-do
                    _LOGGER.debug("%s currently not supported", command)
                    pass

                elif command in [
                    "PUSH_CONTENT_FOCUS_CHANGE",  # Likely prime related refocus
                    "PUSH_DEVICE_SETUP_STATE_CHANGE",  # Likely device changes mid setup
                ]:
                    _LOGGER.debug("%s currently not supported", command)
                    pass

                elif (
                    command
                    in [
                        "PUSH_MEDIA_PREFERENCE_CHANGE",  # Disliking or liking songs, https://github.com/alandtse/alexa_media_player/issues/1599
                    ]
                ):
                    _LOGGER.debug("%s currently not supported", command)
                    pass

                elif command in [
                    "MATTER_SETUP_NOTIFICATION",  # New command observed 2026-02-20
                ]:
                    _LOGGER.debug("%s: New command; currently not supported", command)
                    pass

                else:
                    _LOGGER.debug(
                        "Unhandled command: %s with data %s. Please report at %s",
                        command,
                        hide_serial(json_payload),
                        ISSUE_URL,
                    )

                # Preserve existing http2 activity tracking + new-device discovery
                if serial in existing_serials:
                    history = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                        "http2_activity"
                    ]["serials"].get(serial)
                    if history is None or (
                        history and command_time - history[len(history) - 1][1] > 2
                    ):
                        history = [(command, command_time)]
                    else:
                        history.append([command, command_time])

                    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_activity"][
                        "serials"
                    ][serial] = history

                    events = []
                    for old_command, old_command_time in history:
                        if (
                            old_command
                            in {"PUSH_VOLUME_CHANGE", "PUSH_EQUALIZER_STATE_CHANGE"}
                            and command_time - old_command_time < 0.25
                        ):
                            events.append(
                                (old_command, round(command_time - old_command_time, 2))
                            )
                        elif old_command in {"PUSH_AUDIO_PLAYER_STATE"}:
                            events = []

                    if len(events) >= 4:
                        _LOGGER.debug(
                            "%s: Detected potential DND http2push change with %s events %s",
                            hide_serial(serial),
                            len(events),
                            events,
                        )
                        await setup_dnd.update_dnd_state(login_obj, ctx)

                if (
                    serial
                    and serial not in existing_serials
                    and serial
                    not in hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                        "excluded"
                    ].keys()
                ):
                    _LOGGER.debug("Discovered new media_player %s", hide_serial(serial))
                    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["new_devices"] = True
                    if coordinator:
                        await coordinator.async_request_refresh()

    @callback
    async def http2_open_handler():
        """Handle http2 open."""

        email: str = login_obj.email
        _LOGGER.debug("%s: HTTP2push successfully connected", hide_email(email))
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = (
            0  # set errors to 0
        )
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_lastattempt"] = time.time()

    @callback
    async def http2_close_handler():
        """Handle http2 close.

        This should attempt to reconnect up to 5 times
        """
        email: str = login_obj.email
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"] = None
        if login_obj.close_requested:
            _LOGGER.debug(
                "%s: Close requested; will not reconnect http2", hide_email(email)
            )
            return
        if not login_obj.status.get("login_successful"):
            _LOGGER.debug(
                "%s: Login error; will not reconnect http2", hide_email(email)
            )
            return
        errors: int = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"]
        delay: int = 5 * 2**errors
        last_attempt = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
            "http2_lastattempt"
        ]
        now = time.time()
        if (now - last_attempt) < delay:
            return
        http2_client = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"]
        http2_enabled = bool(http2_client)
        while errors < 5 and not (http2_enabled):
            _LOGGER.debug(
                "%s: HTTP2 push closed; reconnect #%i in %is",
                hide_email(email),
                errors,
                delay,
            )
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_lastattempt"] = (
                time.time()
            )
            http2_client = await http2_connect()
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"] = http2_client
            http2_enabled = bool(http2_client)
            if http2_enabled:
                break
            errors = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = (
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] + 1
            )
            delay = 5 * 2**errors
            errors = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"]
            await asyncio.sleep(delay)
        if not http2_enabled:
            _LOGGER.debug(
                "%s: HTTP2Push connection closed; retries exceeded; polling",
                hide_email(email),
            )
        coordinator = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get("coordinator")
        if coordinator:
            coordinator.update_interval = timedelta(
                seconds=scan_interval * 10 if http2_enabled else scan_interval
            )
            _LOGGER.debug(
                "HTTP2push: %s, Polling interval: %s",
                http2_enabled,
                coordinator.update_interval,
            )
            await coordinator.async_request_refresh()

    @callback
    async def http2_error_handler(message):
        """Handle http2push error.

        This currently logs the error.  In the future, this should invalidate
        the http2push and determine if a reconnect should be done.
        """
        email: str = login_obj.email
        errors = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"]
        _LOGGER.debug(
            "%s: Received http2push error #%i %s: type %s",
            hide_email(email),
            errors,
            message,
            type(message),
        )
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"] = None
        if not login_obj.close_requested and (
            login_obj.session.closed or isinstance(message, AlexapyLoginError)
        ):
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = 5
            _LOGGER.debug("%s: Login error detected.", hide_email(email))
            hass.bus.async_fire(
                "alexa_media_relogin_required",
                event_data={"email": hide_email(email), "url": login_obj.url},
            )
            return
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = errors + 1

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
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["login_obj"] = login_obj

    # Initialize the per-account probe worker exactly once here (not per push message).
    setup_last_called._init_last_called_probe_worker(
        ctx, hass.data[DATA_ALEXAMEDIA]["accounts"][email]
    )

    _t = time.monotonic()
    http2_enabled = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
        "http2"
    ] = await http2_connect()
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
            update_method=async_update_data,
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
    _LOGGER.debug("Logging in: %s %s", obfuscate(account), in_progress_instances(hass))
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
