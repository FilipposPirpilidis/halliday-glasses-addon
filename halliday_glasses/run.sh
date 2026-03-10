#!/usr/bin/with-contenv bashio
set -euo pipefail

SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
LANGUAGE="$(bashio::config 'language')"
MODEL_VARIANT="$(bashio::config 'model_variant')"
MODEL_PATH="$(bashio::config 'model_path')"

case "${MODEL_VARIANT}" in
  "0.15")
    DEFAULT_MODEL_PATH="/models/vosk-model-small-en-us-0.15"
    ;;
  "0.22")
    DEFAULT_MODEL_PATH="/models/vosk-model-en-us-0.22"
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

exec python3 /app.py \
  --listen-host "${SERVER_HOST}" \
  --listen-port "${SERVER_PORT}" \
  --language "${LANGUAGE}" \
  --model-path "${RESOLVED_MODEL_PATH}"
