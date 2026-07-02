# Architecture

High-level overview of how the `alexa_media` integration is structured.

## Data flow

```
Amazon Alexa cloud
        │  (unofficial API, via alexapy)
        ▼
  AlexaLogin / AlexaAPI
        │
        ├──► setup/coordinator_data.async_update_data ──► AlexaMediaCoordinator ──► entities
        │         (polling: devices, bluetooth, DND, notifications, entity state)
        │
        └──► setup/push.http2_connect (HTTP/2 push) ──► push handlers ──► coordinator refresh
                                                          (near-real-time updates)
```

Per `ConfigEntry`, `setup_alexa` builds a `SetupContext` (`setup/context.py`) that
carries the shared state (`hass`, the config entry, the account email, the
`login_obj`, the polling interval and the DND throttling state). Every helper in
the `setup/` package receives that context explicitly instead of relying on
closures, which is what makes them testable in isolation.

## Key modules

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | Config-entry lifecycle only: `async_setup` / `async_setup_entry` (login bootstrap, cookie probe, listeners registered via `entry.async_on_unload`) / `async_unload_entry` / `async_remove_entry`, plus the thin `setup_alexa` orchestrator that wires the `setup/` helpers together |
| `setup/context.py` | `SetupContext` — the typed per-invocation state shared across the `setup/` helpers |
| `setup/coordinator_data.py` | `async_update_data` — the `DataUpdateCoordinator` update method, decomposed into named steps (`_build_fetch_plan` named-coroutine gather, `_run_network_discovery`, `_apply_device_updates`, `_prune_stale_devices`, `_persist_oauth`, …); raises `UpdateFailed` on transient cloud errors and `UpdateFailed(retry_after=…)` on Amazon 429 |
| `setup/push.py` | `http2_connect` + the HTTP/2 push handlers; `http2_handler` routes each command through the `_PUSH_HANDLERS` dispatch table (`_handle_*` coroutines sharing a `_PushEvent`) |
| `setup/last_called.py` | "last called" device tracking: voice-activity ↔ push-event correlation from customer-history records |
| `setup/notifications.py` | Notification snapshot processing + debounced refresh worker |
| `setup/dnd.py` | Do-Not-Disturb state sync with cooldown throttling |
| `setup/bluetooth.py` | Bluetooth state sync on push events |
| `coordinator.py` | `AlexaMediaCoordinator` — `DataUpdateCoordinator` subclass (debouncer, HTTP/2-aware interval) |
| `runtime_data.py` | `AlexaRuntimeData` — typed per-`ConfigEntry` state, plus the `AlexaConfigEntry = ConfigEntry[AlexaRuntimeData]` alias |
| `config_flow.py` | UI configuration, reauth and options flow |
| `alexa_entity.py` | Parsing of raw Alexa entity payloads into typed data |
| `const.py` | Domain, defaults, tuning constants, bus event names, `TO_REDACT` |
| `model_ids.py` | Static Amazon model-ID → human-readable name table (pure data) |
| `helpers.py` | Shared utilities (`redact_sensitive`, login-error decorator, serial helpers, …) |
| `services.py` | Custom services registration |

## Platforms

Entities are exposed through the standard Home Assistant platform files —
`media_player.py`, `sensor.py`, `switch.py`, `alarm_control_panel.py`,
`light.py`, `notify.py`, `binary_sensor.py`. Each platform module only declares
`async_setup_entry` and instantiates entities; device discovery and state live in
the coordinator data.

### Entity conventions

- **`has_entity_name`** — entity platforms set `_attr_has_entity_name = True`,
  return `name=None`, and expose `device_info` whose `name` is the human label
  (the device/contact/light/guard name). The composed `friendly_name` is
  therefore unchanged versus the old `name` property (name-neutral).
- **`EntityDescription`** — entities with static descriptive metadata use a
  (frozen) `*EntityDescription`. The control switches share
  `AlexaSwitchEntityDescription` (translation_key + entity_category + on/off
  icons); sensors carry their `device_class`/`state_class`/unit via `_attr_*`.
- **`unique_id` stability is non-negotiable** — `unique_id` derives from the
  Amazon serial (+ a fixed suffix), never from an `EntityDescription.key`.
  Changing it silently renames established entities, so any entity refactor must
  keep it byte-identical (guarded by tests).

## Extending the integration

- **New `setup/` concern** — add a module under `setup/` whose functions take the
  `SetupContext` first (decorated helpers keep `login_obj` first because
  `@_catch_login_errors` inspects `args[0]`), then call it from `setup_alexa`.
- **New platform** — add `<platform>.py` with an `async_setup_entry`, register it
  in `ALEXA_COMPONENTS`/`PLATFORMS`, and read state from `coordinator.data`.

## Security / logging

Credentials must never reach the logs. `helpers.redact_sensitive()` wraps
`alexapy.obfuscate()` with `async_redact_data(..., TO_REDACT)` so OAuth secrets
(`access_token`, `refresh_token`, `authorization_code`, `code_verifier`,
`mac_dms` → `adp_token` / `device_private_key`) are fully redacted even at
`DEBUG`. Route any log of account/config data through it.

## External dependencies

- **[alexapy](https://pypi.org/project/alexapy/)** — unofficial Amazon Alexa API client
- **authcaptureproxy** — handles the login capture flow
- Pinned in `custom_components/alexa_media/manifest.json` (`requirements`)

## Translations

UI strings live in `custom_components/alexa_media/strings.json`; localized copies are in
`custom_components/alexa_media/translations/<lang>.json`.

> ⚠️ This integration relies on an **unofficial** API. Amazon may change or revoke access at any time.
