# Architecture

High-level overview of how the `alexa_media` integration is structured.

## Data flow

```
Amazon Alexa cloud
        │  (unofficial API, via alexapy)
        ▼
  AlexaLogin / AlexaAPI ──► AlexaMediaCoordinator ──► entities (platforms)
        │                         │
   authcaptureproxy         runtime_data (per ConfigEntry)
```

## Key modules

| Module | Responsibility |
|--------|----------------|
| `__init__.py` | `async_setup_entry` / `async_unload_entry`, login bootstrap, background refresh tasks, platform forwarding |
| `coordinator.py` | `AlexaMediaCoordinator` — `DataUpdateCoordinator` driving polling and push (HTTP/2) updates |
| `runtime_data.py` | `AlexaRuntimeData` — typed per-`ConfigEntry` state (login, coordinator, devices, listeners) |
| `config_flow.py` | UI configuration, reauth and options flow |
| `alexa_entity.py` | Parsing of raw Alexa entity payloads into typed data |
| `const.py` | Domain, defaults, tuning constants |
| `helpers.py` | Shared utilities |
| `services.py` | Custom services registration |

## Platforms

Entities are exposed through the standard Home Assistant platform files, e.g.
`media_player.py`, `sensor.py`, `switch.py`, `alarm_control_panel.py`,
`light.py`, `notify.py`, `binary_sensor.py`.

## External dependencies

- **[alexapy](https://pypi.org/project/alexapy/)** — unofficial Amazon Alexa API client
- **authcaptureproxy** — handles the login capture flow
- Pinned in `custom_components/alexa_media/manifest.json` (`requirements`)

## Translations

UI strings live in `custom_components/alexa_media/strings.json`; localized copies are in
`custom_components/alexa_media/translations/<lang>.json`.

> ⚠️ This integration relies on an **unofficial** API. Amazon may change or revoke access at any time.
