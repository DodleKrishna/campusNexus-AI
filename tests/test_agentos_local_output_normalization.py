"""Local-model schema compatibility: the Ollama adapter drops a stray ``user_message`` before validation.

Observed with qwen3:8b: every schema field is required (nullable), so the model sometimes fills ``user_message`` on a
FAIL decision, which ``AgentDecision`` refuses. Only that slot is normalized, only in the local adapter, and only for
kinds that may not carry it. Everything else stays refused by the unchanged validation. Offline: a fake Ollama.
"""
from __future__ import annotations

import json

import pytest

from app.agentos.brain import BrainOutputError
from app.agentos.local_brain import normalize_local_output
from app.agentos.providers import parse_decision
from app.agentos.schemas import AgentDecision, DecisionKind
from app.db.models import AgentMissionStatus
from tests.test_agentos_nexus import ask, mission_rows, use_brain, wire
from tests.test_agentos_offline_edge import THINKING, FakeOllama, context, db_text, local_router, ollama_brain
from tests.test_phase22b_tenant_isolation import A_STUDENT, app, client, orgs  # noqa: F401 -- fixtures

STRAY = "Sorry, I can't do that right now."


def reply(raw: dict) -> dict:
    return {"message": {"content": json.dumps(raw), "thinking": THINKING}, "done_reason": "stop"}


def decide(raw: dict, tools=("get_my_identity_context",)) -> AgentDecision:
    return ollama_brain(FakeOllama(replies=[reply(raw)])).decide(context(tools=tools))


# --- The normalizer (pure) ------------------------------------------------------------------------------------------


@pytest.mark.parametrize("raw", [
    wire("fail", reason="No tool for this.", user_message=STRAY),
    wire("tool", tool_name="get_my_identity_context", tool_input_json="{}", user_message=STRAY),
    {"kind": "delegate", "delegate_agent": "x_agent", "delegate_goal": "g", "user_message": STRAY},
    {"kind": "wait", "wait_for": "ASSIGNMENT_SUBMITTED", "user_message": STRAY},
    {"kind": "replan", "plan": ["a"], "user_message": STRAY},
])
def test_user_message_is_stripped_from_kinds_that_may_not_carry_it(raw) -> None:
    normalized = normalize_local_output(raw)
    assert "user_message" not in normalized
    assert normalized == {k: v for k, v in raw.items() if k != "user_message"}  # nothing else changes
    assert "user_message" in raw  # the input is not mutated


@pytest.mark.parametrize("raw", [
    wire("complete", outcome="Answered.", user_message="Here you go."),
    wire("ask_human", question="Which course?", user_message="Which course do you mean?"),
])
def test_complete_and_ask_human_keep_their_message(raw) -> None:
    assert normalize_local_output(raw) == raw


@pytest.mark.parametrize("raw", [
    "not a dict", ["fail"], None, {"kind": "fail", "reason": "r"},
    wire("complete", outcome="Answered."),  # no message: none is invented
])
def test_anything_else_passes_through_unchanged(raw) -> None:
    assert normalize_local_output(raw) is raw


# --- Through the local brain -----------------------------------------------------------------------------------------


def test_a_fail_with_a_stray_message_becomes_a_valid_fail() -> None:
    decision = decide(wire("fail", reason="Capability not available yet.", user_message=STRAY))
    assert (decision.kind, decision.reason, decision.user_message) == (DecisionKind.FAIL, "Capability not available yet.",
                                                                       None)


def test_a_tool_with_a_stray_message_becomes_a_valid_tool_call() -> None:
    decision = decide(wire("tool", tool_name="get_my_identity_context", tool_input_json="{}", user_message=STRAY))
    assert (decision.kind, decision.tool_name, decision.user_message) == (DecisionKind.TOOL, "get_my_identity_context",
                                                                          None)


def test_complete_keeps_the_message_and_none_is_invented() -> None:
    assert decide(wire("complete", outcome="Answered.", user_message="Hi.")).user_message == "Hi."
    assert decide(wire("complete", outcome="Answered.")).user_message is None


@pytest.mark.parametrize("raw, code", [
    (wire("fail", user_message=STRAY), "INVALID_DECISION_SHAPE"),  # still missing its reason
    (wire("fail", reason="r", outcome="x", user_message=STRAY), "INVALID_DECISION_SHAPE"),  # other strays still refused
    (wire("complete", user_message="Hi."), "INVALID_DECISION_SHAPE"),  # complete still needs an outcome
    ({**wire("fail", reason="r", user_message=STRAY), "reasoning": "hidden"}, "UNEXPECTED_FIELDS"),
    (wire("explode", reason="r", user_message=STRAY), "KIND_NOT_OFFERED"),
    (wire("tool", tool_name="get_my_identity_context", tool_input_json="{bad", user_message=STRAY),
     "MALFORMED_TOOL_INPUT"),
])
def test_other_malformed_local_outputs_are_still_refused(raw, code) -> None:
    with pytest.raises(BrainOutputError) as exc:
        decide(raw)
    assert exc.value.code == code


def test_shared_validation_is_not_weakened() -> None:
    """The cloud path (parse_decision without the local shim) and AgentDecision itself still refuse it."""
    with pytest.raises(BrainOutputError) as exc:
        parse_decision(wire("fail", reason="r", user_message=STRAY),
                       {"properties": {**{k: {} for k in wire("fail")}, "kind": {"enum": ["fail"]}}})
    assert exc.value.code == "INVALID_DECISION_SHAPE"
    with pytest.raises(ValueError):
        AgentDecision(kind=DecisionKind.FAIL, reason="r", user_message=STRAY)


def test_one_local_call_per_decision_no_retry() -> None:
    fake = FakeOllama(replies=[reply(wire("fail", user_message=STRAY))])
    with pytest.raises(BrainOutputError):
        ollama_brain(fake).decide(context())
    assert len(fake.requests) == 1


# --- Through the kernel ----------------------------------------------------------------------------------------------


def test_the_kernel_records_a_clean_fail_and_stores_no_stray_text_or_thinking(app, client, session_factory) -> None:
    fake = FakeOllama(replies=[reply(wire("fail", reason="Capability not available yet.", user_message=STRAY))])
    use_brain(app, local_router(app, fake)[0])
    body = ask(client, A_STUDENT).json()
    mission, steps, _ = mission_rows(session_factory, body["mission_id"])
    assert mission.status == AgentMissionStatus.FAILED
    assert [(s.action_type, s.status.value) for s in steps] == [("fail", "executed")]  # not "invalid" / rejected
    stored = db_text(session_factory)
    assert STRAY not in stored and THINKING not in stored
