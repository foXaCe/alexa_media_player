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

See the [Configuration wiki page](https://github.com/foXaCe/alexa_media_player/wiki/Configuration).

## Documentation

Full documentation, FAQ and automation examples live in the [wiki](https://github.com/foXaCe/alexa_media_player/wiki).

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
