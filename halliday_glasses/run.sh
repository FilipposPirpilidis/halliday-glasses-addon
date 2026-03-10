#!/usr/bin/with-contenv bashio
set -euo pipefail

SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
LANGUAGE="$(bashio::config 'language')"
MODEL_VARIANT="$(bashio::config 'model_variant')"
MODEL_PATH="$(bashio::config 'model_path')"
ENABLE_OPENAI_REALTIME="$(bashio::config 'enable_openai_realtime')"
OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
OPENAI_REALTIME_MODEL="$(bashio::config 'openai_realtime_model')"
OPENAI_TRANSCRIPTION_MODEL="$(bashio::config 'openai_transcription_model')"
OPENAI_PROMPT="$(bashio::config 'openai_prompt')"

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
bashio::log.info "Using Vosk model variant ${MODEL_VARIANT}"
bashio::log.info "Using Vosk model at ${RESOLVED_MODEL_PATH}"

if bashio::var.true "${ENABLE_OPENAI_REALTIME}"; then
  bashio::log.info "OpenAI Realtime backend enabled"
  bashio::log.info "OpenAI session model ${OPENAI_REALTIME_MODEL}"
  bashio::log.info "OpenAI transcription model ${OPENAI_TRANSCRIPTION_MODEL}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --language "${LANGUAGE}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --enable-openai-realtime \
    --openai-api-key "${OPENAI_API_KEY}" \
    --openai-realtime-model "${OPENAI_REALTIME_MODEL}" \
    --openai-transcription-model "${OPENAI_TRANSCRIPTION_MODEL}" \
    --openai-prompt "${OPENAI_PROMPT}"
else
  bashio::log.info "Using Vosk backend"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --language "${LANGUAGE}" \
    --model-path "${RESOLVED_MODEL_PATH}"
fi
