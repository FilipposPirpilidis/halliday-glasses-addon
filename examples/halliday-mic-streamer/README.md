# Halliday Mic Streamer

macOS console application that:

- captures microphone audio
- converts it to PCM16 mono at 16 kHz
- connects to the Halliday Glasses add-on through Home Assistant ingress WebSocket
- streams live microphone audio continuously while running
- prints `transcript-chunk` and final `transcript` responses
- can enable or disable translation and set source/target languages from the client

## Build

```bash
cd examples/halliday-mic-streamer
swift build
```

## Run

```bash
cd examples/halliday-mic-streamer
swift run halliday-mic-streamer \
  --host homeassistant.local \
  --port 8123 \
  --ingress-path api/hassio_ingress/<id> \
  --ha-token <your_home_assistant_token>
```

Optional flags:

- `--scheme ws`
- `--language en`
- `--ha-token <token>` or `HA_TOKEN=...`
- `--ingress-path api/hassio_ingress/<id>` or `HA_INGRESS_PATH=...`
- `--translate-enabled true`
- `--translate-source en`
- `--translate-target el`

Example with translation enabled:

```bash
cd examples/halliday-mic-streamer
swift run halliday-mic-streamer \
  --host homeassistant.local \
  --port 8123 \
  --ingress-path api/hassio_ingress/<id> \
  --ha-token <your_home_assistant_token> \
  --translate-enabled true \
  --translate-source en \
  --translate-target el
```

## Notes

- The first run needs microphone permission.
- The terminal or app may need macOS Accessibility permission so the global ESC key monitor works.
- Open the add-on with **Open Web UI** once to get the `api/hassio_ingress/<id>` path from the browser URL.
