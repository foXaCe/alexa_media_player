# FAQ

## Login issues / endless authentication loop

Amazon occasionally invalidates cached sessions. Open the integration, follow the
re-authentication prompt (a repair notification is raised), or use the
`alexa_media.force_logout` action to start from a clean session. Two-factor
authentication must use the **Authenticator App** method; the built-in OTP secret
completes it automatically during the flow.

## Some devices are missing

Check `include_devices` / `exclude_devices` in the integration options. Devices
turned off or removed from your Amazon account disappear from the list until the
next successful refresh. When `extended_entity_discovery` is disabled, only media
player entities are created.

## Guard / region features unavailable

Alexa Guard and other region-dependent features require a supported Amazon domain
and device/region combination. Set the correct **Amazon login URL** (e.g.
`amazon.de`, `amazon.co.uk`) during setup.

## Announcements fail

Text-to-speech, mobile pushes and announcements are subject to Amazon-side rate
limits. Space out commands and avoid rapid bursts.

## Why does the integration rely on an unofficial API?

This project replicates the Alexa app's behaviour through the unofficial API. Amazon
may change or block it at any time; that is an inherent limitation of the approach.

For anything else, open an [issue](https://github.com/foXaCe/alexa_media_player/issues)
with the relevant logs and diagnostics.
