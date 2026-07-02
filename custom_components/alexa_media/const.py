"""
Support to interface with Alexa Devices.

SPDX-License-Identifier: Apache-2.0

For more details about this platform, please refer to the documentation at
https://community.home-assistant.io/t/echo-devices-alexa-as-media-player-testers-needed/58639
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.const import (
    CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
    CONCENTRATION_PARTS_PER_MILLION,
    PERCENTAGE,
)

PROJECT_URL = "https://github.com/foXaCe/alexa_media_player/"
ISSUE_URL = f"{PROJECT_URL}issues"
NOTIFY_URL = f"{PROJECT_URL}wiki/Configuration%3A-Notification-Component#use-the-notifyalexa_media-service"

DOMAIN = "alexa_media"
DATA_ALEXAMEDIA = "alexa_media"

PLAY_SCAN_INTERVAL = 20
SCAN_INTERVAL = timedelta(seconds=60)
MIN_TIME_BETWEEN_SCANS = SCAN_INTERVAL
MIN_TIME_BETWEEN_FORCED_SCANS = timedelta(seconds=1)

ALEXA_COMPONENTS = [
    "media_player",
]
DEPENDENT_ALEXA_COMPONENTS = [
    "notify",
    "switch",
    "sensor",
    "alarm_control_panel",
    "light",
    "binary_sensor",
]

CONF_ACCOUNTS = "accounts"
CONF_DEBUG = "debug"
CONF_HASS_URL = "hass_url"
CONF_INCLUDE_DEVICES = "include_devices"
CONF_EXCLUDE_DEVICES = "exclude_devices"
CONF_QUEUE_DELAY = "queue_delay"
CONF_PUBLIC_URL = "public_url"
CONF_EXTENDED_ENTITY_DISCOVERY = "extended_entity_discovery"
CONF_SECURITYCODE = "securitycode"
CONF_OTPSECRET = "otp_secret"
CONF_PROXY_WARNING = "proxy_warning"
CONF_SCAN_INTERVAL = (
    "scan_interval"  # local definition; HA's CONF_SCAN_INTERVAL is deprecated
)
CONF_TOTP_REGISTER = "registered"
CONF_OAUTH = "oauth"

# Bus events fired when the Amazon session needs / regains authentication.
EVENT_RELOGIN_REQUIRED = "alexa_media_relogin_required"
EVENT_RELOGIN_SUCCESS = "alexa_media_relogin_success"

EXCEPTION_TEMPLATE = "An exception of type {0} occurred. Arguments:\n{1!r}"

DEFAULT_DEBUG = False
DEFAULT_EXTENDED_ENTITY_DISCOVERY = False
DEFAULT_HASS_URL = "http://homeassistant.local:8123"
DEFAULT_PUBLIC_URL = ""
DEFAULT_QUEUE_DELAY = 1.5
DEFAULT_SCAN_INTERVAL = 60

EPOCH_MS_THRESHOLD = 10_000_000_000

# Service name constants used by services.py SERVICE_DEFS
SERVICE_UPDATE_LAST_CALLED = "update_last_called"
SERVICE_RESTORE_VOLUME = "restore_volume"
SERVICE_GET_HISTORY_RECORDS = "get_history_records"
SERVICE_FORCE_LOGOUT = "force_logout"
SERVICE_ENABLE_NETWORK_DISCOVERY = "enable_network_discovery"

# Backoff durations for the last-called probe worker
LAST_CALLED_429_BACKOFF_INITIAL_S = 30.0
LAST_CALLED_429_BACKOFF_MAX_S = 15 * 60.0
LAST_CALLED_CONN_BACKOFF_S = 10.0
LAST_CALLED_LOGIN_BACKOFF_S = 30.0

# Tuning constants for the per-account last-called probe worker
LAST_CALLED_DEBOUNCE_S = 3.5  # coalesce bursty pushes, but stay snappy
LAST_CALLED_RETRY_DELAY_S = 4.0  # wider retry cadence for delayed routine history
LAST_CALLED_RETRY_LIMIT = 2  # total attempts = 1 + retries (3 attempts)
LAST_CALLED_STALE_FUDGE_MS = 5_000  # allow some clock/ordering jitter
LAST_CALLED_SUCCESS_PACE_S = 4.0  # post-success pacing to avoid hammering
LAST_CALLED_LOOKBACK_MS = 60_000
LAST_CALLED_ITEMS = 10
LAST_CALLED_COALESCE_WINDOW_MS = 2000

# Tuning constants for notification retries
NOTIFICATION_COOLDOWN = 60
NOTIFY_REFRESH_BACKOFF = 15.0
NOTIFY_REFRESH_MAX_RETRIES = 3

# Reauth tolerance. A relogin requested within this window of the last successful
# login can be a genuine login loop OR just transient API/auth flakiness right
# after startup (e.g. a single GraphQL "Unauthenticated" response). Allow a few
# automatic relogin attempts to absorb the latter before demanding a manual login.
REAUTH_RAPID_RELOGIN_WINDOW_S = 60
REAUTH_MAX_AUTO_ATTEMPTS = 3

# Number of consecutive login errors in the coordinator update tolerated as
# transient (e.g. a flaky get_devices / GraphQL "Unauthenticated" right after a
# successful login at boot) before escalating to a manual reauth. Each tolerated
# error is surfaced as UpdateFailed so the first refresh re-raises
# ConfigEntryNotReady and Home Assistant re-bootstraps the entry on its own — a
# single reboot self-heals instead of needing several manual reboots.
LOGIN_ERROR_RETRY_TOLERANCE = 5

# Delay before the coordinator retries after an Amazon 429 (seconds)
COORDINATOR_429_RETRY_AFTER_S = 60.0

# push-health magic numbers
HTTP2_ERROR_THRESHOLD = 5
LAST_PUSH_INACTIVITY_SECONDS = 600.0
LAST_PING_MAX_AGE_SECONDS = 900.0

RECURRING_PATTERN = {
    None: "Never Repeat",
    "P1D": "Every day",
    "P1M": "Every month",
    "XXXX-WE": "Weekends",
    "XXXX-WD": "Weekdays",
    "XXXX-WXX-1": "Every Monday",
    "XXXX-WXX-2": "Every Tuesday",
    "XXXX-WXX-3": "Every Wednesday",
    "XXXX-WXX-4": "Every Thursday",
    "XXXX-WXX-5": "Every Friday",
    "XXXX-WXX-6": "Every Saturday",
    "XXXX-WXX-7": "Every Sunday",
}

RECURRING_DAY = {
    "MO": 1,
    "TU": 2,
    "WE": 3,
    "TH": 4,
    "FR": 5,
    "SA": 6,
    "SU": 7,
}
RECURRING_PATTERN_ISO_SET = {
    None: {},
    "P1D": {1, 2, 3, 4, 5, 6, 7},
    "XXXX-WE": {6, 7},
    "XXXX-WD": {1, 2, 3, 4, 5},
    "XXXX-WXX-1": {1},
    "XXXX-WXX-2": {2},
    "XXXX-WXX-3": {3},
    "XXXX-WXX-4": {4},
    "XXXX-WXX-5": {5},
    "XXXX-WXX-6": {6},
    "XXXX-WXX-7": {7},
}

ATTR_EMAIL = "email"
ATTR_ENTITY_ID = "entity_id"
ATTR_NUM_ENTRIES = "entries"
COMMON_BUCKET_COUNTS = (
    "accounts",
    "devices",
    "media_players",
    "players",
    "notifications",
    "entities",
)
COMMON_DIAGNOSTIC_BUCKETS = (
    "account",
    "accounts",
    "login",
    "logins",
    "session",
    "sessions",
)
COMMON_DIAGNOSTIC_NAMES = (
    "name",
    "deviceName",
    "accountName",
    "friendlyName",
    "title",
)
DEVICE_PLAYER_BUCKETS = ("devices", "media_players", "players")
TO_REDACT: set[str] = {
    "email",
    "password",
    "access_token",
    "refresh_token",
    "token",
    "csrf",
    "cookie",
    "cookies",
    "session",
    "sessionid",
    "macDms",
    "mac_dms",
    "otp_secret",
    "authorization_code",
    "securitycode",
    "code_verifier",
    "adp_token",
    "device_private_key",
    "customerId",
}
STREAMING_ERROR_MESSAGE = (
    "Sorry, direct music streaming isn't supported. "
    "This limitation is set by Amazon, and not by Alexa-Media-Player, Music-Assistant, nor Home-Assistant."
)
PUBLIC_URL_ERROR_MESSAGE = (
    "To send TTS, please set the public URL in integration configuration."
)
STARTUP_MESSAGE = """
{name} Version Info
{DOMAIN}: v{version}
alexapy API: v{alexapy_version}
If you have any issues with this custom component, you need to open an issue here: {ISSUE_URL}
"""

AUTH_CALLBACK_PATH = "/auth/alexamedia/callback"
AUTH_CALLBACK_NAME = "auth:alexamedia:callback"
AUTH_PROXY_PATH = "/auth/alexamedia/proxy"
AUTH_PROXY_NAME = "auth:alexamedia:proxy"

ALEXA_UNIT_CONVERSION = {
    "Alexa.Unit.Percent": PERCENTAGE,
    "Alexa.Unit.PartsPerMillion": CONCENTRATION_PARTS_PER_MILLION,
    "Alexa.Unit.Density.MicroGramsPerCubicMeter": CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
}

ALEXA_ICON_CONVERSION = {
    "Alexa.AirQuality.CarbonMonoxide": "mdi:molecule-co",
    "Alexa.AirQuality.Humidity": "mdi:water-percent",
    "Alexa.AirQuality.IndoorAirQuality": "mdi:numeric",
    "Alexa.AirQuality.ParticulateMatter": "mdi:blur",
    "Alexa.AirQuality.VolatileOrganicCompounds": "mdi:air-filter",
}
ALEXA_ICON_DEFAULT = "mdi:molecule"

# Device class mapping for air quality sensors
# Maps Alexa sensor types to Home Assistant SensorDeviceClass
ALEXA_AIR_QUALITY_DEVICE_CLASS = {
    "Alexa.AirQuality.ParticulateMatter": "pm25",
    "Alexa.AirQuality.CarbonMonoxide": "carbon_monoxide",
    "Alexa.AirQuality.IndoorAirQuality": "aqi",
    "Alexa.AirQuality.VolatileOrganicCompounds": "aqi",
    "Alexa.AirQuality.Humidity": "humidity",
}

UPLOAD_PATH = "www/alexa_tts"

# Note: Some of these are likely wrong
