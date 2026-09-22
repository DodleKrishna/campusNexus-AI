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

## Non-Goals (for now)

- No multi-tenant/campus-scale deployment concerns yet (auth, scaling, multi-region) — single-campus,
  hackathon-scope only.
- No new agents beyond the frozen 10 components without an explicit decision to unfreeze the architecture.
- No direct LLM-to-tool calling outside the Action Agent, under any circumstance.
