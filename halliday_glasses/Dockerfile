ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.19
FROM ${BUILD_FROM}

ENV PYTHONUNBUFFERED=1

RUN apk add --no-cache python3

COPY app.py /app.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
