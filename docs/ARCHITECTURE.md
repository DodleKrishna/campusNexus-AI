# CampusNexus AI — Architecture

This document expands on the frozen architecture summarized in [CLAUDE.md](../CLAUDE.md). It is reference
material — architectural rules that must be obeyed live in CLAUDE.md; details on *why* and *how* live here.

## System Shape

CampusNexus is a **goal-driven multi-agent system**, not a request/response chatbot. The unit of work is a
**mission**: a student-stated goal that the Mission Orchestrator decomposes into steps, dispatches across
specialist agents, verifies, and (where needed) routes through human approval before any real-world effect.

```
                        ┌─────────────────────┐
                        │   Mission Orchestr.  │  (LangGraph state machine)
                        └──────────┬───────────┘
                                   │ dispatches sub-goals
              ┌────────────┬───────┼───────┬──────────────┬────────────┐
              ▼            ▼       ▼       ▼              ▼            ▼
         Academic       Career   Events  Campus       Knowledge/     (future
          Agent         Agent    Agent   Services      RAG Agent      agents
                                          Agent                        TBD)
              │            │       │       │              │
              └────────────┴───────┴───────┴──────────────┘
                                   │  agent outputs (Pydantic)
                                   ▼
                          ┌──────────────────┐
                          │  Context Service │  (state, history, audit — NOT an agent)
                          └────────┬─────────┘
                                   │
                                   ▼
                       ┌───────────────────────┐
                       │  Deterministic Verifier │  (pre-check: is this plan/action valid?)
                       └───────────┬────────────┘
                                   │ sensitive?
                                   ▼
                          ┌────────────────┐
                          │  Approval Gate │  (human approve / reject / edit)
                          └───────┬────────┘
                                  ▼
                          ┌────────────────┐
                          │  Action Agent  │  (only component that calls side-effecting tools)
                          └───────┬────────┘
                                  ▼
                       ┌───────────────────────┐
                       │  Deterministic Verifier │  (post-check: did the world actually change?)
                       └────────────────────────┘
                                  │
                                  ▼
                         Context Service (audit log entry)
```

## Component Responsibilities

### 1. Mission Orchestrator
- Owns the mission lifecycle: goal → plan → dispatch → collect → verify → respond/replan.
- Implemented as a LangGraph graph; nodes are agent calls, edges are conditioned on Verifier/Approval outcomes.
- Never calls tools directly and never computes deterministic rules itself — it delegates to specialist agents,
  the Verifier, and the Action Agent.
- On Verifier failure or Approval rejection, re-enters planning rather than forcing the original plan through.

### 2. Academic Agent
- Domain: courses, grades, GPA context, degree progress, academic policies/deadlines.
- Reads evidence via the RAG Agent for any policy text; delegates numeric eligibility/GPA math to deterministic
  rule modules (not LLM arithmetic).

### 3. Career Agent
- Domain: internships, jobs, resume/skill matching, employer-facing opportunities.
- Any matching logic that has a "correct" answer (e.g., GPA cutoff, eligibility criteria) is deterministic;
  the LLM's job is ranking/explaining fit, not computing eligibility.

### 4. Events & Opportunity Agent
- Domain: campus events, clubs, scholarships, competitions, deadlines.
- Deadline/quota checks are deterministic; the agent's LLM role is surfacing and summarizing relevant items.

### 5. Campus Services Agent
- Domain: hostel, fees, facilities, IT helpdesk, administrative requests.
- Frequently the agent whose missions terminate in a real side effect (ticket filed, form submitted) — those
  always go through Action Agent + Approval Gate, never direct from this agent.

### 6. Knowledge/RAG Agent
- The only component that queries the vector store (ChromaDB).
- Returns structured citations (doc id, snippet, source, score) — never bare prose — so downstream agents and
  the Verifier can trace any factual claim to its source.
- If nothing relevant is retrieved, it returns an explicit "no evidence found" result; agents must surface that
  rather than fabricating an answer.

### 7. Action Agent
- The **only** component permitted to invoke side-effecting tools (submit form, send email/notification, book
  a resource, file a ticket, write to an external system).
- Every tool call it makes must already have passed the Verifier's pre-check and, if sensitive, the Approval
  Gate. It does not decide on its own whether something is sensitive — that's the Gate's job, driven by a
  declared sensitivity classification per tool.
- All tool calls use idempotency keys derived from the mission-step id so retries are safe.

### 8. Deterministic Verifier
- Plain Python, no LLM involved. Two invocation points per action:
  - **Pre-check**: is this planned action valid against official rules (eligibility, deadline, quota,
    prerequisite, budget)? Blocks dispatch to Action Agent if not.
  - **Post-check**: after execution, did the world state actually reflect the intended change? Blocks the
    mission from being marked complete if not.
- Houses all "official rule" calculations (GPA thresholds, deadline math, quota counts) as pure functions with
  unit tests — this is the system's source of truth for anything numeric/policy-based.

### 9. Approval Gate
- Enforces human-in-the-loop for any action classified sensitive (irreversible, financial, third-party-facing,
  or acting on behalf of the student toward another person/office).
- Records every decision (approve/reject/edit + who + timestamp) to the Context Service audit trail.
- No silent auto-approval on timeout; a pending approval blocks the mission step until resolved.

### 10. Context Service (service, not an agent)
- Single source of truth for mission state, per-agent outputs, approval records, and the audit log.
- Pure state store + accessor API — it does not plan, reason, or call an LLM. Backed by SQLite via SQLAlchemy.
- All agents read/write mission context through this service rather than passing ad hoc state between
  themselves, so the Orchestrator and Verifier always see a consistent view.

## Data Flow Contracts

- Every agent-to-orchestrator and orchestrator-to-agent message is a Pydantic model (see CLAUDE.md's structured
  output requirement). This document doesn't freeze specific schema names yet — those are designed during
  scaffolding, not during this planning phase.
- RAG evidence is passed as a structured citation list, attached to whatever answer depended on it.
- Mission/step identifiers are stable ids used for: Context Service lookups, Verifier pre/post-check pairing,
  Approval Gate records, and Action Agent idempotency keys.

## Technology Stack Rationale

| Concern              | Choice           | Why |
|-----------------------|------------------|-----|
| Agent orchestration   | LangGraph        | Explicit state machine fits mission lifecycle (plan → verify → approve → act → verify) better than a free-form agent loop. |
| API layer             | FastAPI          | Async, Pydantic-native, fast to scaffold for a hackathon-paced build. |
| Structured data       | Pydantic         | Enforces the structured-output requirement at every component boundary. |
| Persistence           | SQLite + SQLAlchemy | Zero-ops relational store, sufficient for mission/audit/approval state at hackathon scale. |
| Vector store           | ChromaDB         | Lightweight local embedding store for the RAG Agent's knowledge base. |
| UI                     | Streamlit        | Fast to stand up a demo UI for missions/approvals without a full frontend build. |
| Testing                | pytest           | Standard, supports both unit tests (deterministic rules) and mocked-integration tests (agent flow). |

This stack is intentionally lightweight for a hackathon-paced build; nothing here precludes swapping components
later (e.g., SQLite → Postgres) as long as the frozen agent boundaries and core principle are preserved.

## Database Implementation (Phase 2)

- SQLite via SQLAlchemy 2.x typed ORM (`app/db/`). No Alembic yet -- schema changes recreate the dev DB by
  re-running `scripts/seed_data.py`, which is an acceptable hackathon-scope tradeoff for now.
- `app/db/models/` groups ORM models by domain (identity, academic, career, events, services, communication,
  mission) mirroring the frozen agent boundaries, but these are **persistence** representations -- they
  intentionally do not duplicate the Phase 1 Pydantic contracts in `app/schemas/`. Cross-boundary agent
  messages still use those Pydantic models; ORM models are what gets written to disk.
- Every table enabling a future deterministic rule (attendance, eligibility, SLA) stores raw source values
  (e.g. `classes_attended`/`classes_conducted`, not a percentage) so the rule computes the answer, never a
  cached one.
- Eligibility-style criteria (opportunity allowed departments/years/required skills) are modeled as join
  tables, not CSV/JSON blobs, so a later rule module can query them directly.
- **Datetime strategy**: the campus operates on India Standard Time (UTC+5:30) for wall-clock scheduling
  (seed data authors instants in IST), but every column is stored as UTC via a `UTCDateTime` TypeDecorator
  (`app/db/base.py`) that rejects naive input and always returns timezone-aware UTC on read. `TimetableSlot`
  is the one exception: it's a recurring weekly slot (weekday + wall-clock time, no calendar date), so it has
  no absolute instant to convert.
- No module-level global engine/session: `app/db/session.py` exposes factory functions
  (`create_db_engine`, `create_session_factory`, `init_db`) that every caller (seed script, Context Service,
  tests) invokes explicitly. Tests always point `CAMPUSNEXUS_DB_PATH` at a temp file, so they never touch the
  dev database.
- The **Context Service** (`app/services/context.py`) is the only place that writes mission/step/agent-run/
  tool-call/approval/audit/memory rows. It has no LLM calls, no planning, no agent routing -- pure typed
  accessor methods, each its own committed transaction. Read-only domain lookups for future specialist agents
  live in `app/db/repositories/` instead, kept separate from the Context Service's mission-lifecycle rules.
- `scripts/seed_data.py` is idempotent: every row is inserted via a `get_or_create` keyed on natural/unique
  fields, so re-running it is a no-op against already-seeded data. It seeds a fictional campus with a stable
  demo student (`STU-DEMO-001`) and deliberately includes edge-case scenarios (attendance shortage, CGPA/
  department/skill-gap/expired opportunities, timetable/exam-conflicting events, an at-capacity event, and an
  SLA-breached case) for later phases' rule and agent logic to exercise.

## Knowledge/RAG Retrieval Infrastructure (Phase 3)

Phase 3 implements the deterministic retrieval half of the future Knowledge/RAG Agent (component 6) as a plain
service -- `KnowledgeService` (`app/services/knowledge.py`) -- backed by the `app/rag/` package. It has no LLM
calls, does not plan missions, does not generate final answers, and never invents a citation: `query -> retrieve
-> filter -> rank -> return structured Evidence`, and an unsupported query returns an empty list rather than a
guess. The future Knowledge/RAG Agent wraps this service with LLM query understanding/summarization in a later
phase; this phase only builds the infrastructure it will call.

**Pipeline**: `app/rag/documents.py` parses each Markdown file under `data/policies/` (YAML front matter +
`##`-delimited sections) into a `PolicyDocument`. `app/rag/chunking.py` chunks on section boundaries first, only
falling back to paragraph-boundary splitting when a section exceeds `MAX_CHUNK_CHARS` -- a short coherent
section is never chopped. `app/rag/embeddings.py` defines a swappable `EmbeddingProvider`: `DeterministicHashEmbedding`
(offline hashing-trick bag-of-words, no model download, used by the entire test suite and as a safe fallback) and
`OnnxMiniLMEmbedding` (chromadb's bundled local ONNX all-MiniLM-L6-v2 -- the production/demo default; real
semantic embeddings, no external API calls after a one-time model download). `app/rag/vector_store.py` wraps a
single Chroma collection (`PolicyVectorStore`); `upsert_chunks` is keyed on a deterministic `chunk_id`
(`{document_id}::chunk::{index}`), so re-running ingestion (`app/rag/ingest.py`, `scripts/ingest_policies.py`)
never duplicates rows -- it replaces existing chunks in place. `app/rag/retriever.py` (`PolicyRetriever`) ranks
Chroma's semantic hits with a hybrid score (0.7 semantic + 0.3 keyword-overlap, both computed over the same
stopword-filtered tokenizer), requires at least one shared content word before a hit is scored at all, and drops
anything under `DEFAULT_MIN_SCORE` -- together this is what makes an off-topic query resolve to *no evidence*
instead of padding the top-k with noise. `KnowledgeService` is the only place this converts to the Phase 1
`Evidence` schema; `evidence.snippet` is always the verbatim retrieved chunk text, never a paraphrase.

**Active vs. historical policy**: every chunk carries `policy_version`, `effective_from`, and `effective_to`.
`RetrievalQuery.as_of` filters to the version whose window covers that date (`effective_from <= as_of <=
effective_to`, with `effective_to = None` meaning still active); omitting `as_of` disables date filtering
entirely, and an explicit `document_id` lookup always bypasses it, so a superseded version stays retrievable for
audit purposes -- nothing is ever deleted. The attendance policy corpus deliberately ships two versions
(`attendance-policy-v1`, 70% minimum, effective 2023-08-01 to 2025-06-30; `attendance-policy-v2`, 75% minimum,
effective 2025-07-01 onward) to exercise this.

**Visibility**: every document declares `visibility` (`public` or `admin_only`) and `audience`; callers pass
their own `visibility`/`audience` explicitly (no auth/RBAC yet -- that's a later phase), and `PolicyRetriever`
filters before results ever reach `Evidence`. `data/policies/internal_case_escalation_sop.md` is the seeded
`admin_only` document exercising this.

**Deliberate conflict scenario**: `data/policies/event_policy.md` (general, 24-hour registration window) and
`data/policies/cse_department_event_circular.md` (CSE-specific, 48-hour window, `department: CSE`) describe the
same procedure differently. Retrieval does not resolve this -- a department-unscoped query returns both
documents' evidence side by side, preserving the metadata (`department`, `policy_version`, `source`) a future
Deterministic Verifier needs to flag it `NEEDS_REVIEW` rather than silently picking one.

**Evaluation**: `eval/rag_scenarios.json` (16 scenarios: per-domain retrieval, current/historical attendance,
no-evidence, visibility filtering, conflict preservation) is run by `eval/run_rag_eval.py`, a deterministic
PASS/FAIL checker with no LLM judge, against document-id membership (`expected_document_ids` /
`forbidden_document_ids` / `expect_empty`).

## Academic Agent Vertical Slice (Phase 4)

Phase 4 builds the first complete specialist-agent path end-to-end -- natural-language request -> structured
intent -> DB facts -> RAG evidence -> deterministic rule -> verified `AgentResult` -> grounded response -- for
the Academic Agent (component 2) only, against the seeded demo student. No Orchestrator, no LangGraph, no
other specialist agent, no write tools: those stay deferred to later phases.

**Responsibility split** (the concrete instance of CLAUDE.md's Core Principle for this slice):

- **LLM** (`app/llm/`): classifies the query into an `AcademicIntent`, proposes a raw course-reference
  substring, and -- only after everything else below has run -- renders the final natural-language response
  from an already-verified `AcademicResponseContext`. It never computes a percentage, a threshold, or an
  eligibility decision, and it never asserts a course exists on its own say-so.
- **Deterministic rules** (`app/rules/`): `attendance.py` computes attendance percentage/eligibility/recovery
  math from raw `classes_attended`/`classes_conducted` counters using `Decimal` closed-form algebra (no
  iteration, no float); `policy_threshold.py` extracts the official numeric threshold from retrieved policy
  `Evidence`; `eligibility.py` derives attendance-only exam eligibility from an `AttendanceCalculation`.
- **AcademicVerifier** (`app/verification/academic.py`): does not recompute the rules independently (that
  would just be a second implementation to go stale); it checks that the right inputs were used (student
  exists, course resolved, evidence present, threshold unambiguous) and re-derives only the core eligibility
  inequality directly from the raw counters as a genuine internal-consistency check.

**Course resolution** (`app/agents/academic/course_resolution.py`) is entirely deterministic: exact course
code/title/title-acronym match first, then a substring-on-title match (only for references of 4+ characters,
to avoid over-matching), and an LLM-proposed reference matching more than one enrolled course's title (e.g.
"Systems" against both "Operating Systems" and "Database Management Systems") resolves to `AMBIGUOUS`, never a
guess.

**Threshold extraction** (`app/rules/policy_threshold.py`) is a validated parser tied to the corpus's known
structure, not an LLM interpreting prose: it looks only inside the `"Minimum Attendance Requirement"` section
of retrieved `Evidence` and regex-extracts the `NN%` figure, traceable to `document_id`/`policy_version`/
`section`/`effective_from`. No matching section, or active evidence disagreeing on the number, yields
`NOT_FOUND`/`AMBIGUOUS` -- the agent then skips the attendance calculation entirely rather than falling back to
a hardcoded percentage.

**LLM abstraction** (`app/llm/`): `LLMProvider` is a two-method ABC (`classify_academic_intent`,
`generate_academic_response`) the Academic Agent depends on, never a provider SDK directly. `MockLLMProvider`
(`providers/mock.py`) is deterministic keyword/regex logic with no network or model -- used by the entire test
suite, `eval/run_academic_eval.py`, and the demo runner's default mode, per CLAUDE.md's requirement that tests
never call a live LLM. `AnthropicLLMProvider` (`providers/anthropic_provider.py`) is the one real integration,
chosen because `.env.example` already reserved `ANTHROPIC_API_KEY`; it lazily imports the `anthropic` package
(an optional `llm` dependency extra) so nothing else in the app ever requires it installed, uses tool-calling
with a JSON schema built from `AcademicIntentResult.model_json_schema()` for structured intent output (never
regex-parses free text), and constrains response generation to the verified JSON context it's given.

**Known gaps, called out rather than hidden:**

- Exam eligibility is attendance-only. `exam_regulations.md` also conditions eligibility on cleared fee dues,
  but fee data belongs to the (not-yet-built) Campus Services Agent; `ExamEligibilityResult.caveat` says this
  explicitly rather than the result silently overclaiming full eligibility.
- Two of the Phase 4 spec's sixteen required test scenarios have no live path through the current seeded data
  without touching the already-approved Phase 2/3 baseline, so they're unit-test-only instead of live eval
  scenarios: "exactly at threshold" (no seeded course sits at exactly 75%) is covered by
  `tests/test_attendance_rule.py::test_exactly_at_threshold_is_eligible`; "conflicting/ambiguous policy
  evidence" (the seeded attendance corpus's two versions have contiguous, non-overlapping effective windows, so
  there's no live conflict) is covered by
  `tests/test_policy_threshold.py::test_ambiguous_threshold_needs_review` against synthetic `Evidence`. See
  `eval/run_academic_eval.py`'s module docstring.

## Mission Orchestrator (Phase 5)

Phase 5 builds the Mission Orchestrator (component 1) as an explicit LangGraph state machine (`app/graph/`)
that turns a free-form student goal into a validated, dependency-aware `MissionPlan` (Phase 1 schema, unused
until now) and executes it against a registry of specialist agents -- today, only the Academic Agent
(component 2), integrated with **zero changes** to its Phase 4 implementation.

**LangGraph state** (`app/graph/state.py`): a plain `TypedDict` (`OrchestratorState`), not LangGraph's
`Annotated`-reducer machinery -- every node returns the *full* new value for whichever keys it updates (built
from the current state), which keeps each node's behavior simple to reason about and unit-test in isolation
from the compiled graph.

**Mission lifecycle / graph shape**: `load_context -> generate_plan -> validate_plan -> schedule_ready_tasks ->
dispatch_and_collect -> update_mission_state -> (replan | schedule_ready_tasks | finalize)`. Conditional edges
(plain Python functions returning the next node name) implement: an invalid plan routes straight to
`finalize(FAILED)`; a task verification of `NEEDS_REVIEW` sets the mission to `NEEDS_APPROVAL` and the run
ends *paused* -- never auto-approved; a `FAILED` task with replan budget left routes to `replan` (which
re-invokes the LLM planner and resets only the failed task(s) back to `PENDING`, never silently re-running
already-`COMPLETED`/`BLOCKED` work) and back through `validate_plan`; budget exhausted routes to
`finalize(FAILED)`; all tasks terminal (`COMPLETED`/`FAILED`/`SKIPPED`) routes to `finalize(COMPLETED)`;
otherwise the graph loops back to `schedule_ready_tasks` for the next round (needed whenever a task's
dependency has only just completed). `TaskStatus`/`MissionStatus`/`VerificationStatus` (all Phase 1, frozen)
are reused as-is -- no new enum values: `VERIFIED -> COMPLETED`, `NEEDS_REVIEW -> BLOCKED`, `FAILED -> FAILED`.

**Planning** (`app/graph/planner.py`): a thin call-site over the *same* `LLMProvider` abstraction the Academic
Agent uses (`app/llm/base.py` gained one additive method, `plan_mission`) -- not a second, parallel LLM
abstraction. `MockLLMProvider.plan_mission` is deterministic: it splits a compound goal on `", and "/", "/"
and "`, and carries a shared "subject" (a capitalized phrase or acronym mentioned once, e.g. a course name)
into clauses that omit it, so each resulting task objective is independently resolvable. `AnthropicLLMProvider.plan_mission`
uses tool-calling against a small index-referenced task schema (dependencies by index, not by
self-invented string id) and converts it into a real `MissionPlan` with generated task ids.

**DAG validation** (`app/graph/validator.py`): deterministic, no LLM. Checks unique task ids, valid agent
assignments (against the registry), valid/no-dangling dependency references, no cycles and no unreachable
tasks (one Kahn's-algorithm pass covers both), a matching mission id, and a minimal objective-quality check.
Self-dependency is *not* re-checked here -- `MissionTask`'s own Pydantic validator (Phase 1) already makes it
impossible to construct one, so a second check would be dead code, not defense in depth. An invalid plan is
never dispatched.

**Agent registry** (`app/graph/registry.py`): maps `AgentName -> Callable[[Session], SpecialistAgent]` --
factories, not instances. `app/graph/results.py` defines `AgentOutcome`/`SpecialistAgent` as structural
`typing.Protocol`s (`agent_result`/`verification`/`response_text`; `handle(AgentMessage) -> AgentOutcome`) that
`AcademicAgent`/`AcademicAgentOutcome` already satisfy without modification. Only `AgentName.ACADEMIC_AGENT` is
registered in production; Career/Events/Campus Services/Knowledge-RAG/Action are added later by one
`register()` call each, never by editing orchestration logic. An unregistered assignment raises
`UnsupportedAgentError` at dispatch (defense in depth -- the validator is the primary gate).

**Task scheduling** (`app/graph/scheduler.py`): pure functions over a `MissionPlan` + the current
`Dict[task_id, TaskStatus]`, no I/O. `compute_ready_and_blocked` returns every `PENDING` task whose
dependencies are all `COMPLETED` as "ready" (so independent tasks all become ready together, in one round --
this is what makes the from-the-real-demo-goal 3-task plan genuinely parallel-eligible, not just a synthetic
test fixture), and cascades `SKIPPED` to any `PENDING` task with a `FAILED`/`SKIPPED` dependency, repeated to a
fixed point so the cascade propagates through chains of dependents -- a failed prerequisite can never silently
let a dependent run. A `BLOCKED` (needs-review) dependency is different: it's a pause, not a failure, so its
dependents simply wait rather than being skipped. **Dispatch** (`app/graph/dispatcher.py`) then runs every
ready task *concurrently* via a `ThreadPoolExecutor`, each against its own freshly-opened `Session` (SQLAlchemy's
`Session` is not thread-safe, so sharing one across concurrent agent calls would be a real bug) -- this is what
makes "parallel execution" of independent tasks genuinely true rather than nominal.

**Checkpointing vs. persistence** -- the split the spec specifically asks to be documented:

- LangGraph's own checkpointer (`app/graph/checkpoint.py`, `InMemorySaver`, `thread_id = mission_id`) gives the
  compiled graph *real* LangGraph checkpoint semantics (`get_state`/`update_state`, resuming a paused `invoke`
  within the same process). It is intentionally **not** the durable source of truth -- it lives in process
  memory and is lost on restart.
- The **Context Service** (`app/services/context.py`, SQLAlchemy/SQLite, unchanged Phase 2 tables) remains the
  single source of truth across process restarts, per its frozen component-10 role. Every orchestrator node
  writes through it: `Mission`/`MissionStep` status, `AgentRun` (with the agent's free-form `facts` JSON blob
  additionally carrying a `_response_text` key -- the one piece of per-task data with nowhere else to live in
  the existing schema, consistent with that column's documented "no fixed relational shape" purpose), and an
  append-only `AuditLog` (`mission_created`/`mission_resumed`, `plan_generated` -- which snapshots the *entire
  validated `MissionPlan`* as JSON, since Phase 2 has no dedicated plan table -- `plan_invalid`, `task_verified`,
  `task_failed`, `task_skipped`, `replan_triggered`, `mission_finalized`).
- `MissionOrchestrator.resume_mission(mission_id)` reconstructs state **entirely from the Context Service**,
  never from a prior LangGraph checkpoint: it reads the `Mission` row and its `MissionStep`s (giving
  `task_status` straight from the already-persisted, already-frozen `TaskStatus` enum), the most recent
  `plan_generated` audit event (`ContextService.get_latest_plan_snapshot`, additive), and every `AgentRun`
  (`ContextService.list_agent_runs`, additive) to rebuild best-effort `AgentResult`/`VerificationResult`
  objects for already-completed tasks. This is what lets a **brand-new** `MissionOrchestrator` instance (a
  fresh in-memory checkpointer, no shared state with whatever process ran the mission before) resume correctly
  -- proven in `tests/test_orchestrator_persistence.py` by constructing a genuinely-interrupted mid-DAG state
  directly via the Context Service and confirming resumption completes the remaining task without re-dispatching
  the already-completed one or re-invoking the planner. One known limitation: `AgentRun` doesn't persist the
  original `Evidence` list or the verifier's named checks, so a resumed mission's *pre-interruption* steps show
  reconstructed (status-only) verification, not the original rich object -- a natural extension for whichever
  later phase needs it, not required for correct scheduling/resumption behavior.

**Failure handling**: invalid plan / unsupported agent / missing or circular dependency all fail at validation,
before any task is ever dispatched. An agent-call exception is caught by the dispatcher and turned into that
task's `FAILED` status rather than crashing the mission run. Verification is never inferred from "the call
didn't raise" -- the Orchestrator routes purely on `VerificationResult.status`, so a successful-looking
`AgentResultStatus.SUCCESS` call with a `FAILED` verification still fails the mission
(`tests/test_orchestrator.py::test_successful_invocation_is_not_treated_as_verified`). Replanning is bounded
(`max_replans`, default 2) and re-invokes the LLM planner rather than blindly re-running the identical failed
step; every specialist agent registered in this phase is read-only and deterministic, so retrying the same
input deterministically fails again -- the bound is exercised honestly with a deterministic always-failing
**test** agent (`tests/graph_doubles.py`, registered only in tests/eval, never in production) rather than
faked against the real Academic Agent. Execution states already distinguish `IN_PROGRESS` from `BLOCKED`
(needs-review, pending future human approval) from `FAILED`, which is deliberate: this is the seam a future
Approval Gate and Action Agent plug into without another redesign, even though neither exists yet in this phase.

## Multi-Agent Collaboration (Phase 6)

Phase 6 adds three more read-only specialists -- Career (component 3), Events & Opportunity (component 4),
Campus Services (component 5) -- and the one genuinely new Orchestrator capability needed to make them
collaborate: a downstream task can see an upstream task's structured output when the plan declares a
dependency between them.

**Each new agent follows the Academic Agent's template exactly** (`app/agents/{career,events,services}/agent.py`):
LLM classifies intent (`app/llm/base.py` gained one `classify_*_intent`/`generate_*_response` method pair per
agent, on the same `LLMProvider` interface -- not a second abstraction) → deterministic service/rule calls →
the agent's own `*Verifier` → a local `*AgentOutcome` dataclass that structurally satisfies
`app.graph.results.AgentOutcome`, exactly like `AcademicAgentOutcome`, so registering them
(`app/graph/registry.py`) required no Orchestrator changes.

- **Career Agent** (`app/rules/opportunity_eligibility.py`, `app/services/career.py`): deterministic eligibility
  against status/deadline/year/department/CGPA and every *mandatory* required skill. "Mandatory vs.
  recommended" reuses `OpportunitySkill.minimum_proficiency`'s existing `Optional[int]` as-is -- non-`None` is
  mandatory, `None` is recommended and never blocks eligibility, so no schema or seed-data change was needed;
  every already-seeded skill requirement specifies a number and stays mandatory, preserving every documented
  Phase 2 eligibility outcome unchanged. `skill_gaps` (surfaced to a dependent task, see below) is deliberately
  scoped to opportunities *within reach* -- eligible ones, or ones blocked by nothing except a missing skill --
  so a department-blocked opportunity's skill requirements never pollute the "what should I learn" list.
- **Events & Opportunity Agent** (`app/rules/event_availability.py`, `app/services/events.py`): availability
  (open/full/deadline-closed) is a separate, never-conflated dimension from schedule conflicts -- an event can
  be available to register *and* conflict with a class. Timetable conflicts convert the event's absolute UTC
  instant to IST wall-clock (the existing `IST = UTC+5:30` convention) and compare against the recurring
  `TimetableSlot`; exam conflicts compare two absolute UTC ranges directly. Relevance matching (which upcoming
  events are even worth assessing) is deterministic keyword overlap between the task's own query text and any
  upstream `skill_gaps` against each event's title/description/category -- never an LLM judgment call, so an
  event is never claimed relevant without a traceable shared keyword.
- **Campus Services Agent** (`app/rules/sla.py`, `app/services/services.py`): `CaseSLA.response_due_at`/
  `resolution_due_at` are already pre-computed absolute timestamps at seed time from the Grievance SLA Policy's
  numeric windows, so the rule is a direct comparison, no policy-prose parsing needed. A breach is checked
  against the actual response/resolution timestamp when one exists (so a case resolved *late* still counts as
  breached) or against "now" when it doesn't.

**Dependency-aware information sharing** (`app/graph/dispatcher.py`, `app/graph/orchestrator.py`) is the one
concrete Orchestrator modification this phase makes: `_build_message` now merges each dependency's already-
`COMPLETED` `AgentResult.facts` into a dispatched task's `AgentMessage.facts`, in addition to the existing
`student_id`/`as_of` base facts. This is safe by construction, not by a new runtime check -- a task only
becomes dispatchable once every dependency has reached `COMPLETED` (the Phase 5 scheduler's existing
invariant), so `agent_results` is guaranteed to already hold each dependency's real result; nothing is ever
guessed. A second, small fix made while touching this code: `_summarize` now orders a synthesized multi-task
response by the plan's declared task sequence instead of a lexical sort of task ids (which could misorder past
task 9), covered by `tests/test_multiagent_orchestration.py`.

**The flagship mission** ("Find internships I'm eligible for, identify my skill gaps, find relevant workshops
that don't conflict with my classes...") is planned as four tasks: two independent Academic Agent reads
(timetable, exam schedule), one independent Career Agent opportunity/skill-gap discovery, and one Events Agent
workshop search that depends on all three -- so round 1 dispatches three tasks in genuine parallel
(`ThreadPoolExecutor`, one `Session` each) and round 2 dispatches the Events task only once all three are
verified `COMPLETED`. Its relevance matching keys off the *union* of its own query keywords ("AI") and the
upstream `skill_gaps` -- both signals matter: "AI" alone finds the AI/ML workshop the skill gaps (Docker/
Kubernetes/AWS, from the demo student's actual gap) wouldn't lexically match, and the skill gaps separately
surface a "Cloud Native Systems" talk the query text alone wouldn't. `MockLLMProvider.plan_mission` recognizes
this goal shape (and the campus-services goal shape) via keyword pattern-matching and returns this exact DAG
directly -- a second planning path added *alongside*, never replacing, the existing generic clause-splitting
path, so plain academic-only goals are unaffected
(`tests/test_multiagent_orchestration.py::test_existing_academic_only_mission_is_unaffected_by_new_agents`).

**A real bug found and fixed while building this phase**: none of the agents' `KnowledgeService.search()`/
`get_active_policy()` calls -- including the already-approved Phase 4 Academic Agent's -- passed
`visibility="public"`, so an `admin_only` document (the internal case-escalation SOP) could leak into a
student-facing response if a query happened to be semantically close to it (confirmed happening for a Campus
Services case-status query before the fix). Fixed at all eight call sites across all four agents; every agent
test now explicitly asserts the admin-only document never appears in a student-facing result.

**Missing upstream results are never fabricated**: if the Career task fails (e.g. an unknown student), the
scheduler's existing failure-cascade (Phase 5) marks the dependent Events task `SKIPPED` -- it is never
dispatched, so it never has the chance to guess at `skill_gaps` it didn't receive
(`tests/test_multiagent_orchestration.py::test_missing_upstream_result_blocks_the_dependent_task`).

## Action Agent, Approval Gate & Verified Execution (Phase 7)

Phase 7 turns CampusNexus from a read-only recommendation system into a controlled action-execution platform:
the Action Agent (component 7), a real Tool Gateway, the Approval Gate (component 9), and independent
post-condition verification, wired onto persistence (`ApprovalRecord`/`ToolCallRecord`/`CalendarEvent`) that
Phase 1/2 had already reserved but never used. Three write tools are implemented: `register_event`,
`create_calendar_event`, `create_campus_case`.

**Every write action requires approval in this phase** -- there is no "trusted agent" bypass and no
sensitivity classification to opt out of it; CLAUDE.md's Approval Gate rule (irreversible/financial/
third-party/on-behalf-of-student actions) is satisfied by simply requiring it unconditionally for all three
tools, kept deliberately simple for this phase.

### Execution lifecycle: proposal -> pre-check -> approval -> execution -> post-check

1. **Action proposal** (`app/agents/action/agent.py`, `app/schemas/action.py::ActionProposal`): the Action
   Agent reads its plan `constraints` (tool name + target resource, e.g. an event title) and any verified
   upstream facts a dependency already gathered (e.g. an Events Agent's schedule-conflict assessment), and
   drafts a proposal: tool, target, validated parameters, supporting facts, policy evidence, and a
   deterministic user-visible description (never LLM-generated -- see "Design choices" below).
2. **Pre-action verification** (`app/rules/action_preconditions.py` + `app/verification/action.py::ActionVerifier`):
   deterministic checks (student exists, event exists/open/not-full/deadline-not-passed/not-already-registered;
   calendar duplicate/overlap/referenced-event-exists; case category/priority/department validity). A hard
   failure (e.g. already registered, at capacity) stops here -- **no approval is ever created for a proposal
   that fails outright**, per CLAUDE.md's "never accept an unverified recommendation as authorization to
   execute". A few checks are "soft" (couldn't confirm a schedule-conflict check ran, or a conflict/overlap
   was found) and downgrade the result to NEEDS_REVIEW instead, which still proceeds to approval -- the human
   approver sees the concern in the proposal, since every action requires approval anyway.
3. **Human approval** (`app/services/approval_gate.py::ApprovalGate`): a `PENDING` `ApprovalRecord` is
   persisted, referencing the exact validated `ToolCallRecord` (also `PENDING`). `ApprovalGate.decide(...)` is
   the one and only way to resolve it -- a **separate, explicit operation**, never invoked automatically by
   mission execution. Deciding an already-resolved approval raises rather than silently re-applying. Editing an
   approved-but-not-yet-executed action never mutates it in place: `ApprovalGate.mark_superseded` flips the old
   record to `EDIT_REQUIRED` and `ActionAgent.propose_edit` builds a brand-new proposal/approval pair (a new,
   versioned idempotency key) from the edited parameters, which itself goes through pre-action verification
   again before a human can approve it.
4. **Execution** (`app/tools/registry.py::ToolGateway` + `app/tools/{event,calendar,case}_tools.py`): only once
   an `ApprovalRecord` is `APPROVED` does the Action Agent recheck preconditions against *current* DB state
   (capacity/duplicate can have changed since the first check) and call `ToolGateway.execute`, which itself
   re-validates role/ownership/argument-schema before ever touching a handler.
5. **Post-condition verification** (`ActionVerifier.verify_post_action`, the first real use of
   `VerificationPhase.POST_ACTION`): independently re-reads the DB (a fresh `EventRegistration`/
   `CalendarEvent`/`CampusCase` query) rather than trusting the tool's own return value. A tool call that
   "returned success" but didn't actually persist the expected row is never marked verified --
   `ActionAgent.verify_claimed_result` is the reusable entry point for this check, exercised directly in
   `scripts/demo_actions.py`'s Demo D7 against a fabricated result.

### Reusing existing statuses -- no new enum values

The spec's lifecycle names (`WAITING_FOR_APPROVAL`/`APPROVED`/`REJECTED`/`EXECUTING`/`VERIFIED COMPLETION`/
`NEEDS_REVIEW`/`FAILED`) all map onto enum values that already existed before this phase:

| Lifecycle concept | Mechanism |
|---|---|
| WAITING_FOR_APPROVAL | `TaskStatus.BLOCKED` + `MissionStatus.NEEDS_APPROVAL` + `ApprovalStatus.PENDING` -- the *same* pause the Orchestrator already uses for any specialist's NEEDS_REVIEW result (Phase 5/6) |
| APPROVED / REJECTED | `ApprovalStatus.APPROVED` / `REJECTED` |
| EXECUTING | `TaskStatus.IN_PROGRESS`, already set by `_dispatch_and_collect` before every agent call |
| VERIFIED COMPLETION | `TaskStatus.COMPLETED` + `VerificationStatus.VERIFIED` |
| FAILED | `TaskStatus.FAILED` / `VerificationStatus.FAILED` / `MissionStatus.FAILED` |

Concretely, `ActionAgent.handle()` is **dispatched twice** per approved action -- exactly like any other
`SpecialistAgent.handle(AgentMessage) -> AgentOutcome`, no new orchestrator concept needed. First dispatch:
propose + pre-check; if it doesn't fail outright, persist `PENDING` records and return `NEEDS_REVIEW`, which
the existing (unmodified) scheduler turns into `BLOCKED`/`NEEDS_APPROVAL` and the mission run ends paused.
`ApprovalGate.decide(APPROVED)` flips the `MissionStep` back to `PENDING`; `ApprovalGate.decide(REJECTED)`
flips it to `FAILED` (the scheduler's pre-existing SKIPPED-cascade guarantees a `FAILED` step is never
re-dispatched -- a rejected action structurally can never execute). Calling the **existing, unmodified**
`MissionOrchestrator.resume_mission(mission_id)` re-enters the graph; the now-`PENDING` step becomes ready
again and gets re-dispatched -- second dispatch: recheck, execute, post-check, return VERIFIED/FAILED, which
the existing `_VERIFICATION_TO_TASK_STATUS` map turns into `COMPLETED`/`FAILED`.

**This is why `app/graph/orchestrator.py`, `scheduler.py`, `validator.py`, `state.py`, `checkpoint.py`,
`results.py`, and `registry.py` needed zero changes.** `resume_mission` already reconstructs state purely from
the Context Service, so cross-process approval + resumption (a brand-new `MissionOrchestrator` instance, no
shared state with whatever ran the mission before -- `tests/test_action_orchestration.py::test_cross_process_approval_and_resumption`)
works for free, the same way Phase 5 already proved for a plain interrupted mission. The one necessary,
purely-additive change is a single line in `app/graph/dispatcher.py::_build_message`, which now forwards
`MissionTask.constraints` into `AgentMessage.constraints` (a Phase 1 field no agent had ever read until now)
-- the Action Agent's structured tool-target input.

### Idempotency and transactions

- Every write `ToolCall` carries `idempotency_key = "{mission_id}:{task_id}:{tool_name}:v{n}"` (`n` increments
  on `propose_edit`, so an edited proposal's key never collides with the superseded one's).
- `ToolGateway.execute` short-circuits on a prior `SUCCESS` `ToolCallRecord` with the same key -- a retried
  execute call is replayed, never re-executed.
- `register_event`/`create_calendar_event` additionally dedup on a natural key (an existing
  `EventRegistration`/matching `CalendarEvent` row) independent of the idempotency key, so even a *different*
  mission step somehow re-registering the same (event, student) pair hits `EventRegistration`'s own
  `UniqueConstraint` and returns the existing row rather than erroring or duplicating.
- `create_campus_case` has no natural dedup key (two similar-looking complaints are still two distinct
  real-world events) -- its idempotency rests entirely on the Tool Gateway's key check.
- Every handler recommits inside its own transaction and catches its table's `IntegrityError` race (rollback
  + re-query the now-existing row) rather than letting a concurrent duplicate attempt crash.

### Security -- current prototype trust boundary

There is no real authentication in this phase. `ToolGateway.execute(..., caller_role, caller_student_id)`
enforces role authorization and an ownership check (`arguments.student_id == caller_student_id`) **in the
gateway itself**, not only via an LLM prompt -- but `caller_role`/`caller_student_id` are supplied by whatever
calls `run_mission`/`ActionAgent` directly, and are trusted only as far as that caller is trusted. A real
deployment needs an authentication layer upstream of the Orchestrator that derives these from a verified
session, not a caller-supplied argument. The "simulated admin identity" used to approve/reject in
`scripts/demo_actions.py` (a plain string, e.g. `"admin-demo"`) is explicitly a stand-in, not a role-checked
principal -- `ApprovalGate.decide` records *who claimed to* decide, not a verified identity. The Phase 6
Knowledge/RAG visibility fix (agents always pass `visibility="public"` themselves, never take it from
caller-supplied data) is unchanged and still applies to the Action Agent's own policy-evidence lookups.

### Known limitations

- `create_campus_case` generates a non-sequential `case_code` (`CASE-{uuid4 hex}`), unlike the seeded
  `CASE-000N` demo data -- deliberate, to stay race-free without a shared counter; not a scheme new cases are
  expected to match.
- A newly-created `CampusCase` has no `CaseSLA` row (SLA tracking setup is out of this phase's scope), which
  the existing, unmodified `ServicesVerifier` correctly flags `NEEDS_REVIEW` if a *later* Campus Services read
  mission looks at that student's cases. This is why the case-creation mock-planner mission
  (`app/llm/providers/mock.py::_build_case_creation_plan`) is a standalone Action Agent task rather than
  chained after a Campus Services gather task -- chaining would make one action's approval-pause depend on how
  many *other* cases the student happens to already have, which is real but orthogonal.
- ~~Schedule-conflict data for a registration proposal is read from the upstream Events Agent's already-computed
  assessment (reused, not re-derived) rather than the Action Agent independently re-running conflict detection
  against raw timetable/exam data~~ -- **closed in Phase 8 §11**: the execute-time recheck now independently
  re-derives conflicts from the student's current timetable/exams. This remains true only for the *propose-time*
  precheck, which still reuses the upstream assessment (cheap, and re-verified for real immediately before commit).
- ~~No FastAPI/Streamlit UI exists yet for reviewing/approving pending actions~~ -- **closed in Phase 8**: see
  below.

## CampusNexus Application: FastAPI + Streamlit (Phase 8)

Phase 8 adds the first application surface -- a FastAPI backend (`app/api/`) and a Streamlit demo UI
(`streamlit_app/`) -- over the multi-agent backend Phases 1-7 already built. Neither layer reimplements agent,
rule, or verification logic: every route/screen composes existing `app.services`/`app.db.repositories`/
`app.graph`/`app.tools` calls. This is a presentation layer, not a new component -- it sits outside CLAUDE.md's
frozen 10-component list by design.

### FastAPI (`app/api/`)

- **Factory pattern**: `app/api/main.py::create_app(session_factory, knowledge_service, llm_provider,
  tool_gateway=None)` builds a fully-wired `FastAPI` app -- the same `MissionOrchestrator`/`AgentRegistry`
  wiring every demo script already does, stored on `app.state` and read back through per-request FastAPI
  dependencies (`app/api/deps.py`). The module-level `app` (for `uvicorn app.api.main:app`) is just
  `create_app(...)` called with the real dev DB/Chroma store/`CAMPUSNEXUS_LLM_PROVIDER`; tests call
  `create_app(...)` directly with an isolated temp DB and a fresh `MockLLMProvider`, so the exact same routers/
  dependencies/authorization logic run against test state -- nothing is mocked at the route level.
- **Demo identity & trust boundary** (`app/api/identities.py`, spec section 3): `DEMO_IDENTITIES` is a fixed,
  small, server-side dict (`student-demo`/`student-alt` backed by real seeded `Student` rows with a live-resolved
  display name; `faculty-demo`/`admin-demo` backed by nothing but a constant label, exactly Phase 7's
  `"admin-demo"` approver-string pattern formalized). A request identifies itself only via the opaque
  `X-Demo-Identity` header; `app/api/deps.py::get_identity` looks that key up and is the *only* place a
  request's role/student_id is ever established -- no endpoint accepts either as a trusted client-supplied
  value, and `require_role`/`require_student_ownership` enforce them on every relevant route. **This is
  explicitly a local-only demo convenience, not authentication.** A real deployment needs a genuine
  authentication/SSO layer upstream of the Orchestrator that derives a verified identity from a real session
  -- there is no password, token, or session concept here at all, and this app must never be exposed publicly
  as-is.
- **Two independent authorization layers**: the API boundary checks role/ownership *before* ever calling into
  the Orchestrator/Action Agent; the Phase 7 `ToolGateway` still separately checks role/ownership again inside
  `ActionAgent._execute` before any write. Removing either layer would still leave the other standing -- this
  is deliberate defense in depth, not redundancy to be trimmed.
- **Mission endpoints** (`app/api/routers/missions.py`) are thin wrappers around
  `MissionOrchestrator.run_mission`/`resume_mission` -- both already-synchronous, already-blocking calls (as
  everywhere else in this codebase); FastAPI runs a plain `def` route in its threadpool automatically, so no
  background job queue was needed for a demo-scale app. `POST /missions` forces a STUDENT caller's mission onto
  their own `student_id` (a mismatched explicit value is a 400, not silently overridden) and requires
  ADMIN/FACULTY callers to name one explicitly (staff-assisting-a-student case).
- **Evidence persistence gap closed**: `AgentRun` (Phase 1) never persisted `AgentResult.evidence` (a
  documented Phase 5 limitation -- a resumed mission's pre-interruption steps had no retrievable citations).
  Phase 8 adds an additive `evidence` JSON column, threads it through
  `ContextService.record_agent_result(evidence=...)`, and the one call site in
  `orchestrator.py::_dispatch_and_collect` now passes `outcome.agent_result.evidence` through (and
  `_rebuild_results`, the resume path, reads it back) -- this is what makes `GET /missions/{id}/evidence` and
  the Action Center's "supporting evidence" real, not reconstructed or approximated.
- **Timeline mapping** (`app/api/timeline.py::build_timeline`): a pure function merging two kinds of *real*
  persisted events -- each `MissionStep.started_at` (already written by the Orchestrator before every
  dispatch) becomes a RUNNING entry, and every `AuditLog` row maps deterministically (via its `event_type`,
  and for `task_verified`/`mission_finalized` the status embedded in the message text the Orchestrator already
  writes) onto the spec's PLANNING/RUNNING/VERIFYING/WAITING_FOR_APPROVAL/COMPLETED/NEEDS_REVIEW/FAILED
  vocabulary. Nothing is fabricated -- there is no "VERIFYING" *event* recorded today (verification completes
  before an event is ever appended), so that state is inferred only from the checks a caller can already see
  (`task_verified`'s embedded status), never invented with a synthetic timestamp.
- **Dashboard composition** (`app/api/dashboard.py`): calls `app.services.{academic,career,events,services}`
  and `app/db/repositories/calendar.py` exactly as the corresponding agents do. The one real computation here
  (attendance percentage/eligibility) reuses the *exact* deterministic pipeline the Academic Agent uses
  (`extract_attendance_threshold` over real RAG evidence, then `compute_attendance`) rather than an
  ungrounded/hardcoded percentage -- a required-attendance figure is a policy fact, not something to invent
  client-side, per CLAUDE.md's RAG-grounding rule.

### Streamlit (`streamlit_app/`)

- Single-entry app (`app.py`) with a persistent sidebar identity switcher (options imported directly from
  `app.api.identities`, so the picker can never drift from the server-side allowlist) and a section radio
  (Dashboard / Mission Workspace / Action Center / Campus Operations -- the last gated to ADMIN/FACULTY
  identities client-side, and enforced again server-side regardless).
- `api_client.py` is the *only* way any section talks to the backend -- no section imports `app.db`/
  `app.services`/`app.graph` directly, so the UI can never drift from what the API actually enforces. Test-only
  offline mode: `set_default_http_client(fastapi.testclient.TestClient(app))` points every `ApiClient`
  constructed during a test at the real FastAPI app in-process. (A plain `httpx.Client` cannot call an ASGI app
  synchronously on its own in the installed httpx version -- `ASGITransport` there only implements the async
  interface; `TestClient` is what actually bridges that, and it's a real `httpx.Client` subclass, so it's a
  drop-in `http_client`.)
- **Action Center duplicate-submission guard** (`sections/action_center.py`): each pending-approval card sets a
  per-approval `st.session_state` flag *before* calling the decision endpoint, so a rerun triggered by that same
  click never re-renders the Approve/Reject buttons for an already-decided card -- the backend's 409-on-
  already-resolved (Phase 7's `ApprovalGateError`) is the second line of defense if a request somehow still
  lands twice.
- Visual direction: light background, navy/teal accents (`theme.py`), Inter typeface, status badges mapped
  from the same vocabulary the timeline uses, minimal animation.

### Testing scope (spec section 14)

Test depth is intentionally asymmetric: the spec's detailed priority list (contract validation, authorization,
duplicate prevention, evidence rendering, permission separation) is API-shaped, so `tests/test_api_*.py` covers
it thoroughly against the real FastAPI app + an isolated seeded DB (the same `create_app` factory tests and
production both use). `tests/test_streamlit_app.py` is deliberately light -- a handful of
`streamlit.testing.v1.AppTest` smoke tests (app loads, identity switch changes what's visible, each section
renders without exception) proving the screens work against real data, not exhaustive interaction testing.

### Genuine separate-process persistence test (spec section 12)

`tests/test_persistence_subprocess.py` spawns `scripts/_persistence_subprocess_helper.py` (test-only,
underscore-prefixed) as two real, independent `subprocess.run` OS processes against one isolated temp DB --
not two `MissionOrchestrator` instances sharing the test's own interpreter (that same-process variant is
`tests/test_action_orchestration.py::test_cross_process_approval_and_resumption`, already covering the
same-process case). Process A proposes a registration and exits completely; process B (a fresh Python
interpreter) approves and resumes it; a third invocation of process B's command demonstrates that repeated
resumption never double-registers.

### Known limitations

- No production authentication/SSO, HTTPS, or rate limiting -- see the trust-boundary note above. Do not
  deploy `app/api/`/`streamlit_app/` publicly as-is.
- No background job queue: `POST /missions` and `.../resume` block for the duration of the mission run
  (FastAPI's threadpool absorbs this for the demo's scale; a production system with slow/long-running agent
  calls would want an async task queue and a polling/websocket status API instead).
- The Streamlit app talks to the FastAPI backend over real HTTP (`http://127.0.0.1:8000` by default) -- both
  processes must be started separately for a live demo (`uvicorn app.api.main:app` then
  `streamlit run streamlit_app/app.py`).

## Non-Goals (for now)

- No multi-tenant/campus-scale deployment concerns yet (auth, scaling, multi-region) — single-campus,
  hackathon-scope only.
- No new agents beyond the frozen 10 components without an explicit decision to unfreeze the architecture.
- No direct LLM-to-tool calling outside the Action Agent, under any circumstance.
