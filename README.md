# Halliday Glasses Home Assistant Add-on

This repository contains a Home Assistant add-on that exposes a Wyoming speech-to-text endpoint for Halliday Glasses.

## Backends

Set `stt_backend` to exactly one of these:

- `vosk`: bundled offline recognition inside the add-on
- `openai`: OpenAI Realtime transcription
- `whisplaybot`: Raspberry Pi WhisplayBot/Faster-Whisper recognizer

## Main Options

- `server_host`: bind address, default `0.0.0.0`
- `server_port`: internal Wyoming bridge port, default `10310`
- `language`: language hint, default `en`
- `stt_backend`: `vosk`, `openai`, or `whisplaybot`

## Home Assistant Integration

This repo also includes a custom integration in [`custom_components/halliday_glasses_bridge`](/Users/filippospirpilidis/Projects/halliday-glasses-addon/custom_components/halliday_glasses_bridge) so clients can connect only to Home Assistant `/api/websocket`.

Install it by copying that folder into Home Assistant:

```text
/config/custom_components/halliday_glasses_bridge
```

Then restart Home Assistant and add the integration from:

```text
Settings -> Devices & Services -> Add Integration
```

Use:

```text
Add-on host: homeassistant.local
Add-on port: 10310
```

YAML setup is still supported if you prefer `configuration.yaml`:

```yaml
halliday_glasses_bridge:
  addon_host: homeassistant.local
  addon_port: 10310
```

`addon_host` and `addon_port` are used only by Home Assistant to reach the Halliday add-on internally. External clients should not connect there directly.

### Dashboard

The integration is command-based and does not create entities by itself, so there is no built-in Lovelace card yet. The practical validation flow is:

1. install the custom integration
2. restart Home Assistant
3. run the Swift client or the Python test client in [`examples/ha-ws-test`](/Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/ha-ws-test)

If you want a Lovelace card later, the right next step is adding entities or a custom panel that subscribes to the `halliday_glasses_bridge` WebSocket events.

## Translation Options

- `translate_enabled`: enable final-text translation after STT
- `translate_url`: LibreTranslate `/translate` endpoint, default `http://127.0.0.1:5000/translate`
- `translate_pairs`: allowed source-target pairs such as `en-el` and `el-en`
- `translate_source`: default source language
- `translate_target`: default target language
- `translate_timeout_seconds`: HTTP timeout for translation requests

When translation is enabled, the add-on keeps sending the final result as a normal Wyoming `transcript` event, but `text` becomes the translated string. The original STT text is included as `original_text`.
If `translate_url` stays on `127.0.0.1`, the add-on starts LibreTranslate inside the same container and installs the configured `translate_pairs` models into `/data`.

## Vosk Options

- `model_variant`: `0.15` or `zamia`
- `model_path`: Vosk model path, default `/models/vosk-model-small-en-us-0.15`

## OpenAI Options

- `openai_api_key`: OpenAI API key
- `openai_realtime_model`: realtime session model, default `gpt-realtime-mini`
- `openai_transcription_model`: transcription model, default `gpt-4o-mini-transcribe`
- `openai_prompt`: optional prompt

## WhisplayBot Options

- `whisplaybot_recognize_url`: default `http://192.168.2.29:8801/recognize`
- `whisplaybot_timeout_seconds`
- `whisplaybot_partial_window_seconds`
- `whisplaybot_partial_inference_seconds`
- `whisplaybot_auto_final_silence_ms`
- `whisplaybot_auto_final_min_seconds`
- `whisplaybot_auto_final_silence_level`

For the LM8850 prebuilt image, see:
[PiSugar Whisplay AI Chatbot Prebuild Image - LLM8850](https://github.com/PiSugar/whisplay-ai-chatbot/wiki/Prebuild-Image-%E2%80%90-LLM8850)

## Client Protocol

Clients should connect through the Home Assistant WebSocket API, not directly to the add-on.

The external client URL is:

```text
ws://<home-assistant-host>:8123/api/websocket
```

or:

```text
wss://<home-assistant-host>:8123/api/websocket
```

The client must first complete the normal Home Assistant WebSocket auth handshake:

1. receive `auth_required`
2. send:

```json
{"type":"auth","access_token":"<your_home_assistant_token>"}
```

3. wait for `auth_ok`

Each message is:

1. one JSON object per WebSocket text frame
2. audio chunks are base64-encoded in the `audio` field

### Client -> Add-on

Send these Home Assistant WebSocket commands:

- `halliday_glasses_bridge/open_stream`
  Opens a new stream and returns `session_id`.
- `halliday_glasses_bridge/audio_chunk`
  Sends PCM16 mono audio bytes for an existing session.
- `halliday_glasses_bridge/close_stream`
  Closes a stream.
- `halliday_glasses_bridge/translate_get`
  Requests current runtime translation settings.
- `halliday_glasses_bridge/translate_set`
  Updates runtime translation settings for a session.

Example `open_stream`:

```json
{"id":1,"type":"halliday_glasses_bridge/open_stream","language":"en","rate":16000,"width":2,"channels":1}
```

Example success result:

```json
{"id":1,"type":"result","success":true,"result":{"session_id":"139901234560000:1"}}
```

Example `audio_chunk`:

```json
{"id":2,"type":"halliday_glasses_bridge/audio_chunk","session_id":"139901234560000:1","rate":16000,"width":2,"channels":1,"audio":"<base64-pcm16-mono>"}
```

Example `translate-set`:

```json
{"id":3,"type":"halliday_glasses_bridge/translate_set","session_id":"139901234560000:1","enabled":true,"source":"en","target":"el"}
```

or:

```json
{"id":3,"type":"halliday_glasses_bridge/translate_set","session_id":"139901234560000:1","enabled":true,"pair":"en-el"}
```

### Add-on -> Client

Home Assistant sends normal WebSocket `event` messages back:

- `transcript_chunk`
  Partial live caption text.
- `transcript`
  Final caption text.
- `translate_config`
  Current runtime translation settings.
- `error`
  Error message.

Example `transcript_chunk` event:

```json
{"id":1,"type":"event","event":{"type":"transcript_chunk","text":"hello wor"}}
```

Example final `transcript` without translation:

```json
{"id":1,"type":"event","event":{"type":"transcript","text":"hello world"}}
```

Example final `transcript` with translation enabled:

```json
{"id":1,"type":"event","event":{"type":"transcript","text":"γειά σου κόσμε","original_text":"hello world","translated":true,"source_language":"en","target_language":"el"}}
```

Example `translate_config` event:

```json
{"id":1,"type":"event","event":{"type":"translate_config","enabled":true,"pairs":["en-el","el-en"],"pair":"en-el","source":"en","target":"el"}}
```

## Test Client

A minimal Home Assistant WebSocket test client is included in [`examples/ha-ws-test/ha_ws_test.py`](/Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/ha-ws-test/ha_ws_test.py).

Example:

```bash
cd /Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/ha-ws-test
python3 ha_ws_test.py \
  --host homeassistant.local \
  --port 8123 \
  --ha-token <your_home_assistant_token> \
  --audio-file /absolute/path/to/sample.wav
```

## Notes

- The add-on expects PCM16 mono input.
- `stt_backend` selects one backend only.
- When `stt_backend: openai`, the add-on resamples PCM16 mono audio to `24 kHz` before sending it to OpenAI Realtime.
- When `stt_backend: whisplaybot`, the add-on talks directly to the Pi `recognize` HTTP API and generates live partial/final captions itself.
- Translation is applied only to final text, not to partial captions.
- A client can change translation settings at runtime without opening the Home Assistant configuration page:
  - send `translate-get` to receive the current pair and available pairs
  - send `translate-set` with `enabled`, `pair`, or explicit `source` and `target`

Example `translate-set` event data:

```json
{
  "enabled": true,
  "pair": "en-el"
}
```
