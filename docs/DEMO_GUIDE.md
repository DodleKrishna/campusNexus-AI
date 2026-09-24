# CampusNexus AI: Demo Guide

A runbook for presenting CampusNexus locally. Each command below was run on the development machine
(Windows, PowerShell, Python 3.12). Each "expected result" was observed in **mock mode**. Live-LLM
wording will differ, but the structure described here (agents, dependencies, verification, approvals)
is the same.

## What is real and what is simulated

| Part | What it is |
|---|---|
| Campus data (students, courses, attendance, events, internships, complaints) | **Fictional**, seeded into a local SQLite file by `scripts/seed_data.py`. |
| Policy documents and citations | Fictional policy corpus in `data/policies/`, retrieved from a local Chroma store. |
| "Actions" (event registration, calendar entry, filing a complaint) | Real writes to the **local demo database only**. No university system, email, or external service is contacted. |
| Identities / login | A fixed demo allowlist chosen from a dropdown. **There is no authentication.** |
| LLM | **Mock mode** (default): deterministic keyword planning and template wording. No AI model is called. **Live mode**: Anthropic Claude plans missions, classifies intents and words answers. |

In both modes, attendance percentages, eligibility, schedule conflicts, SLA status and capacity are
computed by deterministic Python rules, never by the LLM. In live mode the LLM chooses the plan and the
wording; it never computes those results.

Always say which mode you are presenting. The sidebar shows it: **DETERMINISTIC DEMO MODE** or
**LIVE AI MODE · Provider: Groq · Model: openai/gpt-oss-120b** (or Anthropic). If no live key is
configured, demo mode says so calmly and runs fully offline. A live-provider outage shows **PROVIDER
UNAVAILABLE** ("Live AI is temporarily unavailable. Your mission state has been preserved."). It is never
replaced by mock output.

## 1. One-time setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev,app]"          # mock mode
pip install -e ".[dev,app,llm]"      # add this for live-LLM mode
```

## 2. Build a clean demo environment (before every demo)

The demo uses its own database and policy store under `data/demo/`. The development database
(`data/campusnexus.db`) is never touched.

```powershell
# Stop any running API/Streamlit first (Windows cannot delete open files)
python scripts/reset_demo_env.py
```

This deletes and rebuilds only the demo database and demo policy store in `data/demo/` (other files there, such as saved live-LLM reports, are kept). Rebuild before every demo, for two reasons:

- Seed dates (upcoming events, exam dates, complaint SLA deadlines) are relative to the moment of seeding.
- Earlier runs leave registrations, cases and approvals behind. For example, running the registration
  scenario a second time correctly fails as a duplicate.

If the demo machine has no internet access and the embedding model has never been downloaded, use
`python scripts/reset_demo_env.py --embedding deterministic` and set
`CAMPUSNEXUS_EMBEDDING_PROVIDER=deterministic` for the API as well. The two must match.

## 3. Start the application

**Quickest way (recommended).** One command rebuilds `data/demo/`, runs the preflight (and refuses to
start if it fails), starts the API and UI on 127.0.0.1 in two windows, and opens the browser:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1 -Reset
```

For live mode, set `$env:CAMPUSNEXUS_LLM_PROVIDER = "groq"` and `$env:GROQ_API_KEY` (or `anthropic` and
`$env:ANTHROPIC_API_KEY`) in that same shell first. Add `-LiveCheck` to make one real request during preflight. Close the "CampusNexus API"
and "CampusNexus UI" windows to stop. The manual steps below do the same thing by hand.

**Terminal 1: API.** The environment variables must be set in the same terminal that runs uvicorn.

```powershell
$env:CAMPUSNEXUS_DB_PATH = "data/demo/campusnexus_demo.db"
$env:CAMPUSNEXUS_VECTOR_STORE_PATH = "data/demo/chroma"
$env:CAMPUSNEXUS_EMBEDDING_PROVIDER = "onnx_minilm"

# Live mode only (omit both lines for mock mode):
$env:CAMPUSNEXUS_LLM_PROVIDER = "anthropic"
$env:ANTHROPIC_API_KEY = "<your key>"         # never commit this; .env is gitignored

python scripts/demo_preflight.py              # must print READY
python scripts/demo_preflight.py --live-call  # live mode: also makes one small real request
uvicorn app.api.main:app --port 8000
```

**Terminal 2: UI**

```powershell
streamlit run streamlit_app/app.py
```

Open http://127.0.0.1:8501. `.streamlit/config.toml` binds the UI to localhost only. Do not override
that, and do not expose the API or UI on a public network (see `docs/ARCHITECTURE.md`, Phase 8
trust-boundary note).

Check `http://127.0.0.1:8000/health` before presenting. You should see `"ready": true`, the student
count, the policy chunk count, and the active LLM mode.

## 4. Demo identities

Choose the identity in the sidebar under **Viewing as**.

| Identity | Use it for |
|---|---|
| Student: Aditi Rao (STU-DEMO-001) | Running missions and seeing her own data. The demo student. |
| Student: Rohan Mehta (STU2023002) | Showing that a student can't see another student's data. |
| Faculty: Prof. Meera Nair | Staff view. Can view any student, but **cannot approve** actions. |
| Campus Administrator: Priya Desai | The only identity allowed to approve or reject actions in the **Action Center**. |

## 5. Judge-facing demo script

Run everything as **Student: Aditi Rao** unless a step says otherwise. The four starter buttons at the
top of **Mission Workspace** send ordinary natural-language goals through the same path as a typed goal.
The exact inputs are given below, so you can type them instead. Every step was walked through in a real
browser against a freshly reset demo database (mock mode).

Each mission shows **Mission progress** (one stage per agent task, with its real verifier result), then
the plan with dependencies, per-agent results with a one-line summary of what the deterministic check
used and its policy sources, **Evidence & trust**, and the final response. **Show technical details** in
the sidebar reveals the raw structured facts and document ids if a judge asks.

### 1. Dashboard (30 s)

- **Point out:** the five summary cards and **Active missions & actions**.
  - Aditi Rao, CSE year 3, CGPA 7.80.
  - **Attendance risk: 1 course (CS301 68.0%)**.
  - Next exam CS301.
  - 3 open opportunities.
  - Grievances: 3 open, 3 past SLA.
- The sidebar says **DETERMINISTIC DEMO MODE** (or **LIVE AI MODE · Provider · Model** when started
  live). Mock output is never presented as live AI.

### 2. Attendance Recovery (1 min)

- **Input:** 🎓 **Attendance Recovery**, i.e. *"Check my Operating Systems attendance, determine whether
  I currently meet the attendance requirement, and explain how many classes I need to attend to reach the
  required attendance."*
- **Expect:** COMPLETED. The Academic stages are VERIFIED. The answer: 68.0% (34/50) against a required
  75%, needs 14 more consecutive classes.
- **Point out:**
  - Open T3: *"✔ Attendance calculated from current student records (34/50 classes); required 75% from
    Attendance Policy (2025-26) (v2)."*
  - Open its **Evidence & trust** group: the policy title, version, section and snippet.
  - The number is computed in code. The LLM only phrases it.

### 3. AI Internship multi-agent mission (1.5 min)

- **Input:** 💼 **AI Internship Preparation**, i.e. *"I'm a third-year CSE student interested in AI.
  Find internships I'm eligible for, identify my skill gaps, find relevant workshops that don't conflict
  with my classes, and create a preparation plan."*
- **Expect:** Mission progress *Academic Schedule → Academic Exams → Career Analysis → Event Matching*,
  all VERIFIED. In the plan, T4 *uses results from T1, T2, T3*.
- **Point out:**
  - T4: *"… checked for schedule conflicts against 4 timetable slots and 4 exams; matched to your skill
    gaps."*
  - T3: eligibility is calculated from current records, and the skill gaps are Cloud Computing (AWS),
    Docker, Kubernetes, Node.js and React.
  - Tech Talk: Cloud Native Systems is flagged as clashing with CS302.
- **Live mode alternative:** *"Help me prepare for AI internships without missing important classes."*

### 4. Candidate selection + registration (2 min)

- **Input:** 🗓️ **Choose an Event & Register**, i.e. *"Find a suitable event, check my schedule and
  prepare my registration."*
- **Expect:**
  - Status **USER SELECTION REQUIRED** with *"CampusNexus found suitable options. Choose the event you
    want to register for."*
  - Progress: Academic Schedule, Academic Exams, Event Discovery, Schedule Check (all VERIFIED), then
    Student Selection **WAITING**.
  - **Choose an event**: *"2 of 14 events can be selected."* Competitive Coding Contest and Photography
    Contest Exhibition show 🟢 *No class/exam conflict · Registration open* with **Select this event**.
    Blocked cards show why, with the button disabled: Hackathon Kickoff Session *"Clashes with CS301
    class"*, Robotics Expo (CS302 exam), Startup Pitch Night (full), the AI workshop (already
    registered). Six events not yet open are grouped at the bottom.
- **Point out:** the agent recommends and the student chooses. The statuses come from deterministic
  registration rules applied to the verified timetable and exams.
- **Select Competitive Coding Contest.**
  - *"Selected: Competitive Coding Contest"*, then **Selected → Pre-check VERIFIED → Approval
    required**.
  - Status **WAITING FOR APPROVAL**.
  - Same mission, no LLM call.

### 5. Action approval (1 min)

- Switch **Viewing as** to **Campus Administrator: Priya Desai** and open **Action Center**.
- **Point out** the card *"Register for event: Competitive Coding Contest"*:
  - Student STU-DEMO-001, **Deterministic pre-check VERIFIED**.
  - *"Target source: Selected by the student in Mission Workspace."*
  - *"✔ Checks passed: student record, event exists, not already registered, registration open, before
    deadline, seats available, no class/exam conflict."*
  - The schedule note with 4 class slots and 4 exams.
- Click **✅ Approve**. The confirmation says the action has **not run yet**.

### 6. Verified execution (1 min)

- Switch back to **Student: Aditi Rao** → **Mission Workspace** → **🔄 Resume after approval**.
- **Expect:**
  - COMPLETED, with an **ACTION VERIFIED** card: *"Registration successful · Competitive Coding
    Contest"*, *Pre-execution check VERIFIED · Database write SUCCESS · Postcondition check VERIFIED*.
  - Progress ends in *Approval APPROVED → Execution VERIFIED*.
- **Point out:** the preconditions were checked again against current data right before the write, and
  the database was re-read afterwards. Exactly one registration row exists.

### 7. Campus Operations (45 s)

- As **Campus Administrator**, open **Campus Operations**.
- **Point out:**
  - The metric cards (open grievances, open & SLA-breached, pending approvals, total cases).
  - **Breached items (needs attention)**: open cases past SLA, with their due times.
  - The full case list with priority, status and SLA state. SLA breaches are computed deterministically.

### 8. Optional failure/safety example (1 min)

- **Input:** *"Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts
  with my classes or exams, and register me for it."*
- **Expect:** FAILED before any approval. The pre-check finds the CS302 class clash, no approval is
  requested and nothing is written. The ⛔ **ACTION BLOCKED** card says *"Nothing was written"*.
- Also safe to show: *"Book me a flight to Goa for the holidays."* The mission is refused with the list
  of what CampusNexus can do. More options are in section 7.

## 6. Approval procedure

1. Note the mission ID shown in the Mission Workspace.
2. Switch **Viewing as** to **Campus Administrator: Priya Desai** and open **Action Center**. The card
   shows the action, tool, target resource and student. It also shows the **deterministic pre-check**
   result with anything it flagged for the approver, and the validated parameters and supporting policy
   evidence.
3. Click **✅ Approve** (or **❌ Reject** with a reason). The confirmation says the action has **not run
   yet** and names the mission.
4. Switch back to **Student: Aditi Rao** and open **Mission Workspace**. The same browser session keeps
   the mission open; otherwise use **Open an existing mission**. Click **🔄 Resume after approval**.
5. **Expected:**
   - Status COMPLETED, with the ✅ **Action Result** block.
   - T4 shows *run 1 of 2: WAITING FOR APPROVAL* and *run 2 of 2: VERIFIED*.
   - The timeline shows `approval_approved → mission_resumed → … → mission_finalized`.

Faculty cannot approve: only the Campus Administrator identity sees the buttons. Approving an
already-decided card is refused (HTTP 409).

## 7. Optional safety demonstrations (all verified)

| Show | How | Expected |
|---|---|---|
| Duplicate prevention | After step 6, type *"Find the workshop titled 'Competitive Coding Contest', verify there are no conflicts with my classes or exams, and register me for it."* | FAILED: "Student already has a registration for this event." |
| **Judges' failure demo:** known conflict | Type *"Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts with my classes or exams, and register me for it."* | The pre-check uses her timetable/exams **before** approval and finds the CS302 class clash. **No approval is ever requested** and nothing is written. Status FAILED with *"Execution stopped because the same verified failure occurred again without new information."* T4's card reads *"same result on 2 attempts"* (the one replan did not change anything). |
| Schedule changes after approval (TOCTOU) | Not doable from the UI. Run `python scripts/demo_phase10.py` (scenario C) or `eval/action_scenarios.py`. | VERIFIED at approval; an exam is then rescheduled onto the event; the execution-time recheck blocks the write (FAILED, zero rows). |
| Double approval | Click approve on an already-decided card (or repeat the API call). | HTTP 409. No second decision is recorded. |
| Unsupported goal | *"Book me a flight to Goa for the holidays."* | FAILED with "CampusNexus can't help with this goal: …" and the list of what it can do. No task runs. |
| Missing detail | *"Find a suitable event, check my schedule and prepare my registration."* | Lists the candidate events with their deterministic status and waits for the student to select one (section 5, step 4). It never picks an event for the student. Selecting an unsafe event is refused with the reason, and no approval is created. |
| Restart persistence | Leave a mission waiting for approval, restart the API, then approve and resume. | Completes normally. State is read from the database, not from memory. |

In the registration missions, the Events task (T1) says **"schedule conflicts NOT checked in this
step"**. That is honest for T1 alone: it runs in parallel with the timetable/exam tasks, so it has no
schedule data. The conflict check is done by T4's pre-check, using T2 and T3's verified results.

## 8. Recovery if something fails during the demo

| Symptom | Cause | Fix |
|---|---|---|
| Sidebar: "API offline" | API not running, or on another port. | Start uvicorn (terminal 1). For a non-default port, set `$env:CAMPUSNEXUS_API_URL` before starting Streamlit. |
| uvicorn exits at startup with `LLMProviderError: … requires ANTHROPIC_API_KEY` or `… requires the 'anthropic' package` | Live mode selected but not configured. | Set the key or install `.[llm]`. Or switch to mock: `Remove-Item Env:CAMPUSNEXUS_LLM_PROVIDER`, then restart. **Announce the switch to mock mode.** |
| A mission FAILED with "the planner could not produce a usable plan (… AuthenticationError / APIConnectionError / APITimeoutError …)" | The live LLM call failed. The mission is recorded as failed, and nothing is substituted. | Check network and key (`python scripts/demo_preflight.py --live-call`). Retry the goal, or restart in mock mode and say so. |
| An agent task FAILED with an `LLMProviderError` message | A live intent or response call failed for that task. Other tasks are unaffected. | As above. |
| UI: "did not respond in time" | Slow live calls exceeded the UI wait (default 300 s, `CAMPUSNEXUS_UI_MISSION_TIMEOUT_SECONDS`). | The mission may still finish server-side. Check the Action Center, or reopen it by ID, **before** resubmitting. |
| Sidebar: "Database is not seeded" or "Policy store is empty" | The API was pointed at the wrong or empty paths. | Check the env vars in terminal 1. Re-run `reset_demo_env.py`. |
| The registration fails as a duplicate at its first run | Demo data is left over from a rehearsal. | Stop both apps, run `reset_demo_env.py`, restart. |
| Evidence expanders say "No policy evidence" | Embedding provider mismatch between ingest and API. | Use the same `CAMPUSNEXUS_EMBEDDING_PROVIDER` for both, then rebuild. |

## 9. Live-LLM validation (run before presenting in live mode)

```powershell
python scripts/check_live_llm.py            # structured intents + plans for every demo goal
python scripts/check_live_llm.py --e2e --pause 20 --out data/demo/live_report.json   # + full missions
python scripts/check_live_llm.py --affected --out data/demo/live_affected_report.json  # the 3 Phase 12B scenarios
python scripts/check_live_llm.py --case C_unnamed_registration --out data/demo/live_case_c.json  # one scenario only
```

On the Groq free tier, keep `CAMPUSNEXUS_LLM_MAX_CONCURRENCY=1` (the default). Tasks still run in parallel;
only their Groq calls queue, and a 429 waits for the time Groq asks for. If the rate limit still wins, the
mission ends with "Mission paused: the LLM provider … is unavailable". Nothing is replanned. Once the limit
clears, `POST /missions/{mission_id}/resume` continues it. The UI's Resume button only appears for missions
that are waiting for an approval.

Without a key it prints `UNAVAILABLE` and exits 2. It never falls back to mock. With a key, it checks
four things:

- Every intent and plan validates against the Pydantic schemas.
- Every plan passes the deterministic plan validator: only registered agents, known dependencies,
  no cycles, and action tasks carrying an allowlisted tool. It also checks that no action task targets
  something the student did not name.
- The off-topic goal is declined.
- With `--e2e`, each mission reaches its *correct* outcome, and a safe outcome counts as a pass. "Book me a
  flight" must be a SAFE REFUSAL. "Find a suitable event … prepare my registration" must be a SAFE
  CLARIFICATION / USER SELECTION REQUIRED, with no proposal and no approval. A named event must be AWAITING
  APPROVAL. The report includes full traces and per-component latency. On a low per-minute token limit (the
  Groq free tier), use `--pause` so rate-limit errors do not fail tasks.

Supported models: `claude-sonnet-5` (default), `claude-opus-5`, `claude-haiku-4-5`. Models that reject
forced tool calls or disabled thinking are refused at startup with a clear message.

## 10. Screenshot checklist (capture in the browser)

Take these on a freshly reset environment (`start_demo.ps1 -Reset`), with a window 1440 px or wider and the
sidebar visible so the identity and mode label are in every shot. Follow the section 5 order; each state
below appears on the way. Do not edit or composite the screenshots.

| # | Screen | Identity | State to capture |
|---|---|---|---|
| A | Dashboard | Student | Before any mission: summary cards, attendance risk, grievances. |
| B | Multi-agent Mission Workspace | Student | After step 3: Mission progress (four VERIFIED stages) and the plan with "T4 … uses results from T1, T2, T3". |
| C | Evidence / verification | Student | After step 2: T3 expanded (✔ summary + sources) and the opened Evidence & trust group. |
| D | Candidate selection | Student | After step 4, before selecting: eligible and conflicting cards side by side. |
| E | Action Center | Administrator | After selecting: the card with "Target source: Selected by the student in Mission Workspace". |
| F | Completed action | Student | After resume: the ACTION VERIFIED card with Postcondition check VERIFIED. |
| G | Campus Operations | Administrator | Metric cards and Breached items. |
