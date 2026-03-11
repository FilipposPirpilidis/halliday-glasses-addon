# Home Assistant WebSocket Test

Minimal Python test client for the `halliday_glasses_bridge` Home Assistant integration.

It connects to Home Assistant `/api/websocket`, authenticates with a long-lived token, opens a Halliday stream, optionally updates translation settings, and can stream a PCM16 mono `.wav` or raw `.pcm` file.

## Run

```bash
cd /Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/ha-ws-test
python3 ha_ws_test.py \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --audio-file /absolute/path/to/sample.wav
```

Example with translation:

```bash
python3 ha_ws_test.py \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --translate-enabled true \
  --translate-source en \
  --translate-target el \
  --audio-file /absolute/path/to/sample.wav
```
