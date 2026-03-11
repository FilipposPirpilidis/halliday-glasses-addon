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
from urllib.error import HTTPError, URLError
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
    whisplaybot_recognize_url: str
    whisplaybot_timeout_seconds: float
    whisplaybot_partial_window_seconds: float
    whisplaybot_partial_inference_seconds: float
    whisplaybot_auto_final_silence_ms: int
    whisplaybot_auto_final_min_seconds: float
    whisplaybot_auto_final_silence_level: int
    translate_enabled: bool
    translate_url: str
    translate_pairs: tuple[str, ...]
    translate_source: str
    translate_target: str
    translate_timeout_seconds: float


@dataclass(slots=True)
class AudioState:
    rate: int = 16000
    width: int = 2
    channels: int = 1
    language: str = "en"
    chunks: bytearray | None = None
    translate_enabled: bool = False
    translate_pairs: tuple[str, ...] = ()
    translate_source: str = "auto"
    translate_target: str = ""

    def reset(self) -> None:
        self.chunks = bytearray()


class Translator:
    def __init__(self, cfg: ServerConfig):
        self.cfg = cfg

    async def translate(self, text: str, source: str, target: str) -> str:
        payload = json.dumps(
            {
                "q": text,
                "source": source,
                "target": target,
                "format": "text",
            }
        ).encode("utf-8")
        request = Request(
            self.cfg.translate_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _request() -> str:
            with urlopen(request, timeout=max(self.cfg.translate_timeout_seconds, 5.0)) as response:
                body = response.read().decode("utf-8")
                decoded = json.loads(body)
                translated = (decoded.get("translatedText") or "").strip()
                if not translated:
                    raise RuntimeError(f"Translation response missing translatedText: {body}")
                return translated

        try:
            return await asyncio.to_thread(_request)
        except HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LibreTranslate HTTP {err.code}: {body}") from err
        except URLError as err:
            raise RuntimeError(f"LibreTranslate request failed: {err}") from err


class VoskBackend:
    def __init__(self, cfg: ServerConfig, model: Model, emit_partial, emit_final):
        self.cfg = cfg
        self.model = model
        self.emit_partial = emit_partial
        self.emit_final = emit_final
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
                await self.emit_final(text)
            return

        text = result_text(recognizer.PartialResult())
        if text and text != self.last_partial_text:
            self.last_partial_text = text
            await self.emit_partial(text)

    async def finish(self) -> None:
        recognizer = self.recognizer
        if recognizer is None:
            return

        text = result_text(recognizer.FinalResult())
        if text:
            await self.emit_final(text)
        self.recognizer = None

    async def close(self) -> None:
        self.recognizer = None


class OpenAIRealtimeBackend:
    def __init__(self, cfg: ServerConfig, emit_partial, emit_final, emit_error):
        self.cfg = cfg
        self.emit_partial = emit_partial
        self.emit_final = emit_final
        self.emit_error = emit_error
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
                    await self.emit_partial(updated)
                elif event_type == "conversation.item.input_audio_transcription.completed":
                    item_id = event.get("item_id", "")
                    transcript = (event.get("transcript") or "").strip()
                    if item_id:
                        self.partial_by_item.pop(item_id, None)
                    if transcript:
                        await self.emit_final(transcript)
                elif event_type == "error":
                    message_text = (
                        (event.get("error") or {}).get("message")
                        or event.get("message")
                        or "OpenAI Realtime transcription error"
                    )
                    await self.emit_error(message_text)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.exception("OpenAI Realtime receive loop failed")
            await self.emit_error(str(err))

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
    def __init__(self, cfg: ServerConfig, emit_partial, emit_final):
        self.cfg = cfg
        self.emit_partial = emit_partial
        self.emit_final = emit_final
        self.raw_pcm = bytearray()
        self.bytes_transcribed_for_partial = 0
        self.last_partial_text = ""
        self.partial_retry_not_before = 0.0
        self.consecutive_silence_bytes = 0
        self.pending_audio_bytes = 0

    async def start(self, state: AudioState) -> None:
        if state.width != 2 or state.channels != 1:
            raise RuntimeError("WhisplayBot backend expects PCM16 mono audio")
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
        if is_pcm_chunk_silent(payload, self.cfg.whisplaybot_auto_final_silence_level):
            self.consecutive_silence_bytes += len(payload)
        else:
            self.consecutive_silence_bytes = 0

        partial = await self.maybe_partial(state)
        if partial:
            await self.emit_partial(partial)

        if self.should_auto_finalize(state):
            final_text = await self.finalize(state)
            if final_text:
                await self.emit_final(final_text)
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
            int(float(state.rate) * 2.0 * self.cfg.whisplaybot_partial_window_seconds),
            state.rate,
        )
        pending_bytes = len(self.raw_pcm) - self.bytes_transcribed_for_partial
        if pending_bytes < min_bytes_for_partial:
            return ""

        bytes_per_second = state.rate * 2
        partial_bytes = int(float(bytes_per_second) * self.cfg.whisplaybot_partial_inference_seconds)
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
            self.cfg.whisplaybot_recognize_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _request() -> tuple[int, str]:
            with urlopen(request, timeout=max(self.cfg.whisplaybot_timeout_seconds, 10.0)) as response:
                status = getattr(response, "status", response.getcode())
                body = response.read().decode("utf-8")
                return status, body

        try:
            status, body = await asyncio.to_thread(_request)
        except Exception as err:
            raise RuntimeError(f"WhisplayBot request failed: {err}") from err

        if not (200 <= status <= 299):
            raise RuntimeError(f"WhisplayBot request failed (status {status}): {body}")

        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"WhisplayBot response is malformed: {body}") from err

        error = (decoded.get("error") or "").strip()
        if error:
            raise RuntimeError(error)

        return (decoded.get("recognition") or "").strip()

    def should_auto_finalize(self, state: AudioState) -> bool:
        bytes_per_second = state.rate * 2
        min_bytes = max(int(float(bytes_per_second) * self.cfg.whisplaybot_auto_final_min_seconds), state.rate)
        silence_bytes = int(float(bytes_per_second) * float(self.cfg.whisplaybot_auto_final_silence_ms) / 1000.0)
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
        translator: Translator,
    ):
        self.reader = reader
        self.writer = writer
        self.cfg = cfg
        self.vosk_model = vosk_model
        self.translator = translator
        self.state = AudioState(language=cfg.language)
        self.state.translate_enabled = cfg.translate_enabled
        self.state.translate_pairs = cfg.translate_pairs
        self.state.translate_source = cfg.translate_source
        self.state.translate_target = cfg.translate_target
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
                "whisplaybot": "WhisplayBot",
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
                            "version": "1.0.0",
                        }
                    ],
                    "translation": {
                        "enabled": self.state.translate_enabled,
                        "pairs": list(self.state.translate_pairs),
                        "pair": self.current_translation_pair(),
                        "source": self.state.translate_source,
                        "target": self.state.translate_target,
                    },
                },
            )
            return

        if event_type == "transcribe":
            language = (data.get("language") or "").strip()
            if language:
                self.state.language = language
            return

        if event_type == "translate-get":
            await self.send_translation_config()
            return

        if event_type == "translate-set":
            enabled = data.get("enabled")
            pair = (data.get("pair") or "").strip()
            source = (data.get("source") or "").strip()
            target = (data.get("target") or "").strip()
            if enabled is not None:
                self.state.translate_enabled = bool(enabled)
            if pair:
                if not self.is_translation_pair_allowed(pair):
                    await self.emit_error_text(f"Translation pair '{pair}' is not allowed")
                    await self.send_translation_config()
                    return
                source, target = split_translation_pair(pair)
            elif source and target and not self.is_translation_pair_allowed(f"{source}-{target}"):
                await self.emit_error_text(f"Translation pair '{source}-{target}' is not allowed")
                await self.send_translation_config()
                return
            if source:
                self.state.translate_source = source
            if target:
                self.state.translate_target = target
            await self.send_translation_config()
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
            return OpenAIRealtimeBackend(self.cfg, self.emit_partial_text, self.emit_final_text, self.emit_error_text)
        if self.cfg.stt_backend == "whisplaybot":
            return WhisplayBackend(self.cfg, self.emit_partial_text, self.emit_final_text)
        if self.vosk_model is None:
            raise RuntimeError("Vosk backend selected but no model is loaded")
        return VoskBackend(self.cfg, self.vosk_model, self.emit_partial_text, self.emit_final_text)

    async def close_backend(self) -> None:
        if self.backend is not None:
            await self.backend.close()
            self.backend = None

    async def send_event(self, event_type: str, data: Optional[dict[str, Any]] = None, payload: bytes = b"") -> None:
        self.writer.write(event_bytes(event_type, data, payload))
        await self.writer.drain()

    async def emit_partial_text(self, text: str) -> None:
        text = text.strip()
        if text:
            await self.send_event("transcript-chunk", {"text": text})

    async def emit_final_text(self, text: str) -> None:
        text = text.strip()
        if not text:
            return

        if not self.state.translate_enabled or not self.state.translate_target:
            await self.send_event("transcript", {"text": text})
            return

        try:
            translated = await self.translator.translate(
                text,
                self.state.translate_source or "auto",
                self.state.translate_target,
            )
            await self.send_event(
                "transcript",
                {
                    "text": translated,
                    "original_text": text,
                    "translated": True,
                    "source_language": self.state.translate_source or "auto",
                    "target_language": self.state.translate_target,
                },
            )
        except Exception as err:
            LOGGER.exception("Translation failed")
            await self.send_event(
                "transcript",
                {
                    "text": text,
                    "translation_error": str(err),
                    "translated": False,
                },
            )

    async def emit_error_text(self, message: str) -> None:
        await self.send_event("error", {"message": message})

    async def send_translation_config(self) -> None:
        await self.send_event(
            "translate-config",
            {
                "enabled": self.state.translate_enabled,
                "pairs": list(self.state.translate_pairs),
                "pair": self.current_translation_pair(),
                "source": self.state.translate_source,
                "target": self.state.translate_target,
            },
        )

    def current_translation_pair(self) -> str:
        source = (self.state.translate_source or "").strip()
        target = (self.state.translate_target or "").strip()
        if not source or not target:
            return ""
        return f"{source}-{target}"

    def is_translation_pair_allowed(self, pair: str) -> bool:
        allowed = self.state.translate_pairs
        if not allowed:
            return True
        source, target = split_translation_pair(pair)
        if not source or not target:
            return False
        return f"{source}-{target}" in allowed


def split_translation_pair(pair: str) -> tuple[str, str]:
    normalized = pair.strip().replace("->", "-").replace("_", "-")
    source, _, target = normalized.partition("-")
    return source.strip(), target.strip()


def parse_translation_pairs(raw_value: str) -> tuple[str, ...]:
    raw_value = (raw_value or "").strip()
    if not raw_value:
        return ()

    values: list[str]
    if raw_value.startswith("["):
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError:
            decoded = []
        values = [str(item).strip() for item in decoded if str(item).strip()]
    else:
        values = [item.strip() for item in raw_value.split(",") if item.strip()]

    pairs: list[str] = []
    for value in values:
        source, target = split_translation_pair(value)
        if source and target:
            pairs.append(f"{source}-{target}")
    return tuple(dict.fromkeys(pairs))


async def serve(cfg: ServerConfig) -> None:
    vosk_model = None
    translator = Translator(cfg)
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
    if cfg.stt_backend == "whisplaybot":
        LOGGER.info("WhisplayBot backend enabled with recognize URL %s", cfg.whisplaybot_recognize_url)

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = HallidaySession(reader, writer, cfg, vosk_model, translator)
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
    parser.add_argument("--translate-enabled", action="store_true")
    parser.add_argument("--translate-url", default="http://homeassistant.local:5000/translate")
    parser.add_argument("--translate-pairs", default='["en-el","el-en","en-de","de-en","en-fr","fr-en"]')
    parser.add_argument("--translate-source", default="auto")
    parser.add_argument("--translate-target", default="")
    parser.add_argument("--translate-timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()
    translate_pairs = parse_translation_pairs(args.translate_pairs)
    translate_source = args.translate_source
    translate_target = args.translate_target
    if not translate_target and translate_pairs:
        translate_source, translate_target = split_translation_pair(translate_pairs[0])
    if translate_target and translate_pairs:
        current_pair = f"{translate_source}-{translate_target}"
        if current_pair not in translate_pairs:
            translate_source, translate_target = split_translation_pair(translate_pairs[0])

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
        whisplaybot_recognize_url=args.whisplay_recognize_url,
        whisplaybot_timeout_seconds=args.whisplay_timeout_seconds,
        whisplaybot_partial_window_seconds=args.whisplay_partial_window_seconds,
        whisplaybot_partial_inference_seconds=args.whisplay_partial_inference_seconds,
        whisplaybot_auto_final_silence_ms=args.whisplay_auto_final_silence_ms,
        whisplaybot_auto_final_min_seconds=args.whisplay_auto_final_min_seconds,
        whisplaybot_auto_final_silence_level=args.whisplay_auto_final_silence_level,
        translate_enabled=args.translate_enabled,
        translate_url=args.translate_url,
        translate_pairs=translate_pairs,
        translate_source=translate_source,
        translate_target=translate_target,
        translate_timeout_seconds=args.translate_timeout_seconds,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = parse_args()
    asyncio.run(serve(cfg))


if __name__ == "__main__":
    main()
