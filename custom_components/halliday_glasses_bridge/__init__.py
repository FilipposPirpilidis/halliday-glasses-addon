from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.components.websocket_api import ActiveConnection
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ADDON_HOST,
    CONF_ADDON_PORT,
    DATA_COMMANDS_REGISTERED,
    DATA_CONFIG,
    DATA_SESSIONS,
    DEFAULT_ADDON_HOST,
    DEFAULT_ADDON_PORT,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            {
                vol.Optional(CONF_ADDON_HOST, default=DEFAULT_ADDON_HOST): cv.string,
                vol.Optional(CONF_ADDON_PORT, default=DEFAULT_ADDON_PORT): cv.port,
            }
        )
    },
    extra=vol.ALLOW_EXTRA,
)


def event_bytes(event_type: str, data: dict[str, Any] | None = None, payload: bytes = b"") -> bytes:
    header: dict[str, Any] = {"type": event_type}
    if data:
        header["data"] = data
    if payload:
        header["payload_length"] = len(payload)
    return (json.dumps(header, separators=(",", ":")) + "\n").encode("utf-8") + payload


async def read_event(reader: asyncio.StreamReader) -> tuple[dict[str, Any], bytes]:
    line = await reader.readline()
    if not line:
        raise EOFError("Connection closed while reading event header")

    event = json.loads(line.decode("utf-8"))
    data = event.get("data") or {}

    data_length = int(event.get("data_length") or 0)
    if data_length:
        extra = await reader.readexactly(data_length)
        extra_obj = json.loads(extra.decode("utf-8"))
        if isinstance(extra_obj, dict):
            data.update(extra_obj)

    payload_length = int(event.get("payload_length") or 0)
    payload = b""
    if payload_length:
        payload = await reader.readexactly(payload_length)

    event["data"] = data
    return event, payload


@dataclass(slots=True)
class UpstreamConfig:
    host: str
    port: int


class HallidayBridgeSession:
    def __init__(self, hass: HomeAssistant, connection: ActiveConnection, subscription_id: int, upstream: UpstreamConfig):
        self.hass = hass
        self.connection = connection
        self.subscription_id = subscription_id
        self.upstream = upstream
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.read_task: asyncio.Task | None = None
        self.closed = False

    async def connect(self, language: str, codec: str, rate: int, width: int, channels: int) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.upstream.host, self.upstream.port)
        await self.send("transcribe", {"language": language})
        await self.send("audio-start", {"codec": codec, "rate": rate, "width": width, "channels": channels})
        self.read_task = self.hass.loop.create_task(self.read_loop())

    async def read_loop(self) -> None:
        assert self.reader is not None
        try:
            while not self.closed:
                event, _payload = await read_event(self.reader)
                await self.forward_event(event)
        except EOFError:
            LOGGER.info("Halliday upstream session closed")
        except Exception as err:
            LOGGER.exception("Halliday upstream read loop failed")
            self.send_stream_event({"type": "error", "message": str(err)})
        finally:
            await self.close()

    async def forward_event(self, event: dict[str, Any]) -> None:
        event_type = event.get("type") or ""
        data = event.get("data") or {}
        if event_type == "transcript-chunk":
            self.send_stream_event({"type": "transcript_chunk", **data})
        elif event_type == "transcript":
            self.send_stream_event({"type": "transcript", **data})
        elif event_type == "translate-config":
            self.send_stream_event({"type": "translate_config", **data})
        elif event_type == "info":
            self.send_stream_event({"type": "info", **data})
        elif event_type == "error":
            self.send_stream_event({"type": "error", **data})
        elif event_type == "pong":
            self.send_stream_event({"type": "pong", **data})

    async def send(self, event_type: str, data: dict[str, Any] | None = None, payload: bytes = b"") -> None:
        if self.closed or self.writer is None:
            return
        self.writer.write(event_bytes(event_type, data, payload))
        await self.writer.drain()

    async def send_audio_chunk(self, audio_b64: str, rate: int, width: int, channels: int) -> None:
        payload = base64.b64decode(audio_b64)
        await self.send("audio-chunk", {"rate": rate, "width": width, "channels": channels}, payload)

    def send_stream_event(self, event: dict[str, Any]) -> None:
        self.connection.send_message(websocket_api.event_message(self.subscription_id, event))

    async def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self.read_task is not None:
            self.read_task.cancel()
            self.read_task = None
        if self.writer is not None:
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
        self.reader = None


def get_upstream_config(hass: HomeAssistant) -> UpstreamConfig:
    cfg = hass.data[DOMAIN][DATA_CONFIG]
    return UpstreamConfig(host=cfg[CONF_ADDON_HOST], port=cfg[CONF_ADDON_PORT])


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    domain_config = config.get(DOMAIN, {})
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault(
        DATA_CONFIG,
        {
            CONF_ADDON_HOST: domain_config.get(CONF_ADDON_HOST, DEFAULT_ADDON_HOST),
            CONF_ADDON_PORT: domain_config.get(CONF_ADDON_PORT, DEFAULT_ADDON_PORT),
        },
    )
    hass.data[DOMAIN].setdefault(DATA_SESSIONS, {})
    _register_commands(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][DATA_CONFIG] = {
        CONF_ADDON_HOST: entry.data.get(CONF_ADDON_HOST, DEFAULT_ADDON_HOST),
        CONF_ADDON_PORT: entry.data.get(CONF_ADDON_PORT, DEFAULT_ADDON_PORT),
    }
    hass.data[DOMAIN].setdefault(DATA_SESSIONS, {})
    _register_commands(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    sessions: dict[str, HallidayBridgeSession] = hass.data.get(DOMAIN, {}).get(DATA_SESSIONS, {})
    for session in list(sessions.values()):
        await session.close()
    sessions.clear()
    if DOMAIN in hass.data:
        hass.data[DOMAIN][DATA_CONFIG] = {
            CONF_ADDON_HOST: DEFAULT_ADDON_HOST,
            CONF_ADDON_PORT: DEFAULT_ADDON_PORT,
        }
    return True


def _register_commands(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get(DATA_COMMANDS_REGISTERED):
        return
    websocket_api.async_register_command(hass, websocket_open_stream)
    websocket_api.async_register_command(hass, websocket_audio_chunk)
    websocket_api.async_register_command(hass, websocket_close_stream)
    websocket_api.async_register_command(hass, websocket_translate_get)
    websocket_api.async_register_command(hass, websocket_translate_set)
    hass.data[DOMAIN][DATA_COMMANDS_REGISTERED] = True


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/open_stream",
        vol.Optional("language", default="en"): str,
        vol.Optional("codec", default="pcm16"): str,
        vol.Optional("rate", default=16000): int,
        vol.Optional("width", default=2): int,
        vol.Optional("channels", default=1): int,
        vol.Optional("translate_source"): str,
        vol.Optional("translate_target"): str,
    }
)
@websocket_api.async_response
async def websocket_open_stream(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    upstream = get_upstream_config(hass)
    session = HallidayBridgeSession(hass, connection, msg["id"], upstream)
    await session.connect(msg["language"], msg.get("codec", "pcm16"), msg["rate"], msg["width"], msg["channels"])
    sessions: dict[str, HallidayBridgeSession] = hass.data[DOMAIN][DATA_SESSIONS]
    session_id = f"{id(connection)}:{msg['id']}"
    sessions[session_id] = session

    async def cleanup() -> None:
        sessions.pop(session_id, None)
        await session.close()

    def unsubscribe() -> None:
        hass.async_create_task(cleanup())

    connection.subscriptions[msg["id"]] = unsubscribe

    if "translate_source" in msg or "translate_target" in msg:
        await session.send(
            "translate-set",
            {
                "source": msg.get("translate_source", ""),
                "target": msg.get("translate_target", ""),
            },
        )
    await session.send("translate-get", {})
    connection.send_result(msg["id"], {"session_id": session_id})


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/audio_chunk",
        vol.Required("session_id"): str,
        vol.Required("audio"): str,
        vol.Optional("rate", default=16000): int,
        vol.Optional("width", default=2): int,
        vol.Optional("channels", default=1): int,
    }
)
@websocket_api.async_response
async def websocket_audio_chunk(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    session: HallidayBridgeSession | None = hass.data[DOMAIN][DATA_SESSIONS].get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Unknown session_id")
        return
    await session.send_audio_chunk(msg["audio"], msg["rate"], msg["width"], msg["channels"])
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/close_stream",
        vol.Required("session_id"): str,
    }
)
@websocket_api.async_response
async def websocket_close_stream(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    sessions: dict[str, HallidayBridgeSession] = hass.data[DOMAIN][DATA_SESSIONS]
    session = sessions.pop(msg["session_id"], None)
    if session is None:
        connection.send_error(msg["id"], "not_found", "Unknown session_id")
        return
    await session.send("audio-stop", {})
    await session.close()
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/translate_get",
        vol.Required("session_id"): str,
    }
)
@websocket_api.async_response
async def websocket_translate_get(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    session: HallidayBridgeSession | None = hass.data[DOMAIN][DATA_SESSIONS].get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Unknown session_id")
        return
    await session.send("translate-get", {})
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("id"): int,
        vol.Required("type"): f"{DOMAIN}/translate_set",
        vol.Required("session_id"): str,
        vol.Optional("source"): str,
        vol.Optional("target"): str,
        vol.Optional("pair"): str,
    }
)
@websocket_api.async_response
async def websocket_translate_set(hass: HomeAssistant, connection: ActiveConnection, msg: dict[str, Any]) -> None:
    session: HallidayBridgeSession | None = hass.data[DOMAIN][DATA_SESSIONS].get(msg["session_id"])
    if session is None:
        connection.send_error(msg["id"], "not_found", "Unknown session_id")
        return
    payload: dict[str, Any] = {}
    for key in ("source", "target", "pair"):
        if key in msg:
            payload[key] = msg[key]
    await session.send("translate-set", payload)
    connection.send_result(msg["id"])
