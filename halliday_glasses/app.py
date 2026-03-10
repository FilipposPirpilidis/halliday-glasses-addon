import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from vosk import KaldiRecognizer, Model, SetLogLevel


LOGGER = logging.getLogger("halliday_glasses")
SetLogLevel(-1)


def event_bytes(event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> bytes:
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


def result_text(result_json: str) -> str:
    try:
        result = json.loads(result_json)
    except json.JSONDecodeError:
        return ""

    return (result.get("text") or result.get("partial") or "").strip()


@dataclass(slots=True)
class ServerConfig:
    listen_host: str
    listen_port: int
    language: str
    model_path: str


@dataclass(slots=True)
class AudioState:
    rate: int = 16000
    width: int = 2
    channels: int = 1
    language: str = "en"
    recognizer: Optional[KaldiRecognizer] = None
    last_partial_text: str = ""

    def reset(self) -> None:
        self.recognizer = None
        self.last_partial_text = ""


class HallidaySession:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cfg: ServerConfig,
        model: Model,
    ):
        self.reader = reader
        self.writer = writer
        self.cfg = cfg
        self.model = model
        self.state = AudioState(language=cfg.language)
        self._closed = False

    async def run(self) -> None:
        peer = self.writer.get_extra_info("peername")
        LOGGER.info("Client connected: %s", peer)
        try:
            while not self._closed:
                event, payload = await read_event(self.reader)
                await self.handle_event(event, payload)
        except EOFError:
            LOGGER.info("Client disconnected: %s", peer)
        except Exception:
            LOGGER.exception("Session failure for %s", peer)
        finally:
            self.writer.close()
            await self.writer.wait_closed()

    async def handle_event(self, event: dict[str, Any], payload: bytes) -> None:
        event_type = event.get("type")
        data = event.get("data") or {}

        if event_type == "describe":
            await self.send_event(
                "info",
                {
                    "asr": [
                        {
                            "name": "Halliday Glasses (Vosk)",
                            "description": "Local Vosk speech-to-text for Halliday Glasses",
                            "attribution": {"name": "Halliday Glasses Add-on"},
                            "installed": True,
                            "languages": [self.cfg.language],
                            "version": "0.2.0",
                        }
                    ]
                },
            )
            return

        if event_type == "transcribe":
            language = (data.get("language") or "").strip()
            if language:
                self.state.language = language
            return

        if event_type == "audio-start":
            self.state.reset()
            self.state.rate = int(data.get("rate") or 16000)
            self.state.width = int(data.get("width") or 2)
            self.state.channels = int(data.get("channels") or 1)

            if self.state.width != 2 or self.state.channels != 1:
                await self.send_event("error", {"message": "Vosk add-on expects PCM16 mono audio"})
                self._closed = True
                return

            self.state.recognizer = KaldiRecognizer(self.model, float(self.state.rate))
            self.state.recognizer.SetWords(True)
            return

        if event_type == "audio-chunk":
            await self.handle_audio_chunk(payload)
            return

        if event_type == "audio-stop":
            await self.flush_final()
            self.state.reset()
            return

        if event_type == "ping":
            await self.send_event("pong", data)
            return

    async def handle_audio_chunk(self, payload: bytes) -> None:
        recognizer = self.state.recognizer
        if recognizer is None or not payload:
            return

        if recognizer.AcceptWaveform(payload):
            text = result_text(recognizer.Result())
            if text:
                self.state.last_partial_text = ""
                await self.send_event("transcript", {"text": text})
            return

        text = result_text(recognizer.PartialResult())
        if text and text != self.state.last_partial_text:
            self.state.last_partial_text = text
            await self.send_event("transcript-chunk", {"text": text})

    async def flush_final(self) -> None:
        recognizer = self.state.recognizer
        if recognizer is None:
            return

        text = result_text(recognizer.FinalResult())
        if text:
            await self.send_event("transcript", {"text": text})

    async def send_event(self, event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> None:
        self.writer.write(event_bytes(event_type, data, payload))
        await self.writer.drain()


async def serve(cfg: ServerConfig) -> None:
    LOGGER.info("Loading Vosk model from %s", cfg.model_path)
    model = Model(cfg.model_path)
    LOGGER.info("Vosk model loaded")

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = HallidaySession(reader, writer, cfg, model)
        await session.run()

    server = await asyncio.start_server(on_connect, cfg.listen_host, cfg.listen_port)
    addresses = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    LOGGER.info("Halliday Glasses server listening on %s", addresses)
    async with server:
        await server.serve_forever()


def parse_args() -> ServerConfig:
    parser = argparse.ArgumentParser()
    parser.add_argument("--listen-host", default="0.0.0.0")
    parser.add_argument("--listen-port", type=int, default=10310)
    parser.add_argument("--language", default="en")
    parser.add_argument("--model-path", default="/models/vosk-model-small-en-us-0.15")
    args = parser.parse_args()

    return ServerConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        language=args.language,
        model_path=args.model_path,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
