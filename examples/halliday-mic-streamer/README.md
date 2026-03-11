# Halliday Mic Streamer

macOS console application that:

- captures microphone audio
- converts it to PCM16 mono at 16 kHz
- opens a Wyoming TCP connection to the Halliday Glasses add-on
- streams `audio-chunk` packets continuously while running
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
swift run halliday-mic-streamer --host homeassistant.local --port 10310
```

Optional flags:

- `--language en`
- `--ha-token <token>` or `HA_TOKEN=...`
- `--translate-enabled true`
- `--translate-source en`
- `--translate-target el`

`--ha-token` is accepted for convenience but is not used for direct Wyoming TCP streaming.

Example with translation enabled:

```bash
cd examples/halliday-mic-streamer
swift run halliday-mic-streamer \
  --host homeassistant.local \
  --port 10310 \
  --translate-enabled true \
  --translate-source en \
  --translate-target el
```

## Notes

- The first run needs microphone permission.
- The terminal or app may need macOS Accessibility permission so the global ESC key monitor works.
- The add-on must allow the requested translation pair in `translate_pairs`.
