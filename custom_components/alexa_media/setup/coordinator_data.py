"""Coordinator data fetch for Alexa Media Player.

``async_update_data`` is the ``DataUpdateCoordinator`` update method, extracted
from ``setup_alexa``. It pings the Alexa cloud for devices, bluetooth, DND,
notifications and entity state, and returns the structured payload the entities
read. Bound to a :class:`SetupContext` via ``functools.partial`` in ``__init__``.
"""

from __future__ import annotations

import asyncio
from json import JSONDecodeError
import logging
import time
from typing import TYPE_CHECKING

from alexapy import AlexaAPI, AlexapyConnectionError, AlexapyLoginError
from alexapy.errors import AlexapyTooManyRequestsError
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import slugify

from ..alexa_entity import AlexaEntityData, get_entity_data, parse_alexa_entities
from ..const import (
    ALEXA_COMPONENTS,
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_EXTENDED_ENTITY_DISCOVERY,
    CONF_INCLUDE_DEVICES,
    CONF_OAUTH,
    DATA_ALEXAMEDIA,
    DOMAIN,
    HTTP2_ERROR_THRESHOLD,
    LAST_PING_MAX_AGE_SECONDS,
    LAST_PUSH_INACTIVITY_SECONDS,
)
from ..exceptions import TimeoutException
from ..helpers import (
    _entity_backed_device_identifiers,
    _entity_backed_serials,
    _existing_serials,
    _network_allowed,
    hide_email,
)
from . import notifications as setup_notifications
from .last_called import _async_update_last_called_global

if TYPE_CHECKING:
    from .context import SetupContext

_LOGGER = logging.getLogger(__name__)


async def async_update_data(ctx: SetupContext) -> AlexaEntityData | None:
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
    hass = ctx.hass
    config = ctx.config
    config_entry = ctx.config_entry
    metrics = ctx.metrics
    include = (
        cv.ensure_list_csv(config[CONF_INCLUDE_DEVICES])
        if config[CONF_INCLUDE_DEVICES]
        else ""
    )
    exclude = (
        cv.ensure_list_csv(config[CONF_EXCLUDE_DEVICES])
        if config[CONF_EXCLUDE_DEVICES]
        else ""
    )
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

    for binary_sensor in hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"].get(
        "binary_sensor", []
    ):
        if binary_sensor.enabled:
            entities_to_monitor.add(binary_sensor.alexa_entity_id)

    for guard in (
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"]
        .get("alarm_control_panel", {})
        .values()
    ):
        if guard.enabled:
            entities_to_monitor.add(guard.unique_id)

    for smart_switch in hass.data[DATA_ALEXAMEDIA]["accounts"][email]["entities"].get(
        "smart_switch", []
    ):
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

                    _LOGGER.info("%s: Network Discovery: Checking", hide_email(email))
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
                        await setup_notifications.process_notifications(login_obj, ctx)
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
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"][serial] = device
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
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"][serial] = device
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
            raise ConfigEntryNotReady(f"Timeout while loading platforms: {ex}") from ex

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
