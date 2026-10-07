"""Provider-facing voice endpoints (AgentOS V2 Phase 5). Presentation only: they call ``app.communication.voice``.

* ``POST /communication/exotel/status?token=...`` -- Exotel's call-status callback (form-encoded). The signed token
  is the only authority; an invalid token or an unknown call is a 404 that reveals nothing.
* ``WS /communication/voice/stream`` -- Exotel's Voicebot stream (JSON frames: ``connected`` / ``start`` / ``media`` /
  ``stop``). The stream token comes from the ``start`` frame's ``custom_parameters.token`` (or the ``token`` query
  parameter). Audio is buffered in memory per utterance (``UTTERANCE_BYTES`` of 8 kHz 16-bit audio, a simple
  stand-in for voice-activity detection) and never stored. Without a configured ``app.state.voice_pipeline`` the
  stream is refused.
"""
from __future__ import annotations

import base64
import binascii
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.communication.voice.callbacks import StatusCallback, handle_status_callback
from app.communication.voice.conversation import ConversationError, VoiceConversation

router = APIRouter(prefix="/communication", tags=["communication"])

UTTERANCE_BYTES = 8000 * 2 * 3  # ~3 s of 8 kHz 16-bit mono audio
MAX_FRAME_BYTES = 64 * 1024
POLICY_VIOLATION, UNAVAILABLE = 1008, 1011


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


async def _send_audio(ws: WebSocket, stream_sid: Optional[str], audio: Optional[bytes]) -> None:
    if audio:
        await ws.send_json({"event": "media", "stream_sid": stream_sid,
                            "media": {"payload": base64.b64encode(audio).decode("ascii")}})


@router.websocket("/voice/stream")
async def voice_stream(ws: WebSocket) -> None:
    await ws.accept()
    state = ws.app.state
    pipeline = getattr(state, "voice_pipeline", None)
    if pipeline is None:
        await ws.close(code=UNAVAILABLE)
        return
    conversation: Optional[VoiceConversation] = None
    stream_sid: Optional[str] = None
    buffer = bytearray()
    try:
        while True:
            message = await ws.receive_json()
            event = message.get("event") if isinstance(message, dict) else None
            if event == "start" and conversation is None:
                start = message.get("start") or {}
                stream_sid = str(start.get("stream_sid") or message.get("stream_sid") or "")[:80] or None
                token = (start.get("custom_parameters") or {}).get("token") or ws.query_params.get("token")
                try:
                    conversation = await run_in_threadpool(VoiceConversation, state.session_factory,
                                                           state.agent_runtime.communication, pipeline, token or "",
                                                           state.clock)
                    await _send_audio(ws, stream_sid, await run_in_threadpool(conversation.start, stream_sid))
                except ConversationError:
                    conversation = None
                    await ws.close(code=POLICY_VIOLATION)
                    return
            elif event == "media" and conversation is not None:
                try:
                    chunk = base64.b64decode(((message.get("media") or {}).get("payload") or ""), validate=True)
                except (binascii.Error, ValueError):
                    continue
                buffer += chunk[:MAX_FRAME_BYTES]
                if len(buffer) >= UTTERANCE_BYTES:
                    reply = await run_in_threadpool(conversation.on_utterance, bytes(buffer))
                    buffer.clear()
                    await _send_audio(ws, stream_sid, reply)
                    if conversation.ended:
                        await ws.close()
                        return
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    finally:
        buffer.clear()
        if conversation is not None and not conversation.ended:
            await run_in_threadpool(conversation.stop)
