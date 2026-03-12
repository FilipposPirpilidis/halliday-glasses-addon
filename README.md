# Halliday Glasses Home Assistant Add-on

This repository contains a Home Assistant add-on that exposes live speech-to-text for Halliday Glasses.

## Distribution

- HACS: installs the `halliday_glasses_bridge` custom integration
- Home Assistant Add-on Store: installs the `halliday_glasses` add-on

HACS does not install Home Assistant add-ons, so this repo uses both distribution paths.

## Backends

Set `stt_backend` to one of:

- `vosk`
- `openai`
- `assemblyai`
- `whisplaybot`

## Main Options

- `server_host`
- `server_port`
- `accepted_audio_codecs`: `pcm16` and `opus`
- `language`
- `stt_backend`

## Backend Options

### Vosk

- `model_variant`: `0.15` or `zamia`
- `model_path`

### OpenAI

- `openai_api_key`
- `openai_realtime_model`
- `openai_transcription_model`
- `openai_prompt`

### AssemblyAI

- `assemblyai_api_key`
- `assemblyai_speech_model`

### WhisplayBot

- `whisplaybot_recognize_url`
- `whisplaybot_timeout_seconds`
- `whisplaybot_partial_window_seconds`
- `whisplaybot_partial_inference_seconds`
- `whisplaybot_auto_final_silence_ms`
- `whisplaybot_auto_final_min_seconds`
- `whisplaybot_auto_final_silence_level`

For the LM8850 prebuilt image, see:
[PiSugar Whisplay AI Chatbot Prebuild Image - LLM8850](https://github.com/PiSugar/whisplay-ai-chatbot/wiki/Prebuild-Image-%E2%80%90-LLM8850)

## Home Assistant Integration

This repo also includes a custom integration in [`/Users/filippospirpilidis/Projects/halliday-glasses-addon/custom_components/halliday_glasses_bridge`](/Users/filippospirpilidis/Projects/halliday-glasses-addon/custom_components/halliday_glasses_bridge) so clients connect only to Home Assistant `/api/websocket`.

### Install With HACS

1. Open HACS in Home Assistant.
2. Go to `Integrations`.
3. Open the three-dot menu and choose `Custom repositories`.
4. Add:

```text
https://github.com/FilipposPirpilidis/halliday-glasses-addon
```

5. Category: `Integration`
6. Install `Halliday Glasses Bridge`
7. Restart Home Assistant

### Install Add-on

Install the add-on separately from the Home Assistant Add-on Store using this repository URL:

```text
https://github.com/FilipposPirpilidis/halliday-glasses-addon
```

Install it into Home Assistant:

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

## Client Protocol

Clients connect to:

```text
ws://<home-assistant-host>:8123/api/websocket
```

or:

```text
wss://<home-assistant-host>:8123/api/websocket
```

Then complete the normal Home Assistant auth handshake.

### Client Commands

- `halliday_glasses_bridge/open_stream`
- `halliday_glasses_bridge/audio_chunk`
- `halliday_glasses_bridge/close_stream`

Example `open_stream`:

```json
{"id":1,"type":"halliday_glasses_bridge/open_stream","language":"en","rate":16000,"width":2,"channels":1}
```

Example Opus `open_stream`:

```json
{"id":1,"type":"halliday_glasses_bridge/open_stream","language":"en","codec":"opus","rate":16000,"width":2,"channels":1}
```

Example `audio_chunk`:

```json
{"id":2,"type":"halliday_glasses_bridge/audio_chunk","session_id":"139901234560000:1","rate":16000,"width":2,"channels":1,"audio":"<base64-audio>"}
```

### Server Events

- `transcript_chunk`
- `transcript`
- `error`

Example partial:

```json
{"id":1,"type":"event","event":{"type":"transcript_chunk","text":"hello wor"}}
```

Example final:

```json
{"id":1,"type":"event","event":{"type":"transcript","text":"hello world"}}
```

## Test Clients

Swift mic streamer:
[`/Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/halliday-mic-streamer`](/Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/halliday-mic-streamer)

Python websocket test client:
[`/Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/ha-ws-test/ha_ws_test.py`](/Users/filippospirpilidis/Projects/halliday-glasses-addon/examples/ha-ws-test/ha_ws_test.py)

## Notes

- The add-on is transcription-only.
- `stt_backend` selects one backend only.
- When `stt_backend: openai`, the add-on resamples PCM16 mono audio to `24 kHz` before sending it to OpenAI Realtime.
- When `stt_backend: whisplaybot`, the add-on talks directly to the Pi `recognize` HTTP API and generates live partial and final captions itself.
