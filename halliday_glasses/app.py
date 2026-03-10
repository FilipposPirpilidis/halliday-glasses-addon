import argparse
import asyncio
import json
import logging
import math
from dataclasses import dataclass, field
from typing import Any, Optional


LOGGER = logging.getLogger("halliday_glasses")


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


def pcm_duration_ms(pcm16: bytes, rate: int, width: int, channels: int) -> int:
    if not pcm16 or rate <= 0 or width <= 0 or channels <= 0:
        return 0

    bytes_per_frame = width * channels
    return int((len(pcm16) / bytes_per_frame) * 1000 / rate)


@dataclass(slots=True)
class ServerConfig:
    listen_host: str
    listen_port: int
    whisper_host: str
    whisper_port: int
    language: str
    partial_interval_ms: int
    min_partial_audio_ms: int
    silence_ms: int
    min_utterance_ms: int
    speech_threshold: int


@dataclass(slots=True)
class AudioState:
    rate: int = 16000
    width: int = 2
    channels: int = 1
    language: str = "en"
    chunks: list[bytes] = field(default_factory=list)
    utterance_chunks: list[bytes] = field(default_factory=list)
    partial_task: Optional[asyncio.Task] = None
    final_requested: bool = False
    last_partial_text: str = ""
    speech_active: bool = False
    silence_ms: int = 0

    def reset(self) -> None:
        self.chunks.clear()
        self.utterance_chunks.clear()
        self.final_requested = False
        self.last_partial_text = ""
        self.speech_active = False
        self.silence_ms = 0

    @property
    def pcm16(self) -> bytes:
        return b"".join(self.chunks)

    @property
    def duration_ms(self) -> int:
        return pcm_duration_ms(self.pcm16, self.rate, self.width, self.channels)

    @property
    def utterance_pcm16(self) -> bytes:
        return b"".join(self.utterance_chunks)

    @property
    def utterance_duration_ms(self) -> int:
        return pcm_duration_ms(self.utterance_pcm16, self.rate, self.width, self.channels)


def pcm_rms(payload: bytes, width: int) -> float:
    if width != 2 or not payload:
        return 0.0

    sample_count = len(payload) // 2
    if sample_count == 0:
        return 0.0

    samples = memoryview(payload).cast("h")
    total = 0.0
    for sample in samples:
        total += float(sample) * float(sample)

    return math.sqrt(total / sample_count)


class WhisperBridge:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg

    async def transcribe(self, pcm16: bytes, state: AudioState) -> str:
        if not pcm16:
            return ""

        LOGGER.info(
            "Opening upstream whisper connection to %s:%s",
            self.cfg.whisper_host,
            self.cfg.whisper_port,
        )
        try:
            reader, writer = await asyncio.open_connection(self.cfg.whisper_host, self.cfg.whisper_port)
        except Exception as err:
            raise RuntimeError(
                f"Unable to connect to upstream whisper at {self.cfg.whisper_host}:{self.cfg.whisper_port}: {err}"
            ) from err
        try:
            writer.write(event_bytes("transcribe", {"language": state.language}))
            writer.write(
                event_bytes(
                    "audio-start",
                    {"rate": state.rate, "width": state.width, "channels": state.channels},
                )
            )

            chunk_size = max(1024, state.rate // 4) * state.width * state.channels
            for start in range(0, len(pcm16), chunk_size):
                chunk = pcm16[start : start + chunk_size]
                writer.write(
                    event_bytes(
                        "audio-chunk",
                        {"rate": state.rate, "width": state.width, "channels": state.channels},
                        chunk,
                    )
                )

            writer.write(event_bytes("audio-stop", {}))
            await writer.drain()

            partials: list[str] = []
            while True:
                event, _payload = await read_event(reader)
                event_type = event.get("type")
                data = event.get("data") or {}
                text = (data.get("text") or "").strip()

                if event_type == "transcript":
                    return text
                if event_type == "transcript-chunk" and text:
                    partials.append(text)
                if event_type == "error":
                    raise RuntimeError(data.get("message") or "Upstream whisper error")
        finally:
            writer.close()
            await writer.wait_closed()

        return " ".join(partials).strip()


class HallidaySession:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cfg: ServerConfig,
        bridge: WhisperBridge,
    ):
        self.reader = reader
        self.writer = writer
        self.cfg = cfg
        self.bridge = bridge
        self.state = AudioState()
        self.state.language = cfg.language
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
            await self.stop_partial_task()
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
                            "name": "Halliday Glasses",
                            "description": "Live transcription bridge backed by an upstream Wyoming Whisper server",
                            "attribution": {"name": "Halliday Glasses Add-on"},
                            "installed": True,
                            "languages": [self.cfg.language],
                            "version": "0.1.0",
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
            await self.stop_partial_task()
            self.state.reset()
            self.state.rate = int(data.get("rate") or 16000)
            self.state.width = int(data.get("width") or 2)
            self.state.channels = int(data.get("channels") or 1)
            if not self.state.language:
                self.state.language = self.cfg.language
            self.state.partial_task = asyncio.create_task(self.partial_loop())
            return

        if event_type == "audio-chunk":
            if payload:
                self.state.chunks.append(payload)
                await self.handle_audio_chunk(payload)
            return

        if event_type == "audio-stop":
            self.state.final_requested = True
            await self.stop_partial_task()
            try:
                await self.flush_utterance()
            except Exception as err:
                LOGGER.exception("Final transcription failed")
                await self.send_event("error", {"message": str(err)})
            self.state.reset()
            return

        if event_type == "ping":
            await self.send_event("pong", data)
            return

        LOGGER.debug("Ignoring event type: %s", event_type)

    async def handle_audio_chunk(self, payload: bytes) -> None:
        chunk_ms = pcm_duration_ms(payload, self.state.rate, self.state.width, self.state.channels)
        if chunk_ms <= 0:
            return

        rms = pcm_rms(payload, self.state.width)
        is_speech = rms >= self.cfg.speech_threshold

        if is_speech:
            self.state.speech_active = True
            self.state.silence_ms = 0
            self.state.utterance_chunks.append(payload)
            return

        if not self.state.speech_active:
            return

        self.state.utterance_chunks.append(payload)
        self.state.silence_ms += chunk_ms
        if self.state.silence_ms >= self.cfg.silence_ms:
            await self.flush_utterance()

    async def partial_loop(self) -> None:
        interval = self.cfg.partial_interval_ms / 1000
        try:
            while not self.state.final_requested:
                await asyncio.sleep(interval)
                if not self.state.speech_active or self.state.utterance_duration_ms < self.cfg.min_partial_audio_ms:
                    continue

                text = await self.bridge.transcribe(self.state.utterance_pcm16, self.state)
                if text and text != self.state.last_partial_text:
                    self.state.last_partial_text = text
                    await self.send_event("transcript-chunk", {"text": text})
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Partial transcription failed")

    async def stop_partial_task(self) -> None:
        task = self.state.partial_task
        self.state.partial_task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def send_event(self, event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> None:
        self.writer.write(event_bytes(event_type, data, payload))
        await self.writer.drain()

    async def flush_utterance(self) -> None:
        utterance_pcm16 = self.state.utterance_pcm16
        utterance_duration_ms = self.state.utterance_duration_ms
        self.state.utterance_chunks.clear()
        self.state.speech_active = False
        self.state.silence_ms = 0
        self.state.last_partial_text = ""

        if utterance_duration_ms < self.cfg.min_utterance_ms or not utterance_pcm16:
            return

        text = await self.bridge.transcribe(utterance_pcm16, self.state)
        if text:
            await self.send_event("transcript", {"text": text})


async def verify_upstream(cfg: ServerConfig) -> None:
    try:
        reader, writer = await asyncio.open_connection(cfg.whisper_host, cfg.whisper_port)
    except Exception:
        LOGGER.exception(
            "Startup connectivity check failed for upstream whisper at %s:%s",
            cfg.whisper_host,
            cfg.whisper_port,
        )
        return

    LOGGER.info("Startup connectivity check succeeded for upstream whisper at %s:%s", cfg.whisper_host, cfg.whisper_port)
    writer.close()
    await writer.wait_closed()


async def serve(cfg: ServerConfig) -> None:
    bridge = WhisperBridge(cfg)
    await verify_upstream(cfg)

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = HallidaySession(reader, writer, cfg, bridge)
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
    parser.add_argument("--whisper-host", required=True)
    parser.add_argument("--whisper-port", type=int, default=10300)
    parser.add_argument("--language", default="en")
    parser.add_argument("--partial-interval-ms", type=int, default=1500)
    parser.add_argument("--min-partial-audio-ms", type=int, default=1200)
    parser.add_argument("--silence-ms", type=int, default=900)
    parser.add_argument("--min-utterance-ms", type=int, default=400)
    parser.add_argument("--speech-threshold", type=int, default=900)
    args = parser.parse_args()

    return ServerConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        whisper_host=args.whisper_host,
        whisper_port=args.whisper_port,
        language=args.language,
        partial_interval_ms=args.partial_interval_ms,
        min_partial_audio_ms=args.min_partial_audio_ms,
        silence_ms=args.silence_ms,
        min_utterance_ms=args.min_utterance_ms,
        speech_threshold=args.speech_threshold,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
