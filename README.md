# Halliday Glasses Home Assistant Add-on

This repository contains a custom Home Assistant add-on that exposes a Wyoming speech-to-text endpoint for Halliday Glasses and can transcribe with local Vosk, OpenAI Realtime, or a remote Whisplay/Faster-Whisper HTTP recognizer.

## Contents

- `halliday_glasses/` - the add-on package
- `repository.yaml` - add-on repository metadata
- `tmp/` - local reference examples used while building the add-on

## What It Does

The add-on:

- listens as a Wyoming server on port `10310`
- accepts `audio-start`, `audio-chunk`, and `audio-stop` events with PCM16 mono audio
- runs Vosk locally inside the add-on by default
- can optionally switch to OpenAI Realtime transcription
- can optionally switch to a remote Whisplay/Faster-Whisper recognizer over HTTP
- emits `transcript-chunk` updates while audio is still arriving
- emits a final `transcript` event when Vosk detects an utterance boundary or when the stream stops

## Configuration

Default add-on options:

- `server_host`: bind address for this add-on, default `0.0.0.0`
- `server_port`: exposed Wyoming port for Halliday Glasses, default `10310`
- `language`: transcription language hint, default `en`
- `stt_backend`: one of `vosk`, `openai`, or `whisplay`
- `model_variant`: bundled model preset, one of `0.15` or `zamia`
- `model_path`: filesystem path to the Vosk model directory, default `/models/vosk-model-small-en-us-0.15`
- `openai_api_key`: OpenAI API key for Realtime transcription
- `openai_realtime_model`: realtime session model, default `gpt-realtime-mini`
- `openai_transcription_model`: transcription model inside the realtime session, default `gpt-4o-mini-transcribe`
- `openai_prompt`: optional transcription prompt
- `whisplay_recognize_url`: HTTP recognize endpoint of the Whisplay/Faster-Whisper service, default `http://192.168.2.29:8801/recognize`

## Home Assistant Setup

1. Add this repository to your Home Assistant add-on store.
2. Install **Halliday Glasses**.
3. Start the add-on.
4. Point your Halliday Glasses client to this add-on's Wyoming endpoint on port `10310`.

## Notes

- The container downloads the official Vosk small English model `vosk-model-small-en-us-0.15` during build from [Alpha Cephei](https://alphacephei.com/vosk/models).
- The container bundles three English Vosk models during build:
  - `0.15` -> `/models/vosk-model-small-en-us-0.15`
  - `zamia` -> `/models/vosk-model-small-en-us-zamia-0.5`
- The add-on expects PCM16 mono input. Stereo or non-16-bit streams are rejected.
- `model_variant` controls which bundled model is used. `model_path` can still override it if you want to point at a custom model under `/data/models/...`.
- When `stt_backend: openai`, the add-on disables Vosk recognition and forwards audio to the OpenAI Realtime API instead.
- OpenAI Realtime transcription currently expects `audio/pcm` at `24 kHz` mono, so the add-on resamples incoming PCM16 mono audio before sending it. This is based on the current official OpenAI Realtime transcription docs: [Realtime transcription](https://developers.openai.com/api/docs/guides/realtime-transcription), [Realtime WebSocket](https://developers.openai.com/api/docs/guides/realtime-websocket).
- When `stt_backend: whisplay`, the add-on talks directly to the Pi `recognize` HTTP API, periodically transcribes a trailing audio window for partial captions, and emits final captions on silence or stop.
