import argparse
import asyncio
import audioop
import base64
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote
import websockets
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
    enable_openai_realtime: bool
    openai_api_key: str
    openai_realtime_model: str
    openai_transcription_model: str
    openai_prompt: str
    openai_vad_threshold: float
    openai_vad_prefix_padding_ms: int
    openai_vad_silence_duration_ms: int


@dataclass(slots=True)
class AudioState:
    rate: int = 16000
    width: int = 2
    channels: int = 1
    language: str = "en"

    def reset(self) -> None:
        return


class VoskBackend:
    def __init__(self, cfg: ServerConfig, model: Model, send_event):
        self.cfg = cfg
        self.model = model
        self.send_event = send_event
        self.recognizer: Optional[KaldiRecognizer] = None
        self.last_partial_text = ""

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("Vosk backend expects PCM16 mono audio")

        self.last_partial_text = ""
        self.recognizer = KaldiRecognizer(self.model, float(state.rate))
        self.recognizer.SetWords(True)

    async def process_chunk(self, payload: bytes, _state: AudioState) -> None:
        recognizer = self.recognizer
        if recognizer is None or not payload:
            return

        if recognizer.AcceptWaveform(payload):
            text = result_text(recognizer.Result())
            if text:
                self.last_partial_text = ""
                await self.send_event("transcript", {"text": text})
            return

        text = result_text(recognizer.PartialResult())
        if text and text != self.last_partial_text:
            self.last_partial_text = text
            await self.send_event("transcript-chunk", {"text": text})

    async def finish(self) -> None:
        recognizer = self.recognizer
        if recognizer is None:
            return

        text = result_text(recognizer.FinalResult())
        if text:
            await self.send_event("transcript", {"text": text})
        self.recognizer = None

    async def close(self) -> None:
        self.recognizer = None


class OpenAIRealtimeBackend:
    def __init__(self, cfg: ServerConfig, send_event):
        self.cfg = cfg
        self.send_event = send_event
        self.websocket = None
        self.receive_task: Optional[asyncio.Task] = None
        self.partial_by_item: dict[str, str] = {}
        self.resample_state = None

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("OpenAI Realtime backend expects PCM16 mono audio")
        if not self.cfg.openai_api_key:
            raise RuntimeError("OpenAI Realtime backend enabled but openai_api_key is empty")

        realtime_model = quote(self.cfg.openai_realtime_model, safe="")
        uri = f"wss://api.openai.com/v1/realtime?model={realtime_model}"
        headers = {"Authorization": f"Bearer {self.cfg.openai_api_key}"}
        self.websocket = await websockets.connect(uri, extra_headers=headers, max_size=None)
        self.partial_by_item.clear()
        self.resample_state = None
        self.receive_task = asyncio.create_task(self.receive_loop())

        session_update = {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "audio": {
                    "input": {
                        "format": {
                            "type": "audio/pcm",
                            "rate": 24000,
                        },
                        "noise_reduction": {
                            "type": "near_field",
                        },
                        "transcription": {
                            "model": self.cfg.openai_transcription_model,
                            "prompt": self.cfg.openai_prompt,
                            "language": state.language,
                        },
                        "turn_detection": {
                            "type": "server_vad",
                            "threshold": self.cfg.openai_vad_threshold,
                            "prefix_padding_ms": self.cfg.openai_vad_prefix_padding_ms,
                            "silence_duration_ms": self.cfg.openai_vad_silence_duration_ms,
                        },
                    }
                },
                "include": ["item.input_audio_transcription.logprobs"],
            },
        }
        await self.websocket.send(json.dumps(session_update))

    async def process_chunk(self, payload: bytes, state: AudioState) -> None:
        if self.websocket is None or not payload:
            return

        pcm24 = self.resample_to_24k(payload, state)
        if not pcm24:
            return

        event = {
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(pcm24).decode("ascii"),
        }
        await self.websocket.send(json.dumps(event))

    async def finish(self) -> None:
        if self.websocket is None:
            return

        # Inference from the Realtime event naming: force commit of any buffered tail audio on shutdown.
        await self.websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
        await asyncio.sleep(0.5)

    async def close(self) -> None:
        if self.receive_task is not None:
            self.receive_task.cancel()
            try:
                await self.receive_task
            except asyncio.CancelledError:
                pass
            self.receive_task = None

        if self.websocket is not None:
            await self.websocket.close()
            self.websocket = None

        self.partial_by_item.clear()
        self.resample_state = None

    async def receive_loop(self) -> None:
        websocket = self.websocket
        if websocket is None:
            return

        try:
            async for message in websocket:
                event = json.loads(message)
                event_type = event.get("type", "")
                if event_type == "conversation.item.input_audio_transcription.delta":
                    item_id = event.get("item_id", "")
                    delta = (event.get("delta") or "").strip()
                    if not item_id or not delta:
                        continue
                    updated = f"{self.partial_by_item.get(item_id, '')}{delta}"
                    self.partial_by_item[item_id] = updated
                    await self.send_event("transcript-chunk", {"text": updated})
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    item_id = event.get("item_id", "")
                    transcript = (event.get("transcript") or "").strip()
                    if item_id:
                        self.partial_by_item.pop(item_id, None)
                    if transcript:
                        await self.send_event("transcript", {"text": transcript})
                elif event_type == "error":
                    message_text = (
                        (event.get("error") or {}).get("message")
                        or event.get("message")
                        or "OpenAI Realtime transcription error"
                    )
                    await self.send_event("error", {"message": message_text})
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.exception("OpenAI Realtime receive loop failed")
            await self.send_event("error", {"message": str(err)})

    def resample_to_24k(self, payload: bytes, state: AudioState) -> bytes:
        if state.rate == 24000:
            return payload

        converted, self.resample_state = audioop.ratecv(
            payload,
            state.width,
            state.channels,
            state.rate,
            24000,
            self.resample_state,
        )
        return converted


class HallidaySession:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        cfg: ServerConfig,
        vosk_model: Optional[Model],
    ):
        self.reader = reader
        self.writer = writer
        self.cfg = cfg
        self.vosk_model = vosk_model
        self.state = AudioState(language=cfg.language)
        self._closed = False
        self.backend = None

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
            await self.close_backend()
            self.writer.close()
            await self.writer.wait_closed()

    async def handle_event(self, event: dict[str, Any], payload: bytes) -> None:
        event_type = event.get("type")
        data = event.get("data") or {}

        if event_type == "describe":
            backend_name = "OpenAI Realtime" if self.cfg.enable_openai_realtime else "Vosk"
            await self.send_event(
                "info",
                {
                    "asr": [
                        {
                            "name": f"Halliday Glasses ({backend_name})",
                            "description": "Local Wyoming speech-to-text endpoint for Halliday Glasses",
                            "attribution": {"name": "Halliday Glasses Add-on"},
                            "installed": True,
                            "languages": [self.cfg.language],
                            "version": "0.3.0",
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
            await self.close_backend()
            self.state.reset()
            self.state.rate = int(data.get("rate") or 16000)
            self.state.width = int(data.get("width") or 2)
            self.state.channels = int(data.get("channels") or 1)
            self.backend = self.build_backend()
            await self.backend.start(self.state)
            return

        if event_type == "audio-chunk":
            if self.backend is not None:
                await self.backend.process_chunk(payload, self.state)
            return

        if event_type == "audio-stop":
            if self.backend is not None:
                await self.backend.finish()
                await self.close_backend()
            self.state.reset()
            return

        if event_type == "ping":
            await self.send_event("pong", data)
            return

    def build_backend(self):
        if self.cfg.enable_openai_realtime:
            return OpenAIRealtimeBackend(self.cfg, self.send_event)
        if self.vosk_model is None:
            raise RuntimeError("Vosk backend selected but no model is loaded")
        return VoskBackend(self.cfg, self.vosk_model, self.send_event)

    async def close_backend(self) -> None:
        if self.backend is not None:
            await self.backend.close()
            self.backend = None

    async def send_event(self, event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> None:
        self.writer.write(event_bytes(event_type, data, payload))
        await self.writer.drain()


async def serve(cfg: ServerConfig) -> None:
    vosk_model = None
    if not cfg.enable_openai_realtime:
        LOGGER.info("Loading Vosk model from %s", cfg.model_path)
        vosk_model = Model(cfg.model_path)
        LOGGER.info("Vosk model loaded")
    if cfg.enable_openai_realtime:
        LOGGER.info(
            "OpenAI Realtime backend enabled with session model %s and transcription model %s",
            cfg.openai_realtime_model,
            cfg.openai_transcription_model,
        )

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = HallidaySession(reader, writer, cfg, vosk_model)
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
    parser.add_argument("--enable-openai-realtime", action="store_true")
    parser.add_argument("--openai-api-key", default="")
    parser.add_argument("--openai-realtime-model", default="gpt-realtime-mini")
    parser.add_argument("--openai-transcription-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--openai-prompt", default="")
    parser.add_argument("--openai-vad-threshold", type=float, default=0.5)
    parser.add_argument("--openai-vad-prefix-padding-ms", type=int, default=300)
    parser.add_argument("--openai-vad-silence-duration-ms", type=int, default=500)
    args = parser.parse_args()

    return ServerConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        language=args.language,
        model_path=args.model_path,
        enable_openai_realtime=args.enable_openai_realtime,
        openai_api_key=args.openai_api_key,
        openai_realtime_model=args.openai_realtime_model,
        openai_transcription_model=args.openai_transcription_model,
        openai_prompt=args.openai_prompt,
        openai_vad_threshold=args.openai_vad_threshold,
        openai_vad_prefix_padding_ms=args.openai_vad_prefix_padding_ms,
        openai_vad_silence_duration_ms=args.openai_vad_silence_duration_ms,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
