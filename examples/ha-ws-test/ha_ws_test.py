#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
import sys
import wave
from pathlib import Path

import websockets


async def send_json(ws, payload):
    await ws.send(json.dumps(payload, separators=(",", ":")))


async def recv_json(ws):
    payload = await ws.recv()
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    return json.loads(payload)


def load_pcm16_mono(path: Path) -> tuple[bytes, int]:
    if path.suffix.lower() == ".wav":
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            width = wav_file.getsampwidth()
            rate = wav_file.getframerate()
            if channels != 1 or width != 2:
                raise ValueError("WAV file must be PCM16 mono")
            return wav_file.readframes(wav_file.getnframes()), rate
    return path.read_bytes(), 16000


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scheme", default="ws")
    parser.add_argument("--host", default="homeassistant.local")
    parser.add_argument("--port", type=int, default=8123)
    parser.add_argument("--ha-token", required=True)
    parser.add_argument("--audio-file", type=Path)
    parser.add_argument("--language", default="en")
    parser.add_argument("--translate-source")
    parser.add_argument("--translate-target")
    args = parser.parse_args()

    ws_url = f"{args.scheme}://{args.host}:{args.port}/api/websocket"
    async with websockets.connect(ws_url, max_size=2**24) as ws:
        first = await recv_json(ws)
        if first.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected auth handshake: {first}")

        await send_json(ws, {"type": "auth", "access_token": args.ha_token})
        second = await recv_json(ws)
        if second.get("type") != "auth_ok":
            raise RuntimeError(f"Authentication failed: {second}")

        await send_json(
            ws,
            {
                "id": 1,
                "type": "halliday_glasses_bridge/open_stream",
                "language": args.language,
                "rate": 16000,
                "width": 2,
                "channels": 1,
            },
        )
        opened = await recv_json(ws)
        session_id = ((opened.get("result") or {}).get("session_id") or "").strip()
        if not session_id:
            raise RuntimeError(f"Missing session_id in response: {opened}")
        print(f"[open] session_id={session_id}")

        if args.translate_source or args.translate_target:
            payload = {
                "id": 2,
                "type": "halliday_glasses_bridge/translate_set",
                "session_id": session_id,
            }
            if args.translate_source:
                payload["source"] = args.translate_source
            if args.translate_target:
                payload["target"] = args.translate_target
            await send_json(ws, payload)
            print("[translate] update sent")

        if args.audio_file:
            pcm, rate = load_pcm16_mono(args.audio_file)
            chunk_size = 3200
            message_id = 10
            for offset in range(0, len(pcm), chunk_size):
                chunk = pcm[offset : offset + chunk_size]
                await send_json(
                    ws,
                    {
                        "id": message_id,
                        "type": "halliday_glasses_bridge/audio_chunk",
                        "session_id": session_id,
                        "rate": rate,
                        "width": 2,
                        "channels": 1,
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    },
                )
                message_id += 1
            await send_json(ws, {"id": message_id, "type": "halliday_glasses_bridge/close_stream", "session_id": session_id})
            print("[audio] file sent")

        while True:
            event = await recv_json(ws)
            if event.get("type") == "event":
                body = event.get("event") or {}
                print(json.dumps(body, ensure_ascii=False))
            elif event.get("type") == "result":
                continue
            else:
                print(json.dumps(event, ensure_ascii=False))


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(0)
