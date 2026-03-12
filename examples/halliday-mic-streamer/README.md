# Halliday Mic Streamer

macOS console application that:

- captures microphone audio
- converts it to PCM16 mono at 16 kHz or encodes Opus at 16 kHz mono
- connects to Home Assistant `/api/websocket`
- streams live microphone audio continuously while running
- prints `transcript_chunk` and final `transcript` responses

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
  --ha-token <your_home_assistant_token>
```

Optional flags:

- `--scheme ws`
- `--codec pcm16|opus`
- `--language en`
- `--ha-token <token>` or `HA_TOKEN=...`

Example with Opus:

```bash
cd examples/halliday-mic-streamer
swift run halliday-mic-streamer \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --codec opus
```

## Notes

- The first run needs microphone permission.
- The terminal or app may need macOS Accessibility permission so the global ESC key monitor works.
- Home Assistant must have the `halliday_glasses_bridge` custom integration installed and configured.
- `--codec opus` uses the native macOS Opus encoder and sends packetized Opus at 16 kHz mono.
