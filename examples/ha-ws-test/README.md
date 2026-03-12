# HA WebSocket Test

Minimal Python client for testing the Home Assistant WebSocket bridge.

It connects to Home Assistant `/api/websocket`, authenticates with a long-lived token, opens a Halliday stream, and can stream a PCM16 mono `.wav` or raw `.pcm` file.

## Run

```bash
cd examples/ha-ws-test
python3 ha_ws_test.py \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --audio-file /absolute/path/to/sample.wav
```
