ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.19
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1
ENV VOSK_MODEL_URL=https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
ENV VOSK_MODEL_DIR=/models/vosk-model-small-en-us-0.15

RUN apk add --no-cache curl unzip python3 py3-pip \
    && pip3 install --no-cache-dir vosk==0.3.45 \
    && mkdir -p /models \
    && curl -L "${VOSK_MODEL_URL}" -o /tmp/vosk-model.zip \
    && unzip -q /tmp/vosk-model.zip -d /models \
    && rm -f /tmp/vosk-model.zip

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
