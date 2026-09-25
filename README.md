# CampusNexus AI

**From campus goals to verified actions.**

A student states a goal: *"My OS attendance is low, can I still write the exam?"*,
*"Help me prepare for AI internships without missing classes"*, or *"Register me for this workshop"*.
CampusNexus plans the work, sends each part to a specialist agent, grounds policy claims in retrieved
documents, and computes every official rule with deterministic code. Anything that changes the world
waits for a human to approve it, and every step goes into an audit trail.

> **Local hackathon prototype.** All campus data is fictional and seeded locally. "Actions" write only to
> the local demo SQLite database; no university system is contacted. There is **no production
> authentication**: identities come from a fixed demo dropdown. Do not deploy it publicly.

## The problem

Campus help is fragmented across offices: academics, placements, events, hostel/IT/fees. Students
have to know which office owns which rule. A chatbot that makes up an attendance percentage or
registers you for a clashing event is worse than no help. CampusNexus is built so that the LLM
never computes or executes anything on its own authority.

> LLMs plan and explain. Deterministic code calculates official rules. RAG provides evidence.
> A verifier checks before and after every action. Humans approve anything with side effects.

## Architecture (10 frozen components)

| # | Component | Role |
|---|---|---|
| 1 | Mission Orchestrator | LangGraph state machine: plan → validate → run independent tasks in parallel and dependent ones in order → verify → replan/finalize. Passes each task's verified facts to the tasks that depend on it. |
| 2 | Academic Agent | Attendance, recovery path, exam eligibility, timetable, exam schedule. |
| 3 | Career Agent | Eligible internships/jobs, skill gaps, application status. |
| 4 | Events & Opportunity Agent | Workshops/events matching skill gaps; capacity; timetable/exam clash detection. |
| 5 | Campus Services Agent | The student's complaints and their SLA (overdue) status. Read-only. |
| 6 | Knowledge/RAG | Hybrid semantic + keyword retrieval over 13 policy documents (ChromaDB); citations carry document, section and version. |
| 7 | Action Agent | The only component that invokes write tools: event registration, calendar entry, filing a complaint. |
| 8 | Deterministic Verifier | Plain-Python checks on every agent result, plus pre-action and post-action checks. |
| 9 | Approval Gate | Every write tool requires an explicit human decision; a decision can't be repeated or silently expire. |
| 10 | Context Service | SQLite source of truth for missions, steps, agent runs, approvals and audit log. Missions survive a process restart. |

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md). Engineering rules: [CLAUDE.md](CLAUDE.md).

### How an action is protected

1. **Propose.** The Action Agent builds a typed proposal from allowlisted parameters.
2. **Pre-check.** The Deterministic Verifier checks that the student and event exist, capacity,
   deadline, duplicates and schedule conflicts.
3. **Approve.** The mission pauses; a Campus Administrator approves or rejects it in the Action Center.
4. **Recheck at resume.** Every precondition is re-derived against *current* data. Anything short of a
   clean pass blocks execution.
5. **Execute.** The Tool Gateway enforces role, ownership and argument schema. Each call carries an
   idempotency key, so a retried step can't double-register.
6. **Post-check.** An independent re-read confirms the database really changed.

## What's real vs. mock

| Mode | Planning & wording | Rules, evidence, verification, approvals |
|---|---|---|
| **Mock** (default, offline) | Deterministic keyword routing and templates. No AI model is called. | Real |
| **Live** (`CAMPUSNEXUS_LLM_PROVIDER=anthropic`) | Claude plans missions, classifies intents and words answers, all through Pydantic-validated structured output. | Real |

The UI sidebar and `GET /health` always state the active mode. Live mode never falls back to mock: a
missing key stops startup with a clear error, and a failed LLM call fails the mission visibly.

## Setup

Requires Python 3.10+ (developed on 3.12, Windows).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,app]"        # add ,llm for live mode
```

## Run the demo

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_campusnexus.ps1 -Reset
```

This rebuilds the isolated demo database (`data/demo/`, never the dev DB), runs the preflight, starts
the API (http://127.0.0.1:8000) and the React app (http://127.0.0.1:5173), both on localhost only,
prints the LLM mode and opens the browser. Ctrl+C stops both. Sign in as `student@`, `faculty@`, `hod@`
or `admin@campusnexus.local`; the password is in `data/demo/dev_credentials.txt`.

The hackathon demo sequence is in [docs/DEMO_GUIDE.md](docs/DEMO_GUIDE.md). The Streamlit debug console
(`scripts\start_demo.ps1`) and its mission/approval runbook are in
[docs/STREAMLIT_CONSOLE_GUIDE.md](docs/STREAMLIT_CONSOLE_GUIDE.md).

Live mode: set `$env:CAMPUSNEXUS_LLM_PROVIDER = "anthropic"` and `$env:ANTHROPIC_API_KEY` first. Validate
the key and the model's structured outputs, with no code changes, using:

```powershell
python scripts/check_live_llm.py --e2e --out data/demo/live_report.json
```

## Tests

```powershell
pytest                                   # full suite; never calls a live LLM or touches the dev DB
python eval/run_rag_eval.py              # + run_academic_eval.py, orchestration_scenarios.py,
                                         #   multiagent_scenarios.py, action_scenarios.py
```

## Known limitations

- No authentication, HTTPS or rate limiting. Local demo only.
- Live-LLM plan quality and latency have not been measured with a real key yet. The validation
  command above exists for that.
- In the mock registration plan, the timetable isn't fetched before approval. The approval card
  therefore flags "schedule conflicts not checked", and the real clash check happens in the
  execution-time recheck.
- `POST /missions` blocks until the mission finishes. There is no background job queue.
- Fixed data: one fictional campus, 20 students, 13 policy documents.
