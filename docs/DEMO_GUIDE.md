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

**Quickest way (recommended).** One command rebuilds `data/demo/`, runs the preflight (and refuses to
start if it fails), starts the API and UI on 127.0.0.1 in two windows, and opens the browser:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1 -Reset
```

For live mode, set `$env:CAMPUSNEXUS_LLM_PROVIDER = "anthropic"` and `$env:ANTHROPIC_API_KEY` in that
same shell first. Add `-LiveCheck` to make one real request during preflight. Close the "CampusNexus API"
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

## 5. The four demonstration scenarios

Run them as **Student: Aditi Rao** from **Mission Workspace**. Type the goal into **Or describe your own
goal** and click **Run Mission**, or use the matching canned button. Every goal below was run through
the UI against a freshly reset demo database, and the expected results were observed.

In each agent result, the badge is the Deterministic Verifier's verdict: **VERIFIED**, **NEEDS_REVIEW**,
**FAILED**, or **WAITING FOR APPROVAL**. Citations appear under **Evidence & Trust**, labelled by task.

### Demo 1: Academic recovery (evidence-grounded, deterministic)

- **Goal:** *"My Operating Systems attendance is low. Can I write the exam, and how can I recover?"*
- **Expected:**
  - Plan: T1 and T2, both Academic Agent tasks, independent. Both VERIFIED. Status COMPLETED.
  - Course resolved to **Operating Systems (CS301)**.
  - **34/50 = 68.0%** against the active policy's **75%**.
  - On attendance alone she is **NOT currently eligible** for the exam. She needs **14 more consecutive
    classes**.
  - Citation: `attendance-policy-v2`, section *Minimum Attendance Requirement*.
  - The answer notes that fee clearance is not evaluated by the system.
- **Say:** the percentage, the eligibility and the class count come from plain Python rules against the
  active policy version. The LLM only plans and words the answer.
- The canned 🎓 button asks a 3-part variant: attendance, requirement and recovery, without the exam
  question.

### Demo 2: Multi-agent career mission

- **Goal:** *"Help me prepare for AI internships without missing important classes."* (or the 💼 button)
- **Expected plan:**
  - T1 Academic: timetable.
  - T2 Academic: exams.
  - T3 Career: AI internships and skill gaps.
  - T4 Events: *uses results from T1, T2, T3*.
- **Expected results:** all four VERIFIED, status COMPLETED.
  - T3: 3 eligible internships, e.g. AI Software Engineering Intern at NimbusCloud Technologies, where
    she has already applied. Skill gaps: Cloud Computing (AWS), Docker, Kubernetes, Node.js, React.
  - T4: the AI & Deep Learning Workshop is conflict-free (she is already registered).
    **Tech Talk: Cloud Native Systems conflicts with class CS302.**
  - T3 and T4 each show 3 policy citations.
- **Say:** open T4's structured facts. Every assessed event has `conflict_check_performed: true`, using
  the timetable and exams that T1 and T2 produced. Its topic matching used T3's skill gaps.

### Demo 3: Human-approved action (conflict-free event)

- **Goal:** the 📝 **Workshop registration (approval-gated)** button, i.e. *"Find the workshop titled
  'Competitive Coding Contest', verify there are no conflicts with my classes or exams, and register me
  for it."* In mock mode keep the single quotes around the title.
- **Expected before approval:**
  - Status **NEEDS APPROVAL**, with a banner explaining the next step.
  - The plan: T1 Events (find the event), T2 Academic (timetable), T3 Academic (exams), all
    *independent* and run in parallel; T4 Action Agent *uses results from T1, T2, T3*.
  - T1-T3: VERIFIED.
  - T4 Action Agent: **WAITING FOR APPROVAL**, proposing *"Register Aditi Rao … for 'Competitive Coding
    Contest' … at Computer Lab 1"*.
- Then follow the approval procedure (section 6).
- **Expected after resume:**
  - Status COMPLETED.
  - **Action Result:** ✅ "Action 'register_event' executed and verified successfully."
  - Exactly one registration row is written. A repeat is refused as a duplicate.
- **Be precise about the two checks.** Before approval, the deterministic pre-check covers student, event,
  capacity, deadline, duplicate **and** schedule conflicts, using the timetable and exams T2 and T3 just
  produced, so the approval card shows **VERIFIED** with the note *"Schedule conflicts checked before
  approval against 4 weekly class slot(s) and 4 exam(s) … no clash found."* After approval, every
  precondition (including her *current* timetable and exams) is re-derived again immediately before the
  write, because things can change after a human signs off (section 7, TOCTOU row).

### Demo 4: Campus grievance intelligence

- **Goal:** *"Check my complaints and tell me whether any are overdue."* (or the 🏢 button)
- **Expected:**
  - A single Campus Services task, VERIFIED, status COMPLETED.
  - Her 3 cases with SLA status:
    - CASE-0001 hostel: response past due.
    - CASE-0002 IT helpdesk: **OVERDUE**, resolution past due.
    - CASE-0003 fees: **OVERDUE**, response and resolution past due.
  - "Overdue case(s) requiring attention: CASE-0001, CASE-0002, CASE-0003."
  - Citation: `grievance-sla-policy`, section *Escalation*.
  - The SLA deadlines and breach flags are in the task's structured facts, computed by the
    deterministic SLA rule.
- **Staff view:** as the Campus Administrator, **Campus Operations** shows open and overdue counts and
  per-case SLA status across all students.

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
| Duplicate prevention | Run Scenario 4 again after it completed. | FAILED: "Student already has a registration for this event." |
| **Judges' failure demo:** known conflict | Type *"Find the workshop titled 'Tech Talk: Cloud Native Systems', verify there are no conflicts with my classes or exams, and register me for it."* | The pre-check uses her timetable/exams **before** approval and finds the CS302 class clash. **No approval is ever requested** and nothing is written. Status FAILED with *"Execution stopped because the same verified failure occurred again without new information."* T4's card reads *"same result on 2 attempts"* (the one replan did not change anything). |
| Schedule changes after approval (TOCTOU) | Not doable from the UI. Run `python scripts/demo_phase10.py` (scenario C) or `eval/action_scenarios.py`. | VERIFIED at approval; an exam is then rescheduled onto the event; the execution-time recheck blocks the write (FAILED, zero rows). |
| Double approval | Click approve on an already-decided card (or repeat the API call). | HTTP 409. No second decision is recorded. |
| Unsupported goal | *"Book me a flight to Goa for the holidays."* | FAILED with "CampusNexus can't help with this goal: …" and the list of what it can do. No task runs. |
| Missing detail | *"Find a suitable event, check my schedule and prepare my registration."* | Lists conflict-free events, then "Registration was not prepared: no specific event was named…". It never picks an event for the student. |
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

## 10. Screenshot checklist (capture manually in the browser)

Take these on a freshly reset environment (`start_demo.ps1 -Reset`), in mock mode, with a 1440 px or
wider window. Keep the sidebar visible so the identity and the **🧪 Offline mock LLM** label appear in
every shot. Do not edit or composite the screenshots.

| # | Screen | Identity | State to capture |
|---|---|---|---|
| 1 | **Dashboard** | Student: Aditi Rao | Right after startup, before any mission. Show the title/tagline, the identity strip, **Attendance Overview** with the Operating Systems shortage, and **Existing Grievances**. |
| 2 | **Mission Workspace: multi-agent plan** | Student: Aditi Rao | After Demo 2 completes. Scroll so **Structured Execution Plan** shows T1–T4 with "T4 … uses results from T1, T2, T3" and the COMPLETED badge, plus the **Participating agents** line. |
| 3 | **Evidence + verification** | Student: Aditi Rao | Same mission, or Demo 1. Expand one agent result (a VERIFIED badge, readable answer and structured facts) and one **Evidence & Trust** citation group (document, section, snippet). |
| 4 | **Action Center: pending approval** | Campus Administrator | After Demo 3 is proposed, before clicking anything. Show the card with the action summary, tool, target, the **Deterministic pre-check** badge and its note, the evidence expander (opened), and the **Approve / Reject** buttons. |
| 5 | **Completed registration** | Student: Aditi Rao | After approve → **Resume after approval**. Show the COMPLETED status, the ✅ **Action Result** "executed and verified successfully", and T2 *run 1 of 2: WAITING FOR APPROVAL* / *run 2 of 2: VERIFIED*. Optionally a second shot of the opened **Mission timeline**. |
| 6 | **Campus Operations / SLA** | Campus Administrator | Any time after startup. The metric cards (Open Grievances, Overdue Cases, Pending Approvals) and the **Case Priority & SLA Status** list with the overdue cases. |

If you also want the failure demo on camera, capture the Tech Talk mission after resume: FAILED, with
the ⛔ Action Result.
