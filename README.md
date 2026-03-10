# Halliday Glasses Home Assistant Add-on

This repository contains a custom Home Assistant add-on that exposes a Wyoming speech-to-text endpoint for Halliday Glasses and performs local transcription with Vosk.

## Contents

- `halliday_glasses/` - the add-on package
- `repository.yaml` - add-on repository metadata
- `tmp/` - local reference examples used while building the add-on

## What It Does

The add-on:

- listens as a Wyoming server on port `10310`
- accepts `audio-start`, `audio-chunk`, and `audio-stop` events with PCM16 mono audio
- runs Vosk locally inside the add-on
- emits `transcript-chunk` updates while audio is still arriving
- emits a final `transcript` event when Vosk detects an utterance boundary or when the stream stops

## Configuration

Default add-on options:

- `server_host`: bind address for this add-on, default `0.0.0.0`
- `server_port`: exposed Wyoming port for Halliday Glasses, default `10310`
- `language`: transcription language hint, default `en`

## Home Assistant Setup

1. Add this repository to your Home Assistant add-on store.
2. Install **Halliday Glasses**.
3. Start the add-on.
4. Point your Halliday Glasses client to this add-on's Wyoming endpoint on port `10310`.

## Notes

- The container downloads the official Vosk small English model `vosk-model-small-en-us-0.15` during build from [Alpha Cephei](https://alphacephei.com/vosk/models).
- The add-on expects PCM16 mono input. Stereo or non-16-bit streams are rejected.
