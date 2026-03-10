#!/usr/bin/with-contenv bashio
set -euo pipefail

SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
LANGUAGE="$(bashio::config 'language')"

bashio::log.info "Starting Halliday Glasses add-on"
bashio::log.info "Listening on ${SERVER_HOST}:${SERVER_PORT}"
bashio::log.info "Using bundled Vosk model for English transcription"

exec python3 /app.py \
  --listen-host "${SERVER_HOST}" \
  --listen-port "${SERVER_PORT}" \
  --language "${LANGUAGE}"
