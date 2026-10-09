"""Live Exotel voice smoke check (AgentOS V2 Phase 5.1). Places a REAL phone call -- never run by pytest.

    python scripts/exotel_voice_smoke.py

Runs only when every one of these is set in the shell (else it prints SKIPPED with the missing *names* and exits 0):
EXOTEL_API_KEY, EXOTEL_API_TOKEN, EXOTEL_ACCOUNT_SID, EXOTEL_CALLER_ID, CAMPUSNEXUS_PUBLIC_BASE_URL (https),
CAMPUSNEXUS_TEST_PHONE, GROQ_API_KEY (and EXOTEL_FLOW_APP_ID in ``EXOTEL_CALL_MODE=flow``).

It places exactly ONE call to CAMPUSNEXUS_TEST_PHONE with Record=false and a 60 s time limit: in the default stream
mode through Exotel's Connect Voice AI API (StreamUrl = the API's bidirectional wss stream at sample-rate=16000,
StreamType=bidirectional); in flow mode through the configured flow. This smoke call carries a signed token for no
real communication attempt, so the API accepts the 16 kHz media format and then closes the stream (1008): it verifies
call placement and stream negotiation, not a conversation. A full conversation runs through the normal path
(``python scripts/demo_scenarios.py absence --start-and-mark`` with CAMPUSNEXUS_DEMO_MODE=1).

Output: the call SID, the provider's initial status and the request settings. Never the phone number or credentials.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.communication.voice import tokens  # noqa: E402
from app.communication.voice.audio import SAMPLE_RATE  # noqa: E402
from app.communication.voice.exotel import (  # noqa: E402
    STATUS_PATH, ExotelConfig, ExotelError, HttpExotelClient,
)

REQUIRED = ("EXOTEL_API_KEY", "EXOTEL_API_TOKEN", "EXOTEL_ACCOUNT_SID", "EXOTEL_CALLER_ID",
            "CAMPUSNEXUS_PUBLIC_BASE_URL", "CAMPUSNEXUS_TEST_PHONE", "GROQ_API_KEY")
TIME_LIMIT_SECONDS = 60
SMOKE_ATTEMPT_ID = 0  # no real attempt: the API refuses the conversation after validating the stream


def _print(payload: dict) -> None:
    print(json.dumps(payload, separators=(",", ":")), flush=True)


def main() -> int:
    missing = [name for name in REQUIRED if not (os.environ.get(name) or "").strip()]
    if missing:
        _print({"status": "SKIPPED", "missing": missing})
        return 0
    config = ExotelConfig.from_env()
    if not config.configured():
        _print({"status": "SKIPPED", "reason": config.unavailable_reason()})
        return 0
    expires = datetime.now(timezone.utc) + timedelta(minutes=15)
    callback = tokens.sign("callback", 0, SMOKE_ATTEMPT_ID, expires)
    stream = tokens.sign("stream", 0, SMOKE_ATTEMPT_ID, expires)
    try:
        route = ({"stream_url": config.stream_url(stream)} if config.call_mode == "stream"
                 else {"flow_url": config.flow_url(), "custom_field": stream})
        call = HttpExotelClient(config).place_call(
            to=os.environ["CAMPUSNEXUS_TEST_PHONE"].strip(), caller_id=config.caller_id,
            status_callback=f"{config.public_base_url}{STATUS_PATH}?token={quote(callback)}",
            time_limit_seconds=TIME_LIMIT_SECONDS, **route)
    except ExotelError as exc:
        _print({"status": "FAILED", "code": exc.code})
        return 1
    _print({"status": "PLACED", "mode": config.call_mode, "call_sid": call.reference, "provider_status": call.status,
            "record": False,
            "time_limit_seconds": TIME_LIMIT_SECONDS, "stream_sample_rate": SAMPLE_RATE, "calls_placed": 1})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
