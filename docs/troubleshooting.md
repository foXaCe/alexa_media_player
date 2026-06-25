# Troubleshooting

1. Enable debug logging:

   ```yaml
   logger:
     default: info
     logs:
       custom_components.alexa_media: debug
       alexapy: debug
   ```

2. Reproduce the issue, then collect the log from **Settings → System → Logs**.

3. Download a diagnostics dump: **Settings → Devices & Services → Alexa Media Player →
   ⋮ → Download diagnostics**.

Common issues (login loops, missing devices, region/Guard support) are documented in the
[FAQ](https://github.com/foXaCe/alexa_media_player/wiki/FAQ).

When opening an [issue](https://github.com/foXaCe/alexa_media_player/issues), include the
Home Assistant version, the integration version and the relevant logs (with credentials redacted).
