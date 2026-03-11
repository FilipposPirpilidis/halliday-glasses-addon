# Halliday Glasses Home Assistant Add-on

This repository contains a Home Assistant add-on that exposes a Wyoming speech-to-text endpoint for Halliday Glasses.

## Backends

Set `stt_backend` to exactly one of these:

- `vosk`: bundled offline recognition inside the add-on
- `openai`: OpenAI Realtime transcription
- `whisplaybot`: Raspberry Pi WhisplayBot/Faster-Whisper recognizer

## Main Options

- `server_host`: bind address, default `0.0.0.0`
- `server_port`: exposed Wyoming port, default `10310`
- `language`: language hint, default `en`
- `stt_backend`: `vosk`, `openai`, or `whisplaybot`

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

## Notes

- The add-on expects PCM16 mono input.
- `stt_backend` selects one backend only.
- When `stt_backend: openai`, the add-on resamples PCM16 mono audio to `24 kHz` before sending it to OpenAI Realtime.
- When `stt_backend: whisplaybot`, the add-on talks directly to the Pi `recognize` HTTP API and generates live partial/final captions itself.
