"""Phase 12C -- Groq free-tier rate-limit resilience, tested offline.

The live Phase 12B run lost scenario C to HTTP 429: three DAG tasks called
Groq at once, each 429 became a task failure, and the Orchestrator replanned
twice -- spending more of the same tokens-per-minute window on new plans.

These tests pin the fix with scripted ``httpx.MockTransport`` servers (no
network, no key):

- Groq calls are serialized by a provider-level limit (default 1) while the
  DAG still dispatches independent tasks in parallel.
- A 429 waits for the time Groq asks for (retry-after, token-reset header,
  or its "try again in" text) and nothing else is sent meanwhile.
- A rate limit / timeout / network error / 5xx that outlasts the bounded
  retries is an ``LLMTransientError``: the task stays PENDING, the mission
  stops without replanning, and a resume continues it.
- Mock and Anthropic providers are not throttled.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import pytest

httpx = pytest.importorskip("httpx")

from app.graph.orchestrator import MissionOrchestrator
from app.graph.registry import AgentRegistry
from app.llm.base import LLMProviderError, LLMRateLimitError, LLMTransientError
from app.llm.providers.anthropic_provider import AnthropicLLMProvider
from app.llm.providers.groq import DEFAULT_MODEL, GroqLLMProvider
from app.llm.providers.mock import MockLLMProvider
from app.schemas.agent import AgentMessage
from app.schemas.academic import CourseSummary
from app.schemas.career import CareerIntent, CareerResponseContext
from app.schemas.enums import AgentName, AgentResultStatus, MissionStatus, TaskStatus, UserRole, VerificationStatus
from app.schemas.mission import MissionPlan, MissionTask
from app.services.context import ContextService
from scripts import check_live_llm
from tests.graph_doubles import AlwaysFailAgent, FixedPlanLLMProvider, _outcome

SECRET = "gsk_phase12c_secret"


# ---------------------------------------------------------------------------
# Scripted Groq
# ---------------------------------------------------------------------------


def _tool_reply(name: str, arguments: Dict[str, Any], usage: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    message = {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
    reply: Dict[str, Any] = {"choices": [{"index": 0, "finish_reason": "tool_calls", "message": message}]}
    if usage:
        reply["usage"] = usage
    return reply


def _rate_limited(headers: Optional[Dict[str, str]] = None, message: str = "Rate limit reached for model on tokens per minute (TPM).") -> httpx.Response:
    body = {"error": {"message": message, "type": "tokens", "code": "rate_limit_exceeded"}}
    return httpx.Response(429, json=body, headers=headers or {})


CAREER_OK = _tool_reply("classify_career_intent", {"intent": "opportunity_discovery"})


class Server:
    """Answers from ``respond(body)``; tracks how many requests overlap and when each arrived."""

    def __init__(self, respond: Callable[[Dict[str, Any]], Any], hold_seconds: float = 0.0) -> None:
        self.respond = respond
        self.hold_seconds = hold_seconds
        self.bodies: List[Dict[str, Any]] = []
        self.arrivals: List[float] = []
        self._lock = threading.Lock()
        self._in_flight = 0
        self.peak = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self._in_flight += 1
            self.peak = max(self.peak, self._in_flight)
            self.bodies.append(json.loads(request.content))
            self.arrivals.append(time.monotonic())
        try:
            if self.hold_seconds:
                time.sleep(self.hold_seconds)
            result = self.respond(self.bodies[-1])
            if isinstance(result, Exception):
                raise result
            return result if isinstance(result, httpx.Response) else httpx.Response(200, json=result)
        finally:
            with self._lock:
                self._in_flight -= 1


def groq(server: Server, *, sleep: Optional[Callable[[float], None]] = None, **kwargs: Any):
    sleeps: List[float] = []
    client = httpx.Client(base_url="https://groq.test/openai/v1", transport=httpx.MockTransport(server))
    provider = GroqLLMProvider(api_key=SECRET, client=client, sleep=sleep or sleeps.append, **kwargs)
    return provider, sleeps


def _call_in_threads(fn: Callable[[], Any], n: int = 2) -> List[Any]:
    results: List[Any] = [None] * n
    start = threading.Barrier(n)

    def run(i: int) -> None:
        start.wait()
        results[i] = fn()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    return results


# ---------------------------------------------------------------------------
# 1. Concurrency limit
# ---------------------------------------------------------------------------


def test_default_concurrency_is_one_and_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CAMPUSNEXUS_LLM_MAX_CONCURRENCY", raising=False)
    assert groq(Server(lambda b: CAREER_OK))[0].max_concurrency == 1
    monkeypatch.setenv("CAMPUSNEXUS_LLM_MAX_CONCURRENCY", "3")
    assert groq(Server(lambda b: CAREER_OK))[0].max_concurrency == 3
    assert groq(Server(lambda b: CAREER_OK), max_concurrency=2)[0].max_concurrency == 2  # explicit wins


@pytest.mark.parametrize("raw", ["0", "-1", "many"])
def test_invalid_concurrency_is_refused(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("CAMPUSNEXUS_LLM_MAX_CONCURRENCY", raw)
    with pytest.raises(LLMProviderError, match="CAMPUSNEXUS_LLM_MAX_CONCURRENCY"):
        groq(Server(lambda b: CAREER_OK))


def test_two_concurrent_calls_are_serialized_at_limit_one() -> None:
    server = Server(lambda b: CAREER_OK, hold_seconds=0.15)
    provider, _ = groq(server, max_concurrency=1)

    results = _call_in_threads(lambda: provider.classify_career_intent("q").intent)

    assert results == [CareerIntent.OPPORTUNITY_DISCOVERY] * 2
    assert server.peak == 1 and provider.stats()["max_observed_concurrency"] == 1
    assert provider.stats()["requests"] == 2


def test_a_higher_limit_lets_calls_overlap() -> None:
    """Proves the test above would notice overlap: the limit is what serializes."""
    server = Server(lambda b: CAREER_OK, hold_seconds=0.2)
    provider, _ = groq(server, max_concurrency=2)
    _call_in_threads(lambda: provider.classify_career_intent("q"))
    assert server.peak == 2


@dataclass
class _ProviderCallingAgent:
    """A DAG task whose only work is one LLM call; records how many tasks overlap."""

    llm: Any
    tracker: Dict[str, Any]

    def handle(self, message: AgentMessage):
        with self.tracker["lock"]:
            self.tracker["active"] += 1
            self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
        try:
            self.llm.classify_career_intent(message.objective)
        finally:
            with self.tracker["lock"]:
                self.tracker["active"] -= 1
        return _outcome(message, agent_status=AgentResultStatus.SUCCESS, verification_status=VerificationStatus.VERIFIED)


def _two_independent_tasks(mission_id: str, goal: str) -> MissionPlan:
    return MissionPlan(mission_id=mission_id, goal=goal, tasks=[
        MissionTask(task_id=f"{mission_id}-task-1", mission_id=mission_id, agent=AgentName.CAREER_AGENT, objective="gaps"),
        MissionTask(task_id=f"{mission_id}-task-2", mission_id=mission_id, agent=AgentName.EVENTS_OPPORTUNITY_AGENT, objective="events"),
    ])


def _orchestrator(session_factory, llm, agent_factory, plan_factory=_two_independent_tasks, planner_calls=None):
    registry = AgentRegistry()
    registry.register(AgentName.CAREER_AGENT, agent_factory)
    registry.register(AgentName.EVENTS_OPPORTUNITY_AGENT, agent_factory)

    def counting_factory(mission_id: str, goal: str) -> MissionPlan:
        if planner_calls is not None:
            planner_calls.append(goal)
        return plan_factory(mission_id, goal)

    return MissionOrchestrator(
        session_factory=session_factory, registry=registry, llm_provider=FixedPlanLLMProvider(plan_factory=counting_factory)
    )


def test_dag_stays_parallel_while_groq_calls_are_serialized(session_factory) -> None:
    server = Server(lambda b: CAREER_OK, hold_seconds=0.15)
    provider, _ = groq(server, max_concurrency=1)
    tracker: Dict[str, Any] = {"lock": threading.Lock(), "active": 0, "peak": 0}
    orchestrator = _orchestrator(session_factory, provider, lambda s: _ProviderCallingAgent(provider, tracker))

    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert tracker["peak"] == 2  # both tasks dispatched in the same parallel round
    assert server.peak == 1  # ...but only one Groq request at a time


# ---------------------------------------------------------------------------
# 2-3. Rate-limit errors and retry-after
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "response, expected_wait",
    [
        (_rate_limited({"retry-after": "4", "x-ratelimit-reset-tokens": "9s"}), 4.0),  # retry-after wins
        (_rate_limited({"x-ratelimit-reset-tokens": "6.165s"}), 6.165),
        (_rate_limited({"x-ratelimit-reset-tokens": "250ms"}), 0.25),
        (_rate_limited(message="Rate limit reached ... Please try again in 7.5s. Need more tokens?"), 7.5),
        (_rate_limited(message="Please try again in 1m2.5s."), 30.0),  # capped
        (_rate_limited(), 1.0),  # no guidance at all: bounded backoff
    ],
)
def test_429_waits_for_the_time_groq_asks_for(response: httpx.Response, expected_wait: float) -> None:
    replies = iter([response, CAREER_OK])
    provider, sleeps = groq(Server(lambda b: next(replies)))

    assert provider.classify_career_intent("q").intent == CareerIntent.OPPORTUNITY_DISCOVERY
    assert sleeps == [expected_wait]
    stats = provider.stats()
    assert stats["rate_limit_retries"] == 1 and stats["rate_limit_wait_seconds"] == expected_wait


def test_no_request_is_sent_until_the_rate_limit_window_reopens() -> None:
    """Even with room for two calls, a 429 on one blocks the other until the wait is over."""
    wait = 0.4
    first = {"done": False}
    lock = threading.Lock()

    def respond(body: Dict[str, Any]) -> Any:
        with lock:
            if not first["done"]:
                first["done"] = True
                return _rate_limited({"retry-after": str(wait)})
        return CAREER_OK

    server = Server(respond)
    provider, _ = groq(server, sleep=time.sleep, max_concurrency=2)
    thread_a = threading.Thread(target=provider.classify_career_intent, args=("a",))
    thread_a.start()
    while not server.arrivals:
        time.sleep(0.005)
    provider.classify_career_intent("b")  # arrives while A is cooling down
    thread_a.join(timeout=5)

    rate_limited_at = server.arrivals[0]
    assert len(server.arrivals) == 3
    assert all(arrival >= rate_limited_at + wait - 0.05 for arrival in server.arrivals[1:])


def test_exhausted_rate_limit_is_a_classified_transient_error_without_secrets() -> None:
    provider, sleeps = groq(Server(lambda b: _rate_limited({"retry-after": "6"})))

    with pytest.raises(LLMRateLimitError) as info:
        provider.classify_career_intent("q")

    error = info.value
    assert isinstance(error, LLMTransientError) and isinstance(error, LLMProviderError)
    assert error.details() == {
        "kind": "rate_limit", "provider": "groq", "model": DEFAULT_MODEL,
        "status_code": 429, "retry_after_seconds": 6.0, "retries": 2,
    }
    assert sleeps == [6.0, 6.0]
    assert SECRET not in str(error) and SECRET not in json.dumps(error.details())


@pytest.mark.parametrize(
    "respond, kind, status",
    [
        (lambda b: httpx.ReadTimeout("slow"), "timeout", None),
        (lambda b: httpx.ConnectError("refused"), "network", None),
        (lambda b: httpx.Response(503, json={"error": {"message": "overloaded"}}), "server_error", 503),
    ],
)
def test_other_infrastructure_failures_are_transient(respond, kind: str, status: Optional[int]) -> None:
    provider, _ = groq(Server(respond))
    with pytest.raises(LLMTransientError) as info:
        provider.classify_career_intent("q")
    assert info.value.kind == kind and info.value.status_code == status and info.value.retries == 2


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "Invalid API Key", "code": "invalid_api_key"}}),
        httpx.Response(400, json={"error": {"message": "bad", "code": "tool_use_failed"}}),
        _tool_reply("classify_career_intent", {"intent": "not-an-intent"}),
    ],
)
def test_configuration_and_output_errors_are_not_transient(response) -> None:
    provider, _ = groq(Server(lambda b: response))
    with pytest.raises(LLMProviderError) as info:
        provider.classify_career_intent("q")
    assert not isinstance(info.value, LLMTransientError)


# ---------------------------------------------------------------------------
# 4. The Orchestrator never replans on a provider outage
# ---------------------------------------------------------------------------


class _Flaky:
    """Rate-limited until ``healthy`` is set; counts every attempt."""

    def __init__(self) -> None:
        self.healthy = False
        self.calls: Dict[str, int] = {}

    def agent(self, session):
        return self

    def handle(self, message: AgentMessage):
        self.calls[message.objective] = self.calls.get(message.objective, 0) + 1
        if message.objective == "events" and not self.healthy:
            raise LLMRateLimitError(
                "Groq rate limit exceeded (HTTP 429) after 2 retries: try again in 6s",
                provider="groq", model=DEFAULT_MODEL, status_code=429, retry_after_seconds=6.0, retries=2,
            )
        return _outcome(message, agent_status=AgentResultStatus.SUCCESS, verification_status=VerificationStatus.VERIFIED)


def _audit(session_factory, mission_id: str):
    with session_factory() as session:
        return [(e.event_type, e.step_id, dict(e.event_metadata or {})) for e in ContextService(session).list_audit_events(mission_id)]


def test_rate_limit_stops_the_mission_without_semantic_replanning(session_factory) -> None:
    flaky, planner_calls = _Flaky(), []
    orchestrator = _orchestrator(session_factory, None, flaky.agent, planner_calls=planner_calls)

    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)
    mission_id = final["mission_id"]
    events_task = f"{mission_id}-task-2"

    assert final["mission_status"] == MissionStatus.FAILED
    assert planner_calls == ["goal"]  # planned once; no replan
    assert final["replan_count"] == 0
    assert final["task_status"] == {f"{mission_id}-task-1": TaskStatus.COMPLETED, events_task: TaskStatus.PENDING}
    audit = _audit(session_factory, mission_id)
    types = [event for event, _, _ in audit]
    assert "replan_triggered" not in types and "task_failed" not in types
    outage = next(meta for event, step, meta in audit if event == "provider_unavailable")
    assert outage["kind"] == "rate_limit" and outage["status_code"] == 429 and outage["retry_after_seconds"] == 6.0
    assert not any("failure_fingerprint" in meta for _, _, meta in audit)
    assert "Mission paused: the LLM provider groq" in final["final_result"]
    assert "Nothing was replanned" in final["final_result"]
    with session_factory() as session:
        context = ContextService(session)
        steps = {s.step_id: s.status for s in context.get_mission(mission_id).steps}
        runs = [r.step_id for r in context.list_agent_runs(mission_id)]
    assert steps[events_task] == TaskStatus.PENDING
    assert runs == [f"{mission_id}-task-1"]  # no FAILED run recorded for the outage


def test_resume_after_the_outage_continues_the_same_plan(session_factory) -> None:
    flaky, planner_calls = _Flaky(), []
    orchestrator = _orchestrator(session_factory, None, flaky.agent, planner_calls=planner_calls)
    mission_id = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)["mission_id"]

    flaky.healthy = True
    resumed = orchestrator.resume_mission(mission_id)

    assert resumed["mission_status"] == MissionStatus.COMPLETED
    assert planner_calls == ["goal"]  # still one plan
    assert flaky.calls == {"gaps": 1, "events": 2}  # the completed task was not re-run


def test_genuine_task_failures_still_replan(session_factory) -> None:
    planner_calls: List[str] = []
    orchestrator = _orchestrator(session_factory, None, AlwaysFailAgent, planner_calls=planner_calls)
    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)
    assert len(planner_calls) > 1
    assert "replan_triggered" in [event for event, _, _ in _audit(session_factory, final["mission_id"])]


def test_successful_retry_continues_the_same_task(session_factory) -> None:
    replies = iter([_rate_limited({"retry-after": "2"}), CAREER_OK, CAREER_OK])
    provider, sleeps = groq(Server(lambda b: next(replies)))
    tracker: Dict[str, Any] = {"lock": threading.Lock(), "active": 0, "peak": 0}
    planner_calls: List[str] = []
    orchestrator = _orchestrator(
        session_factory, provider, lambda s: _ProviderCallingAgent(provider, tracker), planner_calls=planner_calls
    )

    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)

    assert final["mission_status"] == MissionStatus.COMPLETED
    assert sleeps == [2.0] and provider.stats()["rate_limit_retries"] == 1
    assert planner_calls == ["goal"]
    with session_factory() as session:
        assert len(ContextService(session).list_agent_runs(final["mission_id"])) == 2  # one run per task


def test_planner_rate_limit_is_recorded_as_a_provider_outage(session_factory) -> None:
    def planner(mission_id: str, goal: str) -> MissionPlan:
        raise LLMRateLimitError("Groq rate limit exceeded (HTTP 429) after 2 retries", provider="groq", model=DEFAULT_MODEL, status_code=429)

    orchestrator = MissionOrchestrator(
        session_factory=session_factory, registry=AgentRegistry(), llm_provider=FixedPlanLLMProvider(plan_factory=planner)
    )
    final = orchestrator.run_mission("goal", user_id="u1", user_role=UserRole.STUDENT)
    assert final["mission_status"] == MissionStatus.FAILED
    meta = next(m for event, _, m in _audit(session_factory, final["mission_id"]) if event == "plan_generation_failed")
    assert meta["kind"] == "rate_limit" and meta["status_code"] == 429


# ---------------------------------------------------------------------------
# 5. Token budgets
# ---------------------------------------------------------------------------


def test_each_operation_has_its_own_output_budget_and_usage_is_recorded() -> None:
    def respond(body: Dict[str, Any]) -> Dict[str, Any]:
        tool = (body.get("tool_choice") or {}).get("function", {}).get("name")
        if tool == "classify_academic_intent":
            return _tool_reply(tool, {"intent": "timetable"}, usage={"completion_tokens": 140})
        if tool == "produce_mission_plan":
            return _tool_reply(tool, {"tasks": [], "unsupported_requests": ["n/a"]}, usage={"completion_tokens": 600})
        return {"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}], "usage": {"completion_tokens": 90}}

    server = Server(respond)
    provider, _ = groq(server)
    provider.classify_academic_intent("timetable?", [CourseSummary(course_code="CS301", title="OS", credits=4, semester=5, instructor="A")])
    provider.plan_mission("m", "goal", supported_agents=[AgentName.ACADEMIC_AGENT])
    provider.generate_career_response(
        CareerResponseContext(intent=CareerIntent.OPPORTUNITY_DISCOVERY, verification_status=VerificationStatus.VERIFIED)
    )

    assert [b["max_completion_tokens"] for b in server.bodies] == [512, 2048, 1024]
    assert provider.stats()["max_completion_tokens_used"] == {
        "classify_academic_intent": 140, "produce_mission_plan": 600, "response": 90,
    }


# ---------------------------------------------------------------------------
# 6. Mock and Anthropic are not throttled
# ---------------------------------------------------------------------------


def test_mock_provider_is_unaffected() -> None:
    mock = MockLLMProvider()
    assert check_live_llm.provider_stats(mock) == {}
    assert not hasattr(mock, "max_concurrency")


class _SlowAnthropicClient:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    @property
    def messages(self):
        return self

    def create(self, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
        time.sleep(0.2)
        with self.lock:
            self.active -= 1

        class _Block:
            type = "tool_use"
            name = kwargs["tools"][0]["name"]
            input = {"intent": "opportunity_discovery"}

        return type("Resp", (), {"content": [_Block()], "stop_reason": "tool_use"})()


def test_anthropic_provider_is_not_throttled_and_keeps_its_errors() -> None:
    client = _SlowAnthropicClient()
    provider = AnthropicLLMProvider(model="claude-sonnet-5", client=client)
    _call_in_threads(lambda: provider.classify_career_intent("q"))
    assert client.peak == 2

    class _Failing:
        messages = property(lambda self: self)

        def create(self, **kwargs):
            raise RuntimeError("429 Too Many Requests")

    with pytest.raises(LLMProviderError) as info:
        AnthropicLLMProvider(model="claude-sonnet-5", client=_Failing()).classify_career_intent("q")
    assert not isinstance(info.value, LLMTransientError)  # unchanged Phase 9 behaviour


# ---------------------------------------------------------------------------
# 7. Live evaluator: --case runs exactly one scenario
# ---------------------------------------------------------------------------


def test_case_runs_only_the_requested_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path, capsys) -> None:
    from tests.test_live_evaluator import PROPOSALS, GoalRoutedPlanner

    class LiveStandIn(GoalRoutedPlanner):
        name = "groq"
        is_live = True

        def __init__(self) -> None:
            super().__init__(PROPOSALS)
            self.goals: List[str] = []

        def plan_mission(self, mission_id, goal, *, supported_agents):
            self.goals.append(goal)
            return super().plan_mission(mission_id, goal, supported_agents=supported_agents)

    stand_in = LiveStandIn()
    monkeypatch.setattr(check_live_llm, "get_llm_provider", lambda name: stand_in)
    out = tmp_path / "live_case_c.json"
    monkeypatch.setattr("sys.argv", ["check_live_llm.py", "--provider", "groq", "--case", "C_unnamed_registration", "--out", str(out)])

    with pytest.raises(SystemExit) as info:
        check_live_llm.main()

    assert info.value.code == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "case" and report["checks"] == []  # no intent/plan checks, no A or B
    assert [run["scenario"] for run in report["e2e"]] == ["C_unnamed_registration"]
    assert stand_in.goals == [check_live_llm.UNNAMED_REGISTRATION_GOAL]
    run = report["e2e"][0]
    assert run["pass"] and run["user_selection_required"] and run["approvals"] == [] and run["tool_calls"] == []
    assert run["replans"] == 0
    assert "1/1 E2E scenarios reached their correct outcome." in capsys.readouterr().out


def test_unknown_case_is_rejected(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("sys.argv", ["check_live_llm.py", "--case", "Z_nope"])
    with pytest.raises(SystemExit) as info:
        check_live_llm.main()
    assert info.value.code == 2
    assert "C_unnamed_registration" in capsys.readouterr().err


def test_evaluator_labels_a_provider_outage_distinctly() -> None:
    record = {
        "mission_status": "failed", "plan": {"tasks": [{"n": 1}], "unsupported_requests": []},
        "approvals": [], "tool_calls": [], "dispatches": [],
        "audit": [{"event_type": "provider_unavailable", "message": "Task t was not run: rate limit", "metadata": {"kind": "rate_limit"}}],
    }
    verdict = check_live_llm.evaluate_e2e(record, "clarification")
    assert not verdict["pass"] and verdict["outcome_label"] == "PROVIDER UNAVAILABLE (rate_limit)"
