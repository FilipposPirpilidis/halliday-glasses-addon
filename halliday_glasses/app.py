import argparse
import asyncio
import audioop
import base64
import json
import logging
import struct
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import quote
from urllib.request import Request, urlopen
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
    stt_backend: str
    openai_api_key: str
    openai_realtime_model: str
    openai_transcription_model: str
    openai_prompt: str
    openai_vad_threshold: float
    openai_vad_prefix_padding_ms: int
    openai_vad_silence_duration_ms: int
    whisplay_recognize_url: str
    whisplay_timeout_seconds: float
    whisplay_partial_window_seconds: float
    whisplay_partial_inference_seconds: float
    whisplay_auto_final_silence_ms: int
    whisplay_auto_final_min_seconds: float
    whisplay_auto_final_silence_level: int


@dataclass(slots=True)
class AudioState:
    rate: int = 16000
    width: int = 2
    channels: int = 1
    language: str = "en"
    chunks: bytearray | None = None

    def reset(self) -> None:
        self.chunks = bytearray()


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
            raise RuntimeError("OpenAI backend selected but openai_api_key is empty")

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


class WhisplayBackend:
    def __init__(self, cfg: ServerConfig, send_event):
        self.cfg = cfg
        self.send_event = send_event
        self.raw_pcm = bytearray()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("Whisplay backend expects PCM16 mono audio")
        self.raw_pcm.clear()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0

    async def process_chunk(self, payload: bytes, state: AudioState) -> None:
        if not payload:
            return
        self.raw_pcm.extend(payload)
        self.pending_audio_bytes += len(payload)
        if is_pcm_chunk_silent(payload, self.cfg.whisplay_auto_final_silence_level):
            self.consecutive_silence_bytes += len(payload)
        else:
            self.consecutive_silence_bytes = 0

        partial = await self.maybe_partial(state)
        if partial:
            await self.send_event("transcript-chunk", {"text": partial})

        if self.should_auto_finalize(state):
            final_text = await self.finalize(state)
            if final_text:
                await self.send_event("transcript", {"text": final_text})
            self.reset_stream_state()

    async def finish(self) -> None:
        return

    async def close(self) -> None:
        self.reset_stream_state()

    async def maybe_partial(self, state: AudioState) -> str:
        now = asyncio.get_running_loop().time()
        if now < self.partial_retry_not_before:
            return ""

        min_bytes_for_partial = max(
            int(float(state.rate) * 2.0 * self.cfg.whisplay_partial_window_seconds),
            state.rate,
        )
        pending_bytes = len(self.raw_pcm) - self.bytes_transcribed_for_partial
        if pending_bytes < min_bytes_for_partial:
            return ""

        bytes_per_second = state.rate * 2
        partial_bytes = int(float(bytes_per_second) * self.cfg.whisplay_partial_inference_seconds)
        clipped_pcm = bytes(self.raw_pcm[-max(partial_bytes, bytes_per_second):])

        try:
            transcript = await self.transcribe_pcm(clipped_pcm, state.rate)
            filtered = transcript.strip()
            previous = self.last_partial_text.strip()
            self.last_partial_text = filtered
            self.bytes_transcribed_for_partial = len(self.raw_pcm)
            self.partial_retry_not_before = 0.0
            if filtered and filtered != previous:
                return filtered
            return ""
        except RuntimeError as err:
            if "busy" in str(err).lower():
                self.partial_retry_not_before = now + 0.75
                return ""
            raise

    async def finalize(self, state: AudioState) -> str:
        if not self.raw_pcm:
            return ""
        try:
            transcript = await self.transcribe_pcm(bytes(self.raw_pcm), state.rate)
            normalized = transcript.strip()
            return normalized or self.last_partial_text.strip()
        except RuntimeError as err:
            if "busy" in str(err).lower():
                return self.last_partial_text.strip()
            raise

    async def transcribe_pcm(self, pcm: bytes, sample_rate: int) -> str:
        wav = encode_wav_pcm16_mono(pcm, sample_rate)
        payload = json.dumps({"base64": base64.b64encode(wav).decode("ascii")}).encode("utf-8")
        request = Request(
            self.cfg.whisplay_recognize_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _request() -> tuple[int, str]:
            with urlopen(request, timeout=max(self.cfg.whisplay_timeout_seconds, 10.0)) as response:
                status = getattr(response, "status", response.getcode())
                body = response.read().decode("utf-8")
                return status, body

        try:
            status, body = await asyncio.to_thread(_request)
        except Exception as err:
            raise RuntimeError(f"Whisplay request failed: {err}") from err

        if not (200 <= status <= 299):
            raise RuntimeError(f"Whisplay request failed (status {status}): {body}")

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"Whisplay response is malformed: {body}") from err

        error = (decoded.get("error") or "").strip()
        if error:
            raise RuntimeError(error)

        return (decoded.get("recognition") or "").strip()

    def should_auto_finalize(self, state: AudioState) -> bool:
        bytes_per_second = state.rate * 2
        min_bytes = max(int(float(bytes_per_second) * self.cfg.whisplay_auto_final_min_seconds), state.rate)
        silence_bytes = int(float(bytes_per_second) * float(self.cfg.whisplay_auto_final_silence_ms) / 1000.0)
        return (
            self.pending_audio_bytes >= min_bytes
            and self.consecutive_silence_bytes >= max(silence_bytes, state.rate // 2)
        )

    def reset_stream_state(self) -> None:
        self.raw_pcm.clear()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0


def encode_wav_pcm16_mono(pcm: bytes, sample_rate: int) -> bytes:
    channels = 1
    bits_per_sample = 16
    bytes_per_sample = bits_per_sample // 8
    byte_rate = sample_rate * channels * bytes_per_sample
    block_align = channels * bytes_per_sample
    data_size = len(pcm)
    riff_chunk_size = 36 + data_size

    header = b"".join(
        [
            b"RIFF",
            struct.pack("<I", riff_chunk_size),
            b"WAVE",
            b"fmt ",
            struct.pack("<IHHIIHH", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample),
            b"data",
            struct.pack("<I", data_size),
        ]
    )
    return header + pcm


def is_pcm_chunk_silent(chunk: bytes, threshold: int) -> bool:
    if len(chunk) < 2:
        return True

    peak = 0
    for index in range(0, len(chunk) - 1, 2):
        sample = int.from_bytes(chunk[index : index + 2], byteorder="little", signed=True)
        abs_sample = 32767 if sample == -32768 else abs(sample)
        if abs_sample > peak:
            peak = abs_sample
            if peak > threshold:
                return False
    return True


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
            backend_name = {
                "vosk": "Vosk",
                "openai": "OpenAI Realtime",
                "whisplay": "Whisplay",
            }.get(self.cfg.stt_backend, self.cfg.stt_backend)
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
        if self.cfg.stt_backend == "openai":
            return OpenAIRealtimeBackend(self.cfg, self.send_event)
        if self.cfg.stt_backend == "whisplay":
            return WhisplayBackend(self.cfg, self.send_event)
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
    if cfg.stt_backend == "vosk":
        LOGGER.info("Loading Vosk model from %s", cfg.model_path)
        vosk_model = Model(cfg.model_path)
        LOGGER.info("Vosk model loaded")
    if cfg.stt_backend == "openai":
        LOGGER.info(
            "OpenAI Realtime backend enabled with session model %s and transcription model %s",
            cfg.openai_realtime_model,
            cfg.openai_transcription_model,
        )
    if cfg.stt_backend == "whisplay":
        LOGGER.info("Whisplay backend enabled with recognize URL %s", cfg.whisplay_recognize_url)

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
    parser.add_argument("--stt-backend", default="vosk")
    parser.add_argument("--openai-api-key", default="")
    parser.add_argument("--openai-realtime-model", default="gpt-realtime-mini")
    parser.add_argument("--openai-transcription-model", default="gpt-4o-mini-transcribe")
    parser.add_argument("--openai-prompt", default="")
    parser.add_argument("--openai-vad-threshold", type=float, default=0.5)
    parser.add_argument("--openai-vad-prefix-padding-ms", type=int, default=300)
    parser.add_argument("--openai-vad-silence-duration-ms", type=int, default=500)
    parser.add_argument("--whisplay-recognize-url", default="http://192.168.2.29:8801/recognize")
    parser.add_argument("--whisplay-timeout-seconds", type=float, default=60.0)
    parser.add_argument("--whisplay-partial-window-seconds", type=float, default=2.0)
    parser.add_argument("--whisplay-partial-inference-seconds", type=float, default=4.0)
    parser.add_argument("--whisplay-auto-final-silence-ms", type=int, default=900)
    parser.add_argument("--whisplay-auto-final-min-seconds", type=float, default=0.8)
    parser.add_argument("--whisplay-auto-final-silence-level", type=int, default=700)
    args = parser.parse_args()

    return ServerConfig(
        listen_host=args.listen_host,
        listen_port=args.listen_port,
        language=args.language,
        model_path=args.model_path,
        stt_backend=args.stt_backend,
        openai_api_key=args.openai_api_key,
        openai_realtime_model=args.openai_realtime_model,
        openai_transcription_model=args.openai_transcription_model,
        openai_prompt=args.openai_prompt,
        openai_vad_threshold=args.openai_vad_threshold,
        openai_vad_prefix_padding_ms=args.openai_vad_prefix_padding_ms,
        openai_vad_silence_duration_ms=args.openai_vad_silence_duration_ms,
        whisplay_recognize_url=args.whisplay_recognize_url,
        whisplay_timeout_seconds=args.whisplay_timeout_seconds,
        whisplay_partial_window_seconds=args.whisplay_partial_window_seconds,
        whisplay_partial_inference_seconds=args.whisplay_partial_inference_seconds,
        whisplay_auto_final_silence_ms=args.whisplay_auto_final_silence_ms,
        whisplay_auto_final_min_seconds=args.whisplay_auto_final_min_seconds,
        whisplay_auto_final_silence_level=args.whisplay_auto_final_silence_level,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
