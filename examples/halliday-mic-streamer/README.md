# Halliday Mic Streamer

macOS console application that:

- captures microphone audio
- converts it to PCM16 mono at 16 kHz
- opens a Wyoming TCP connection to the Halliday Glasses add-on
- streams `audio-chunk` packets while SPACE is held
- prints `transcript-chunk` and final `transcript` responses

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

`--ha-token` is accepted for convenience but is not used for direct Wyoming TCP streaming.

## Notes

- The first run needs microphone permission.
- The terminal or app may also need macOS Accessibility permission so the global SPACE key monitor works.
