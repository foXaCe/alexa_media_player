# Alexa Media Player

[![GitHub Release][releases-shield]][releases]
[![License][license-shield]](LICENSE)
[![hacs][hacsbadge]][hacs]
[![CI][ci-shield]][ci]
[![HACS validation][hacs-validate-shield]][hacs-validate]
[![hassfest][hassfest-shield]][hassfest]
[![Downloads][downloads-shield]][releases]

_Custom [Home Assistant](https://home-assistant.io) integration to control Amazon Alexa devices (Echo and friends) using the unofficial Alexa API._

> This is a community fork maintained by [@foXaCe](https://github.com/foXaCe), based on the
> original work of [@alandtse](https://github.com/alandtse) and [@keatontaylor](https://github.com/keatontaylor).

> ⚠️ This mimics the Alexa app and relies on an **unofficial** API. Amazon may cut off access at any time.

## Features

- Control Amazon Echo products as Home Assistant media players (play / pause / stop, next / previous, volume)
- Retrieve and display song title, artist, album name and album art
- Send notifications: text-to-speech, mobile push and announcements
- Turn Alexa Guard on/off (region support required)

## Requirements

- Home Assistant >= 2025.2
- Python >= 3.13
- An Amazon account with Alexa devices

## Installation

### HACS (custom repository)

1. In HACS, add `https://github.com/foXaCe/alexa_media_player` as a custom repository (type: **Integration**)
2. Search for **Alexa Media Player** and install it
3. Restart Home Assistant
4. Go to **Settings → Devices & Services → Add Integration → Alexa Media Player**

### Manual

1. Copy `custom_components/alexa_media/` into `<config>/custom_components/`
2. Restart Home Assistant
3. Add the integration from the UI

## Configuration

Add the integration from **Settings -> Devices & Services -> Add Integration -> Alexa Media Player**
and sign in with your Amazon credentials through the built-in login proxy. App-based
two-factor authentication (an OTP secret) can be completed automatically during the flow.

The detailed walkthrough lives on the
[Configuration wiki page](https://github.com/foXaCe/alexa_media_player/wiki/Configuration).

### Configuration parameters

Set during setup and changeable later from the integration's **Configure** (options) dialog:

| Parameter | Default | Description |
|-----------|---------|-------------|
| Email / Password | - | Amazon account credentials, entered through the login proxy (never stored in plain text). |
| Amazon login URL | `amazon.com` | Regional Amazon domain used for authentication (e.g. `amazon.co.uk`, `amazon.de`). |
| OTP secret | _empty_ | Optional built-in TOTP secret to auto-complete two-factor authentication. |
| Home Assistant URL (`hass_url`) | `http://homeassistant.local:8123` | URL the login proxy redirects back to. |
| Public URL (`public_url`) | _empty_ | External URL when authenticating from outside your network. |
| Include devices (`include_devices`) | _empty_ | Restrict the integration to a comma-separated list of device names. |
| Exclude devices (`exclude_devices`) | _empty_ | Comma-separated list of device names to ignore. |
| Scan interval (`scan_interval`) | `60` s | Polling interval for account and device data. |
| Queue delay (`queue_delay`) | `1.5` s | Debounce delay used to batch consecutive commands. |
| Extended entity discovery (`extended_entity_discovery`) | `false` | Expose Alexa-connected smart-home entities (lights, sensors...). |

## Actions (services)

| Action | Description |
|--------|-------------|
| `alexa_media.force_logout` | Force logout of an account and delete its cached session. |
| `alexa_media.restore_volume` | Restore a media player to its previous volume level. |
| `alexa_media.get_history_records` | Fetch the latest voice-history records for a device. |
| `alexa_media.update_last_called` | Force a refresh of the `last_called` device per account. |
| `alexa_media.enable_network_discovery` | Re-enable Alexa network discovery on the next poll. |

The full field reference is in
[`services.yaml`](custom_components/alexa_media/services.yaml) and the in-app
**Developer Tools -> Actions** UI.

## How data is updated

The integration is **cloud polling** (`iot_class: cloud_polling`). A shared coordinator
refreshes account and device state every *scan interval* seconds (default 60 s); where
Amazon supports it, an HTTP/2 push channel delivers near real-time updates and the
effective polling interval is relaxed automatically.

## Known limitations

- Relies on an **unofficial** API that mimics the Alexa app - Amazon may change or block it at any time.
- Amazon may periodically require re-authentication (a repair notification is raised; follow the re-auth prompt).
- Some features (e.g. Guard) depend on device type and region.
- Announcements and text-to-speech are subject to Amazon-side rate limits.

## Troubleshooting

1. Enable debug logging:
   ```yaml
   logger:
     logs:
       custom_components.alexa_media: debug
       alexapy: debug
   ```
2. Download diagnostics from the integration's **... -> Download diagnostics** (secrets are redacted).
3. Check the [FAQ](https://github.com/foXaCe/alexa_media_player/wiki/FAQ) and open an
   [issue](https://github.com/foXaCe/alexa_media_player/issues) with logs and diagnostics.

## Documentation

Full documentation, FAQ and automation examples live in the [wiki](https://github.com/foXaCe/alexa_media_player/wiki).

## Removal

1. Go to **Settings -> Devices & Services** and open **Alexa Media Player**.
2. Use the **...** menu on each entry and choose **Delete**.
3. (Manual installs only) remove `custom_components/alexa_media/` from your config folder.
4. Restart Home Assistant. The integration removes its services, notify targets and cached
   session files automatically when the entry is unloaded.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).
Translations are stored under `custom_components/alexa_media/translations/`.

## License

[Apache-2.0](LICENSE). By providing a contribution, you agree it is licensed under Apache-2.0
(required for Home Assistant contributions).

<!-- Badges -->
[releases-shield]: https://img.shields.io/github/release/foXaCe/alexa_media_player.svg?style=for-the-badge
[releases]: https://github.com/foXaCe/alexa_media_player/releases
[license-shield]: https://img.shields.io/github/license/foXaCe/alexa_media_player.svg?style=for-the-badge
[hacs]: https://github.com/hacs/integration
[hacsbadge]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=for-the-badge
[ci-shield]: https://img.shields.io/github/actions/workflow/status/foXaCe/alexa_media_player/ci.yml?branch=main&style=for-the-badge&label=CI
[ci]: https://github.com/foXaCe/alexa_media_player/actions/workflows/ci.yml
[hacs-validate-shield]: https://img.shields.io/github/actions/workflow/status/foXaCe/alexa_media_player/hacs.yml?branch=main&style=for-the-badge&label=HACS
[hacs-validate]: https://github.com/foXaCe/alexa_media_player/actions/workflows/hacs.yml
[hassfest-shield]: https://img.shields.io/github/actions/workflow/status/foXaCe/alexa_media_player/hassfest.yml?branch=main&style=for-the-badge&label=hassfest
[hassfest]: https://github.com/foXaCe/alexa_media_player/actions/workflows/hassfest.yml
[downloads-shield]: https://img.shields.io/github/downloads/foXaCe/alexa_media_player/total?style=for-the-badge
