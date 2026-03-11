#!/usr/bin/with-contenv bashio
set -euo pipefail

SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
LANGUAGE="$(bashio::config 'language')"
STT_BACKEND="$(bashio::config 'stt_backend')"
MODEL_VARIANT="$(bashio::config 'model_variant')"
MODEL_PATH="$(bashio::config 'model_path')"
OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
OPENAI_REALTIME_MODEL="$(bashio::config 'openai_realtime_model')"
OPENAI_TRANSCRIPTION_MODEL="$(bashio::config 'openai_transcription_model')"
OPENAI_PROMPT="$(bashio::config 'openai_prompt')"
WHISPLAY_RECOGNIZE_URL="$(bashio::config 'whisplay_recognize_url')"

case "${MODEL_VARIANT}" in
  "0.15")
    DEFAULT_MODEL_PATH="/models/vosk-model-small-en-us-0.15"
    ;;
  "zamia")
    DEFAULT_MODEL_PATH="/models/vosk-model-small-en-us-zamia-0.5"
    ;;
  *)
    bashio::log.warning "Unknown model_variant '${MODEL_VARIANT}', falling back to model_path"
    DEFAULT_MODEL_PATH=""
    ;;
esac

if [ "${MODEL_PATH}" = "/models/vosk-model-small-en-us-0.15" ] && [ -n "${DEFAULT_MODEL_PATH}" ]; then
  RESOLVED_MODEL_PATH="${DEFAULT_MODEL_PATH}"
else
  RESOLVED_MODEL_PATH="${MODEL_PATH}"
fi

bashio::log.info "Starting Halliday Glasses add-on"
bashio::log.info "Listening on ${SERVER_HOST}:${SERVER_PORT}"
bashio::log.info "Using STT backend ${STT_BACKEND}"

if [ "${STT_BACKEND}" = "openai" ]; then
  bashio::log.info "OpenAI backend enabled"
  bashio::log.info "OpenAI realtime session model ${OPENAI_REALTIME_MODEL}"
  bashio::log.info "OpenAI transcription model ${OPENAI_TRANSCRIPTION_MODEL}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --language "${LANGUAGE}" \
    --stt-backend "${STT_BACKEND}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --openai-api-key "${OPENAI_API_KEY}" \
    --openai-realtime-model "${OPENAI_REALTIME_MODEL}" \
    --openai-transcription-model "${OPENAI_TRANSCRIPTION_MODEL}" \
    --openai-prompt "${OPENAI_PROMPT}"
elif [ "${STT_BACKEND}" = "whisplay" ]; then
  bashio::log.info "Whisplay backend enabled"
  bashio::log.info "Whisplay recognize URL ${WHISPLAY_RECOGNIZE_URL}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --language "${LANGUAGE}" \
    --stt-backend "${STT_BACKEND}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --whisplay-recognize-url "${WHISPLAY_RECOGNIZE_URL}"
else
  bashio::log.info "Using Vosk backend"
  bashio::log.info "Using Vosk model variant ${MODEL_VARIANT}"
  bashio::log.info "Using Vosk model at ${RESOLVED_MODEL_PATH}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --language "${LANGUAGE}" \
    --stt-backend "vosk" \
    --model-path "${RESOLVED_MODEL_PATH}"
fi
