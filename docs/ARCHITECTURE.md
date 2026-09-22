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

## Non-Goals (for now)

- No multi-tenant/campus-scale deployment concerns yet (auth, scaling, multi-region) — single-campus,
  hackathon-scope only.
- No new agents beyond the frozen 10 components without an explicit decision to unfreeze the architecture.
- No direct LLM-to-tool calling outside the Action Agent, under any circumstance.
