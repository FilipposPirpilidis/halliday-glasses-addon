# Halliday Glasses Home Assistant Add-on

This repository contains a custom Home Assistant add-on that exposes a Wyoming speech-to-text endpoint for Halliday Glasses and forwards PCM16 audio streams to an existing Wyoming Whisper server.

## Contents

- `halliday_glasses/` - the add-on package
- `repository.yaml` - add-on repository metadata
- `tmp/` - local reference examples used while building the add-on

## What It Does

The add-on:

- listens as a Wyoming server on port `10310`
- accepts `audio-start`, `audio-chunk`, and `audio-stop` events with PCM16 audio
- forwards the buffered audio to your existing Wyoming Whisper service
- emits `transcript-chunk` updates while audio is still arriving
- emits a final `transcript` event after `audio-stop`

## Configuration

Default add-on options:

- `whisper_host`: hostname or IP of your existing Wyoming Whisper server
- `whisper_port`: Wyoming port of that Whisper server, default `10300`
- `server_host`: bind address for this add-on, default `0.0.0.0`
- `server_port`: exposed Wyoming port for Halliday Glasses, default `10310`
- `language`: transcription language hint, default `en`
- `partial_interval_ms`: how often partial updates are attempted, default `1500`
- `min_partial_audio_ms`: minimum buffered audio before partial transcription starts, default `1200`

## Home Assistant Setup

1. Add this repository to your Home Assistant add-on store.
2. Install **Halliday Glasses**.
3. Set `whisper_host` and `whisper_port` to the Wyoming Whisper instance already running in your Home Assistant environment.
4. Start the add-on.
5. Point your Halliday Glasses client to this add-on's Wyoming endpoint on port `10310`.

## Notes

- The add-on assumes incoming audio is PCM16 and forwards the original stream metadata (`rate`, `width`, `channels`) upstream.
- Partial transcripts are implemented by periodically re-transcribing the growing audio buffer. This keeps the implementation dependency-free, but it is less efficient than a streaming ASR engine with native incremental decoding.
