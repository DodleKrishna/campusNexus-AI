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

Always say which mode you are presenting. The sidebar shows it: **"🧪 Offline mock LLM"** or
**"🧠 Live LLM: anthropic · <model>"**.

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

This deletes and rebuilds `data/demo/` only. Rebuild before every demo, for two reasons:

- Seed dates (upcoming events, exam dates, complaint SLA deadlines) are relative to the moment of seeding.
- Earlier runs leave registrations, cases and approvals behind. For example, running the registration
  scenario a second time correctly fails as a duplicate.

If the demo machine has no internet access and the embedding model has never been downloaded, use
`python scripts/reset_demo_env.py --embedding deterministic` and set
`CAMPUSNEXUS_EMBEDDING_PROVIDER=deterministic` for the API as well. The two must match.

## 3. Start the application

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

## 5. The four demonstration scenarios

Run them as **Student: Aditi Rao** from **Mission Workspace**. Use the canned buttons, or type the goal.
The typed variants were also tested; any phrasing about the same topic is routed by meaning in live
mode and by domain vocabulary in mock mode.

### Scenario 1: Academic recovery (single agent, evidence-grounded)

- **Button:** 🎓 Academic attendance recovery
- **Or type:** *"My OS attendance is low. Can I write the exam, and how can I recover?"*
- **Expected (both):**
  - Status: COMPLETED.
  - Plan: independent Academic Agent tasks (3 for the button, 2 for the typed goal).
  - Answer: Operating Systems (CS301) attendance is **68.0% (34/50)** against a **75%** requirement, and
    she needs **14 more consecutive classes**.
  - Each task cites `attendance-policy-v2`, section *Minimum Attendance Requirement*.
- **Typed goal only:** it adds an exam-eligibility check. On attendance alone she is **not eligible**
  for the exam, with a note that fee clearance is not evaluated by the system.
- **Point out:** the percentage, the eligibility and the recovery count come from deterministic
  rules, not from the LLM. The citation is shown under **Evidence & Trust**.

### Scenario 2: Internship preparation (Career → Academic → Events collaboration)

- **Button:** 💼 AI internship & workshop prep
- **Or type:** *"Help me prepare for AI internships without missing important classes."*
- **Expected plan (4 tasks):**
  - T1 Academic: timetable. Independent.
  - T2 Academic: exam schedule. Independent.
  - T3 Career: eligible AI internships and skill gaps. Independent.
  - T4 Events: workshops related to the skill gaps that don't clash with classes or exams. **Uses results
    from T1, T2 and T3.**
- **Expected results:**
  - Career finds 3 eligible opportunities and skill gaps (Cloud Computing (AWS), Docker, Kubernetes,
    Node.js, React).
  - Events lists conflict-free workshops (the AI & Deep Learning Workshop, which she is already
    registered for).
  - It flags **"Tech Talk: Cloud Native Systems: conflicts with class CS302"**.
- **Point out:** open T3's result and show `skill_gaps` in its structured facts. Then open T4. Its
  schedule check and topic matching used the timetable, exams and skill gaps that T1–T3 produced.

### Scenario 3: Campus services (complaints and SLA status)

- **Button:** 🏢 Campus grievance & SLA assistance
- **Or type:** *"Check my complaints and tell me whether any are overdue."*
- **Expected:** a single Campus Services task listing 3 cases, all flagged overdue:
  - CASE-0001 hostel: response past due.
  - CASE-0002 IT helpdesk: resolution past due.
  - CASE-0003 fees: response and resolution past due.
  - Citation: `grievance-sla-policy`, section *Escalation*.

### Scenario 4: Approval-gated action (event registration)

- **Button:** 📝 Workshop registration (approval-gated)
- **Or type:** *"Register me for the 'Competitive Coding Contest' workshop if it doesn't clash with my
  classes."* In mock mode, put the event title in single quotes. In live mode the title only has to be
  named explicitly.
- **Expected:**
  - Status NEEDS APPROVAL.
  - T2 (Action Agent) is BLOCKED, with the proposal "Register Aditi Rao … for 'Competitive Coding
    Contest' … at Computer Lab 1".
- Then follow the approval procedure below.

## 6. Approval procedure

1. Note the mission ID shown in the Mission Workspace.
2. Switch **Viewing as** to **Campus Administrator: Priya Desai** and open **Action Center**. The card
   shows the action, the tool, the target resource, the validated parameters and the supporting policy
   evidence.
3. Click **✅ Approve** (or **❌ Reject** with a reason). The confirmation says the action has **not run
   yet**.
4. Switch back to **Student: Aditi Rao** and open **Mission Workspace**. The same browser session keeps
   the mission open; otherwise use **Open an existing mission**. Click **🔄 Resume after approval**.
5. **Expected:** status COMPLETED. The final response ends with "Action 'register_event' executed and
   verified successfully." The timeline shows `approval_approved → mission_resumed → … →
   mission_finalized`.

Before executing, the system re-checks every precondition against current data. After executing, it
independently confirms the registration exists.

## 7. Optional safety demonstrations (all verified)

| Show | How | Expected |
|---|---|---|
| Duplicate prevention | Run Scenario 4 again after it completed. | FAILED: "Student already has a registration for this event." |
| Execution-time conflict | Register for *'Tech Talk: Cloud Native Systems'*, approve, then resume. | Approved, then blocked at execution: "Action blocked: preconditions are no longer valid (1 schedule conflict(s) found.)". Nothing is written. |
| Double approval | Click approve on an already-decided card (or repeat the API call). | HTTP 409. No second decision is recorded. |
| Unsupported goal | *"Book me a flight to Goa for the holidays."* | FAILED with "CampusNexus can't help with this goal: …" and the list of what it can do. No task runs. |
| Missing detail | *"Find a suitable event, check my schedule and prepare my registration."* | Lists conflict-free events, then "Registration was not prepared: no specific event was named…". It never picks an event for the student. |
| Restart persistence | Leave a mission waiting for approval, restart the API, then approve and resume. | Completes normally. State is read from the database, not from memory. |

In the registration missions, the Events task says **"schedule conflicts NOT checked"**. That is
honest: that plan has no timetable task. The clash is still caught by the execution-time recheck,
as the Tech Talk row shows.

## 8. Recovery if something fails during the demo

| Symptom | Cause | Fix |
|---|---|---|
| Sidebar: "API offline" | API not running, or on another port. | Start uvicorn (terminal 1). For a non-default port, set `$env:CAMPUSNEXUS_API_URL` before starting Streamlit. |
| uvicorn exits at startup with `LLMProviderError: … requires ANTHROPIC_API_KEY` or `… requires the 'anthropic' package` | Live mode selected but not configured. | Set the key or install `.[llm]`. Or switch to mock: `Remove-Item Env:CAMPUSNEXUS_LLM_PROVIDER`, then restart. **Announce the switch to mock mode.** |
| A mission FAILED with "the planner could not produce a usable plan (… AuthenticationError / APIConnectionError / APITimeoutError …)" | The live LLM call failed. The mission is recorded as failed, and nothing is substituted. | Check network and key (`python scripts/demo_preflight.py --live-call`). Retry the goal, or restart in mock mode and say so. |
| An agent task FAILED with an `LLMProviderError` message | A live intent or response call failed for that task. Other tasks are unaffected. | As above. |
| UI: "did not respond in time" | Slow live calls exceeded the UI wait (default 300 s, `CAMPUSNEXUS_UI_MISSION_TIMEOUT_SECONDS`). | The mission may still finish server-side. Check the Action Center, or reopen it by ID, **before** resubmitting. |
| Sidebar: "Database is not seeded" or "Policy store is empty" | The API was pointed at the wrong or empty paths. | Check the env vars in terminal 1. Re-run `reset_demo_env.py`. |
| Scenario 4 fails as a duplicate at its first run | Demo data is left over from a rehearsal. | Stop both apps, run `reset_demo_env.py`, restart. |
| Evidence expanders say "No policy evidence" | Embedding provider mismatch between ingest and API. | Use the same `CAMPUSNEXUS_EMBEDDING_PROVIDER` for both, then rebuild. |

## 9. Live-LLM validation (run before presenting in live mode)

```powershell
python scripts/check_live_llm.py            # structured intents + plans for every demo goal
python scripts/check_live_llm.py --e2e --out data/demo/live_report.json   # + full missions
```

Without a key it prints `UNAVAILABLE` and exits 2. It never falls back to mock. With a key, it checks
three things:

- Every intent and plan validates against the Pydantic schemas.
- Every plan passes the deterministic plan validator: only registered agents, known dependencies,
  no cycles, and action tasks carrying an allowlisted tool.
- The off-topic goal is declined.

Supported models: `claude-sonnet-5` (default), `claude-opus-5`, `claude-haiku-4-5`. Models that reject
forced tool calls or disabled thinking are refused at startup with a clear message.
