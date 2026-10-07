"""Provider-facing voice endpoints (AgentOS V2 Phase 5 / 5.1). Presentation only: they call ``app.communication.voice``.

* ``POST /communication/exotel/status?token=...`` -- Exotel's call-status callback (form-encoded). The signed token is
  the only authority; an invalid token or an unknown call is a 404 that reveals nothing.
* ``GET /communication/exotel/stream-url?CustomField=<stream token>`` -- the dynamic URL for the flow's Voicebot
  applet: the call's ``wss`` stream URL at 16 kHz linear16. Other query parameters (which include the caller's
  number) are never read or logged.
* ``WS /communication/voice/stream`` -- the bidirectional stream (JSON frames ``connected`` / ``start`` / ``media`` /
  ``mark`` / ``stop``). ``start.media_format`` must be linear16 at 16 kHz or the stream is closed (1003). Turn-based:
  the deterministic opening plays first, then each VAD utterance gets one reply; while a reply plays, listening is
  paused (incoming audio, including echo, is dropped) until Exotel returns our ``mark`` (or the reply's duration has
  passed). A final reply's mark closes the stream.

Bounds: frame size, decoded media per frame, cumulative call audio, the VAD's utterance limits and the conversation's
turn limit. Raw WebSocket messages are never logged (``start`` may carry phone numbers); nothing is stored but the
VoiceSession's counters and outcome code.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import timedelta
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.communication.voice import tokens
from app.communication.voice.audio import BYTES_PER_SECOND, AudioFormatError, duration_seconds, playback_chunks, \
    validate_media_format
from app.communication.voice.callbacks import StatusCallback, handle_status_callback
from app.communication.voice.conversation import ConversationError, VoiceConversation

router = APIRouter(prefix="/communication", tags=["communication"])

MAX_FRAME_CHARS = 32 * 1024  # one JSON frame
MAX_MEDIA_BYTES = BYTES_PER_SECOND // 2  # decoded audio per media frame (0.5 s)
CALL_AUDIO_SLACK_SECONDS = 30  # cumulative inbound audio may exceed the call time limit by at most this
PLAYBACK_GRACE = timedelta(seconds=2)  # resume listening if Exotel never returns our mark
UNSUPPORTED_DATA, POLICY_VIOLATION, TOO_BIG, UNAVAILABLE = 1003, 1008, 1009, 1011


def _int(value: Any) -> Optional[int]:
    try:
        number = int(str(value))
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 86400 else None


@router.post("/exotel/status")
async def exotel_status(request: Request, token: str = Query(..., max_length=400)) -> dict:
    form = await request.form()
    callback = StatusCallback(token=token, call_sid=str(form.get("CallSid") or "")[:80],
                              status=str(form.get("Status") or "")[:30],
                              duration_seconds=_int(form.get("ConversationDuration") or form.get("Duration")))
    state = request.app.state
    result = await run_in_threadpool(handle_status_callback, state.session_factory, state.agent_runtime.communication,
                                     callback, state.clock())
    if result.result == "rejected":
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return {"result": result.result}


@router.get("/exotel/stream-url")
async def exotel_stream_url(request: Request, custom_field: str = Query(..., alias="CustomField", max_length=400)) -> dict:
    state = request.app.state
    connector = state.agent_runtime.communication.connectors.get("voice")
    try:
        tokens.verify(custom_field, "stream", state.clock())
    except tokens.VoiceTokenError:
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"}) from None
    if connector is None or not connector.available():
        raise HTTPException(status_code=404, detail={"code": "NOT_FOUND"})
    return {"url": connector.config.stream_url(custom_field)}


class _Stream:
    """Per-WebSocket state. Holds audio only transiently (the VAD's bounded buffers)."""

    def __init__(self, ws: WebSocket, pipeline: Any, limit_seconds: int) -> None:
        self.ws, self.state = ws, ws.app.state
        self.pipeline, self.detector = pipeline, pipeline.new_detector()
        self.conversation: Optional[VoiceConversation] = None
        self.stream_sid: Optional[str] = None
        self.received = 0
        self.max_audio = (limit_seconds + CALL_AUDIO_SLACK_SECONDS) * BYTES_PER_SECOND
        self.marks = 0
        self.awaiting_mark: Optional[str] = None
        self.playback_until = None
        self.closing = False

    async def play(self, pcm: Optional[bytes], final: bool = False) -> None:
        """Send the reply as 100 ms media frames, then a mark; listening stays paused until that mark returns."""
        if not pcm:
            return
        self.detector.pause()
        for chunk in playback_chunks(pcm):
            await self.ws.send_json({"event": "media", "stream_sid": self.stream_sid,
                                     "media": {"payload": base64.b64encode(chunk).decode("ascii")}})
        self.marks += 1
        self.awaiting_mark = f"cn-{self.marks}"
        await self.ws.send_json({"event": "mark", "stream_sid": self.stream_sid, "mark": {"name": self.awaiting_mark}})
        self.playback_until = self.state.clock() + timedelta(seconds=duration_seconds(len(pcm))) + PLAYBACK_GRACE
        self.closing = final

    async def clear(self) -> None:
        """Stop any queued playback (structural support for a later barge-in; unused in turn-based mode)."""
        await self.ws.send_json({"event": "clear", "stream_sid": self.stream_sid})

    def playback_done(self) -> bool:
        return self.awaiting_mark is None or (self.playback_until is not None and self.state.clock() >= self.playback_until)

    def resume(self) -> None:
        self.awaiting_mark, self.playback_until = None, None
        self.detector.resume()


@router.websocket("/voice/stream")
async def voice_stream(ws: WebSocket) -> None:
    await ws.accept()
    pipeline = getattr(ws.app.state, "voice_pipeline", None)
    if pipeline is None:
        await ws.close(code=UNAVAILABLE)
        return
    stream = _Stream(ws, pipeline, ws.app.state.agent_runtime.communication.policy.voice_time_limit_seconds)
    try:
        while True:
            text = await ws.receive_text()
            if len(text) > MAX_FRAME_CHARS:
                await ws.close(code=TOO_BIG)
                return
            try:
                message = json.loads(text)
            except ValueError:
                continue
            event = message.get("event") if isinstance(message, dict) else None
            if event == "start" and stream.conversation is None:
                if not await _start(stream, message):
                    return
            elif event == "media" and stream.conversation is not None:
                if not await _media(stream, message):
                    return
            elif event == "mark" and stream.conversation is not None:
                name = (message.get("mark") or {}).get("name") if isinstance(message.get("mark"), dict) else None
                if name is not None and name == stream.awaiting_mark:
                    if stream.closing:
                        await ws.close()
                        return
                    stream.resume()
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        stream.detector.reset()
        if stream.conversation is not None and not stream.conversation.ended:
            await run_in_threadpool(stream.conversation.stop)


async def _start(stream: _Stream, message: dict) -> bool:
    start = message.get("start") if isinstance(message.get("start"), dict) else {}
    try:
        validate_media_format(start.get("media_format"))
    except AudioFormatError:
        await stream.ws.close(code=UNSUPPORTED_DATA)
        return False
    stream.stream_sid = str(start.get("stream_sid") or message.get("stream_sid") or "")[:80] or None
    params = start.get("custom_parameters") if isinstance(start.get("custom_parameters"), dict) else {}
    token = params.get("token") or stream.ws.query_params.get("token") or ""
    state = stream.state
    try:
        conversation = await run_in_threadpool(VoiceConversation, state.session_factory,
                                               state.agent_runtime.communication, stream.pipeline, str(token)[:400],
                                               state.clock)
        opening = await run_in_threadpool(conversation.start, stream.stream_sid)
    except ConversationError:
        await stream.ws.close(code=POLICY_VIOLATION)
        return False
    stream.conversation = conversation
    await stream.play(opening)
    return True


async def _media(stream: _Stream, message: dict) -> bool:
    media = message.get("media") if isinstance(message.get("media"), dict) else {}
    payload = media.get("payload") or ""
    if not isinstance(payload, str) or len(payload) > (MAX_MEDIA_BYTES * 4) // 3 + 4:
        await stream.ws.close(code=TOO_BIG)
        return False
    try:
        chunk = base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError):
        return True
    stream.received += len(chunk)
    if stream.received > stream.max_audio:  # bounded cumulative call audio
        await run_in_threadpool(stream.conversation.stop)
        await stream.ws.close()
        return False
    if not stream.detector.listening:
        if not stream.playback_done():
            return True  # our own reply is playing: this audio (and its echo) is never transcribed
        if stream.closing:
            await stream.ws.close()
            return False
        stream.resume()
    for utterance in stream.detector.feed(chunk):
        result = await run_in_threadpool(stream.conversation.on_utterance, utterance)
        if result.reply_pcm:
            await stream.play(result.reply_pcm, final=result.end_call)
        elif result.end_call:
            await stream.ws.close()
            return False
        break  # one utterance per turn; anything after it was spoken over our reply and is dropped
    return True
