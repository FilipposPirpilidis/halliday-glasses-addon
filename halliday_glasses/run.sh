#!/usr/bin/with-contenv bashio
set -euo pipefail

SERVER_HOST="$(bashio::config 'server_host')"
SERVER_PORT="$(bashio::config 'server_port')"
WEBSOCKET_HOST="0.0.0.0"
WEBSOCKET_PORT="8099"
ACCEPTED_AUDIO_CODECS="$(bashio::config 'accepted_audio_codecs')"
LANGUAGE="$(bashio::config 'language')"
STT_BACKEND="$(bashio::config 'stt_backend')"
MODEL_VARIANT="$(bashio::config 'model_variant')"
MODEL_PATH="$(bashio::config 'model_path')"
OPENAI_API_KEY="$(bashio::config 'openai_api_key')"
OPENAI_REALTIME_MODEL="$(bashio::config 'openai_realtime_model')"
OPENAI_TRANSCRIPTION_MODEL="$(bashio::config 'openai_transcription_model')"
OPENAI_TRANSLATION_MODEL="$(bashio::config 'openai_translation_model')"
OPENAI_PROMPT="$(bashio::config 'openai_prompt')"
WHISPLAYBOT_RECOGNIZE_URL="$(bashio::config 'whisplaybot_recognize_url')"
WHISPLAYBOT_TIMEOUT_SECONDS="$(bashio::config 'whisplaybot_timeout_seconds')"
WHISPLAYBOT_PARTIAL_WINDOW_SECONDS="$(bashio::config 'whisplaybot_partial_window_seconds')"
WHISPLAYBOT_PARTIAL_INFERENCE_SECONDS="$(bashio::config 'whisplaybot_partial_inference_seconds')"
WHISPLAYBOT_AUTO_FINAL_SILENCE_MS="$(bashio::config 'whisplaybot_auto_final_silence_ms')"
WHISPLAYBOT_AUTO_FINAL_MIN_SECONDS="$(bashio::config 'whisplaybot_auto_final_min_seconds')"
WHISPLAYBOT_AUTO_FINAL_SILENCE_LEVEL="$(bashio::config 'whisplaybot_auto_final_silence_level')"
WHISPLAY_AGENT_ENABLED="$(bashio::config 'whisplay_agent_enabled')"
WHISPLAY_AGENT_URL="$(bashio::config 'whisplay_agent_url')"
WHISPLAY_AGENT_MODEL="$(bashio::config 'whisplay_agent_model')"
WHISPLAY_AGENT_PROMPT="$(bashio::config 'whisplay_agent_prompt')"
WHISPLAY_AGENT_TIMEOUT_SECONDS="$(bashio::config 'whisplay_agent_timeout_seconds')"
TRANSLATE_ENABLED="$(bashio::config 'translate_enabled')"
TRANSLATE_URL="$(bashio::config 'translate_url')"
TRANSLATE_PAIRS="$(bashio::config 'translate_pairs')"
TRANSLATE_SOURCE="$(bashio::config 'translate_source')"
TRANSLATE_TARGET="$(bashio::config 'translate_target')"
TRANSLATE_TIMEOUT_SECONDS="$(bashio::config 'translate_timeout_seconds')"
LIBRETRANSLATE_HOST="127.0.0.1"
LIBRETRANSLATE_PORT="5000"

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
bashio::log.info "WebSocket ingress bridge on ${WEBSOCKET_HOST}:${WEBSOCKET_PORT}/ws"
bashio::log.info "Accepted audio codecs ${ACCEPTED_AUDIO_CODECS}"
bashio::log.info "Using STT backend ${STT_BACKEND}"
if bashio::var.true "${TRANSLATE_ENABLED}"; then
  if [ "${STT_BACKEND}" = "openai" ]; then
    bashio::log.info "Translation enabled via OpenAI model ${OPENAI_TRANSLATION_MODEL}"
  else
    bashio::log.info "Translation enabled via ${TRANSLATE_URL}"
  fi
  bashio::log.info "Selected translation pair ${TRANSLATE_SOURCE}-${TRANSLATE_TARGET}"
else
  bashio::log.info "Translation disabled"
fi

TRANSLATE_ARGS=(
  --translate-url "${TRANSLATE_URL}"
  --translate-pairs "${TRANSLATE_PAIRS}"
  --translate-source "${TRANSLATE_SOURCE}"
  --translate-target "${TRANSLATE_TARGET}"
  --translate-timeout-seconds "${TRANSLATE_TIMEOUT_SECONDS}"
)

if bashio::var.true "${TRANSLATE_ENABLED}"; then
  TRANSLATE_ARGS+=(--translate-enabled)
fi

cleanup() {
  if [ -n "${LIBRETRANSLATE_PID:-}" ]; then
    kill "${LIBRETRANSLATE_PID}" 2>/dev/null || true
  fi
}

start_internal_libretranslate() {
  export XDG_DATA_HOME=/data
  mkdir -p /data/argos-translate

  python3 - <<'PY'
import json
import os
import re

import argostranslate.package as package

DEFAULT_PAIRS = ["en-el", "el-en", "en-de", "de-en", "en-fr", "fr-en"]
PAIR_RE = re.compile(r"^[a-z]{2,3}-[a-z]{2,3}$")
configured_pairs = []
options_path = "/data/options.json"
if os.path.exists(options_path):
    with open(options_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    raw_pairs = data.get("translate_pairs")
    if isinstance(raw_pairs, list):
        configured_pairs = raw_pairs

pairs = []
for pair in configured_pairs:
    if not isinstance(pair, str):
        continue
    normalized = pair.strip().lower().replace("_", "-")
    if PAIR_RE.match(normalized):
        pairs.append(normalized)

if not pairs:
    pairs = DEFAULT_PAIRS

seen = set()
pairs = [pair for pair in pairs if not (pair in seen or seen.add(pair))]
print(f"[halliday_glasses] LibreTranslate requested pairs: {pairs}")

installed = {(pkg.from_code, pkg.to_code) for pkg in package.get_installed_packages()}
missing = []
for pair in pairs:
    src, dst = pair.split("-")
    if (src, dst) not in installed:
        missing.append((src, dst))

if missing:
    print(f"[halliday_glasses] LibreTranslate missing models: {missing}")
    package.update_package_index()
    available = {(pkg.from_code, pkg.to_code): pkg for pkg in package.get_available_packages()}
    for src, dst in missing:
        selected = available.get((src, dst))
        if selected is None:
            print(f"[halliday_glasses] LibreTranslate model not found: {src}->{dst}")
            continue
        print(f"[halliday_glasses] Installing LibreTranslate model: {src}->{dst}")
        package.install_from_path(selected.download())
else:
    print("[halliday_glasses] LibreTranslate models already installed")
PY

  bashio::log.info "Starting internal LibreTranslate on ${LIBRETRANSLATE_HOST}:${LIBRETRANSLATE_PORT}"
  libretranslate --host "${LIBRETRANSLATE_HOST}" --port "${LIBRETRANSLATE_PORT}" &
  LIBRETRANSLATE_PID=$!
  trap cleanup EXIT INT TERM

  for _ in $(seq 1 60); do
    if curl --silent --fail "http://${LIBRETRANSLATE_HOST}:${LIBRETRANSLATE_PORT}/languages" >/dev/null 2>&1; then
      bashio::log.info "Internal LibreTranslate is ready"
      return
    fi
    sleep 1
  done

  bashio::log.warning "Internal LibreTranslate did not become ready before timeout"
}

if bashio::var.true "${TRANSLATE_ENABLED}" && [ "${STT_BACKEND}" != "openai" ]; then
  if [ "${TRANSLATE_URL}" = "http://127.0.0.1:5000/translate" ] || [ "${TRANSLATE_URL}" = "http://localhost:5000/translate" ]; then
    start_internal_libretranslate
  else
    bashio::log.info "Using external translation endpoint ${TRANSLATE_URL}"
  fi
elif bashio::var.true "${TRANSLATE_ENABLED}" && [ "${STT_BACKEND}" = "openai" ]; then
  :
fi

if [ "${STT_BACKEND}" = "openai" ]; then
  bashio::log.info "OpenAI backend enabled"
  bashio::log.info "OpenAI realtime session model ${OPENAI_REALTIME_MODEL}"
  bashio::log.info "OpenAI transcription model ${OPENAI_TRANSCRIPTION_MODEL}"
  bashio::log.info "OpenAI translation model ${OPENAI_TRANSLATION_MODEL}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --websocket-host "${WEBSOCKET_HOST}" \
    --websocket-port "${WEBSOCKET_PORT}" \
    --accepted-audio-codecs "${ACCEPTED_AUDIO_CODECS}" \
    --language "${LANGUAGE}" \
    --stt-backend "${STT_BACKEND}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --openai-api-key "${OPENAI_API_KEY}" \
    --openai-realtime-model "${OPENAI_REALTIME_MODEL}" \
    --openai-transcription-model "${OPENAI_TRANSCRIPTION_MODEL}" \
    --openai-translation-model "${OPENAI_TRANSLATION_MODEL}" \
    --openai-prompt "${OPENAI_PROMPT}" \
    "${TRANSLATE_ARGS[@]}"
elif [ "${STT_BACKEND}" = "whisplaybot" ]; then
  bashio::log.info "WhisplayBot backend enabled"
  bashio::log.info "WhisplayBot recognize URL ${WHISPLAYBOT_RECOGNIZE_URL}"
  if bashio::var.true "${WHISPLAY_AGENT_ENABLED}"; then
    bashio::log.info "Whisplay agent enabled via ${WHISPLAY_AGENT_URL} using model ${WHISPLAY_AGENT_MODEL}"
  fi
  WHISPLAY_AGENT_ARGS=()
  if bashio::var.true "${WHISPLAY_AGENT_ENABLED}"; then
    WHISPLAY_AGENT_ARGS+=(--whisplay-agent-enabled)
  fi
  WHISPLAY_AGENT_ARGS+=(
    --whisplay-agent-url "${WHISPLAY_AGENT_URL}"
    --whisplay-agent-model "${WHISPLAY_AGENT_MODEL}"
    --whisplay-agent-prompt "${WHISPLAY_AGENT_PROMPT}"
    --whisplay-agent-timeout-seconds "${WHISPLAY_AGENT_TIMEOUT_SECONDS}"
  )
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --websocket-host "${WEBSOCKET_HOST}" \
    --websocket-port "${WEBSOCKET_PORT}" \
    --accepted-audio-codecs "${ACCEPTED_AUDIO_CODECS}" \
    --language "${LANGUAGE}" \
    --stt-backend "${STT_BACKEND}" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    --whisplay-recognize-url "${WHISPLAYBOT_RECOGNIZE_URL}" \
    --whisplay-timeout-seconds "${WHISPLAYBOT_TIMEOUT_SECONDS}" \
    --whisplay-partial-window-seconds "${WHISPLAYBOT_PARTIAL_WINDOW_SECONDS}" \
    --whisplay-partial-inference-seconds "${WHISPLAYBOT_PARTIAL_INFERENCE_SECONDS}" \
    --whisplay-auto-final-silence-ms "${WHISPLAYBOT_AUTO_FINAL_SILENCE_MS}" \
    --whisplay-auto-final-min-seconds "${WHISPLAYBOT_AUTO_FINAL_MIN_SECONDS}" \
    --whisplay-auto-final-silence-level "${WHISPLAYBOT_AUTO_FINAL_SILENCE_LEVEL}" \
    "${WHISPLAY_AGENT_ARGS[@]}" \
    "${TRANSLATE_ARGS[@]}"
else
  bashio::log.info "Using Vosk backend"
  bashio::log.info "Using Vosk model variant ${MODEL_VARIANT}"
  bashio::log.info "Using Vosk model at ${RESOLVED_MODEL_PATH}"
  exec python3 /app.py \
    --listen-host "${SERVER_HOST}" \
    --listen-port "${SERVER_PORT}" \
    --websocket-host "${WEBSOCKET_HOST}" \
    --websocket-port "${WEBSOCKET_PORT}" \
    --accepted-audio-codecs "${ACCEPTED_AUDIO_CODECS}" \
    --language "${LANGUAGE}" \
    --stt-backend "vosk" \
    --model-path "${RESOLVED_MODEL_PATH}" \
    "${TRANSLATE_ARGS[@]}"
fi
