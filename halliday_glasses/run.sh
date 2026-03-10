#!/usr/bin/with-contenv bashio
set -euo pipefail

WHISPER_HOST="$(bashio::config 'whisper_host')"
WHISPER_PORT="$(bashio::config 'whisper_port')"
SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
LANGUAGE="$(bashio::config 'language')"
PARTIAL_INTERVAL_MS="$(bashio::config 'partial_interval_ms')"
MIN_PARTIAL_AUDIO_MS="$(bashio::config 'min_partial_audio_ms')"
SILENCE_MS="$(bashio::config 'silence_ms')"
MIN_UTTERANCE_MS="$(bashio::config 'min_utterance_ms')"
SPEECH_THRESHOLD="$(bashio::config 'speech_threshold')"

bashio::log.info "Starting Halliday Glasses add-on"
bashio::log.info "Listening on ${SERVER_HOST}:${SERVER_PORT}"
bashio::log.info "Forwarding transcription to Wyoming Whisper at ${WHISPER_HOST}:${WHISPER_PORT}"

exec python3 /app.py \
  --listen-host "${SERVER_HOST}" \
  --listen-port "${SERVER_PORT}" \
  --whisper-host "${WHISPER_HOST}" \
  --whisper-port "${WHISPER_PORT}" \
  --language "${LANGUAGE}" \
  --partial-interval-ms "${PARTIAL_INTERVAL_MS}" \
  --min-partial-audio-ms "${MIN_PARTIAL_AUDIO_MS}" \
  --silence-ms "${SILENCE_MS}" \
  --min-utterance-ms "${MIN_UTTERANCE_MS}" \
  --speech-threshold "${SPEECH_THRESHOLD}"
