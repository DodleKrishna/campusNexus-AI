# CampusNexus AI: Hackathon Demo Guide

A short, rehearsed sequence for presenting CampusNexus in the React app. Every step below was walked
through in a real browser (Chrome, 1440 px) against a freshly reset demo environment in **mock mode**.
Live-LLM wording differs; the structure (agents consulted, verified numbers, routing, approvals) does not.

The older Streamlit debug console, with the mission/approval runbook, is in
[STREAMLIT_CONSOLE_GUIDE.md](STREAMLIT_CONSOLE_GUIDE.md).

## What is real and what is simulated

- **Campus data** (students, classes, attendance, events, internships, complaints) is fictional, seeded
  into a local SQLite file. **Policies** are a fictional corpus retrieved from a local Chroma store.
- **Writes** (attendance, requests, decisions) go to the local demo database only. No email or external
  system is contacted.
- **Sign-in is real** (bcrypt + JWT) but the accounts are local development accounts.
- **LLM**: mock mode is deterministic and offline. No AI model is called, and the top bar says
  **LOCAL / DETERMINISTIC**. Live mode (Groq or Anthropic) plans and words answers, and the badge shows
  the provider and model. Attendance, eligibility, class state, routing and SLA status are always computed
  by deterministic code, never by the LLM. A live-provider outage is shown as unavailable and is never
  replaced by mock output.

## 1. Start (one command)

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start_campusnexus.ps1 -Reset
```

This rebuilds `data/demo/`, checks `.venv`, the frontend dependencies, the demo DB and the policy store,
runs the preflight, starts FastAPI (127.0.0.1:8000) and React (127.0.0.1:5173), prints the LLM mode and
opens the browser. **Ctrl+C stops both processes.** Without `-Reset` it reuses the current demo data.
Logs go to `data/demo/logs/`. Options: `-NoBrowser`, `-Provider mock|groq|anthropic`, `-Model <id>`,
`-Embedding onnx_minilm|deterministic` (use `deterministic` with `-Reset` on a machine with no internet),
and `-LiveCheck` (one real LLM request during preflight).

**Live mode (Groq):**

```powershell
$env:GROQ_API_KEY = "<your key>"      # never commit it; .env is git-ignored
powershell -ExecutionPolicy Bypass -File scripts\start_campusnexus.ps1 -Reset -Provider groq -LiveCheck
```

If live mode is requested without its key, the launcher stops with an error. It never falls back to mock.

### Environment variables

The launcher sets the first three itself. Never commit real values.

| Variable | Purpose | Demo value |
|---|---|---|
| `CAMPUSNEXUS_DB_PATH` | SQLite database | `data/demo/campusnexus_demo.db` |
| `CAMPUSNEXUS_DATABASE_URL` | Optional PostgreSQL / Supabase database; wins over `CAMPUSNEXUS_DB_PATH` when set | Unset for the local demo. Set it in the shell only. See docs/ARCHITECTURE.md (Phase 21) |
| `CAMPUSNEXUS_VECTOR_STORE_PATH` | Chroma policy store | `data/demo/chroma` |
| `CAMPUSNEXUS_EMBEDDING_PROVIDER` | Must match the store's build | `onnx_minilm` (or `deterministic` offline) |
| `CAMPUSNEXUS_JWT_SECRET` | Token signing secret | Optional. Unset means a random secret per API process, so a restart signs everyone out. |
| `CAMPUSNEXUS_LLM_PROVIDER` | `mock`, `groq` or `anthropic` | `mock` unless you present live |
| `CAMPUSNEXUS_LLM_MODEL` | Live model id | Empty means the provider's default (Groq: `openai/gpt-oss-120b`) |
| `GROQ_API_KEY` | Groq key (live only) | Set in the shell, never in a file in the repo |
| `CAMPUSNEXUS_DEMO_PASSWORD` | Optional fixed sign-in password, used at `-Reset` | Unset means a generated one |

### Sign-in accounts

| Email | Role | Person |
|---|---|---|
| `student@campusnexus.local` | Student | Aditi Rao (STU-DEMO-001, CSE year 3) |
| `faculty@campusnexus.local` | Faculty | Dr. Ashok Verma (teaches CS303 Computer Networks, Aditi's mentor) |
| `hod@campusnexus.local` | Head of Department | Dr. Kavita Iyer (head of CSE) |
| `admin@campusnexus.local` | Administrator | Priya Desai |

The password is in **`data/demo/dev_credentials.txt`** (git-ignored). Every reset rewrites that file with
the password it actually seeded. It is never shown in the UI. To switch roles, use **Log out** (bottom
of the sidebar) and sign in again. The top bar always shows the signed-in name and role.

## 2. Timing: the live class

The seeded timetable repeats weekly (Aditi's classes are at 10:00, Monday to Thursday), so a reset also
records one **extra Computer Networks class starting at the reset time** (rounded down to 5 minutes, 60
minutes long) for Dr. Ashok Verma. It also records a Computer Networks class **tomorrow at 14:00**, so
"leave tomorrow afternoon" requests have an affected class. A class can be started from 15 minutes before
its start until its scheduled end.

Run `python scripts/schedule_demo_class.py` (the API can stay running):

- **when the demo starts more than ~50 minutes after the reset**, so the extra class is still current;
- **after midnight**, if you reset the evening before. Near midnight the class is moved earlier so it
  ends at 23:59 and never crosses into the next day. Don't reset between 23:55 and 00:00. Wait until
  after midnight and reset or schedule then.

Add `--tomorrow-afternoon` if the demo is on a later day than the reset. Resetting just before
presenting covers all of this (morning, afternoon or evening).

## 3. The demo sequence (about 10 minutes)

### Demo 1: Student intelligence (2 min)

Sign in as **student@**.

1. **Overview**: CGPA 7.80, overall attendance 82.5%, *1 course below requirement*, next exam CS301.
   The **Current class** card shows the extra Computer Networks class as SCHEDULED.
2. **Agents → Academic Agent**. Ask: *"My OS attendance is low. Can I write the exam and how can I
   recover?"*
   - Operating Systems 68% (34/50) against the required 75%. **NOT ELIGIBLE**. *Attend 14 consecutive
     classes to reach 75%.*
   - Source: Attendance Policy (2025-26) v2. Open **Evidence & verification**.
   - Point out: the percentage, the eligibility and the 14 are computed in code; the policy threshold
     comes from retrieved evidence; the LLM only explains.

### Demo 2: Multi-agent Enquiry (1 min)

**Agents → Enquiry Agent**. Ask: *"Do I have anything important today?"*

- One answer covering today's classes, the next exam, events with no clash, eligible placements and
  complaints past SLA.
- *Consulted: Academic Agent, Events Agent, Placement Agent, Complaints Agent*. Nine sources, VERIFIED.
  The Enquiry Agent is read-only and composes only verified specialist results.

### Demo 3: Live class automation (2 min)

1. Log out, then sign in as **faculty@**. **Overview → Today's classes → Computer Networks → Start Class**.
2. On the class page, choose **Present** for Aditi Rao (or **Mark all present**). The tally updates.
3. Log out, then sign in as **student@**. The **Current class** card is now **LIVE**, showing *Your attendance:
   PRESENT*. In **Enquiry Agent** ask: *"Has my class started?"*, which answers *"Yes. Computer Networks started at …;
   your attendance was marked present at …"*.

Class state comes only from the faculty's real attendance session, never inferred from the timetable.

### Demo 4: Permission automation (2 min)

1. As **student@**, open **Agents → Permission Agent**. Type: *"I need leave tomorrow afternoon for a family
   function."* A **REQUEST PREVIEW · NOT SENT YET** appears: routed to Dr. Ashok Verma (mentor), with the
   affected class and Aditi's attendance. The LLM never picks the recipient; routing is code.
2. **Confirm & Send**. **Requests** shows it as PENDING.
3. Log out, then sign in as **faculty@**. **Student Requests → Approve**.
4. Log out, then sign in as **student@**. The bell shows *Request approved*, and **Requests** shows it as
   APPROVED by Dr. Ashok Verma.

(Before leaving the faculty account you can **Close Class**. Every student must be marked first, and
closing adds the session to course attendance exactly once.)

### Demo 5: Faculty → HOD (1.5 min)

1. As **faculty@**, open **My Requests → Create Request**. Type *"I need leave tomorrow afternoon."* The
   preview shows 1 affected class, *Substitute: Not assigned*, and the reviewer Dr. Kavita Iyer. Click
   **Confirm & Send**.
2. Log out, then sign in as **hod@**. The **Overview** shows *Pending faculty requests 1* and the department's
   classes today (a class past its 10-minute grace period shows **NOT STARTED**). **Requests → Approve**.
3. Optional: **Agents → Enquiry Agent**. Ask *"Which classes haven't started?"*, then *"Which students
   are below 75% attendance?"* (ask them separately; see Known limitations).

### Demo 6: Institution control (1.5 min)

Optional first step: as **hod@**, open **My Requests → Create Request** with *"I need leave tomorrow."* It routes to
the Administration.

Log out, then sign in as **admin@**.

1. **Overview**: students, faculty, classes today, active and not-started classes, pending requests, SLA
   breaches and agent errors for the whole institution.
2. **Requests**: approve the HOD's leave, if you created it. Nobody can decide their own request.
3. **AI Operations**: the provider and mode (the mock banner says it plainly), missions, agent runs,
   provider errors, RAG and database status.
4. **Audit Log**: every sign-in, class start/mark/close, request prepared/submitted/approved, newest first,
   read-only.
5. **Agents → Enquiry Agent**. Ask *"Is the campus running normally today?"*, which answers with classes, not
   started, attendance risk, requests, complaints past SLA and system errors.

## 4. Screenshot checklist (reset first; no fake data)

| # | Page | Account and path |
|---|---|---|
| 1 | Student Dashboard | student@ → `/student` |
| 2 | Student Agents | student@ → `/student/agents` |
| 3 | Academic Agent response | student@ → `/student/agents/academic`, after the Demo 1 question |
| 4 | Faculty Dashboard / live class | faculty@ → `/faculty`, then the class page after **Start Class** |
| 5 | Permission request | student@ → `/student/agents/permission` preview, or `/faculty/requests` |
| 6 | HOD Dashboard | hod@ → `/hod` |
| 7 | Admin Dashboard | admin@ → `/admin` |
| 8 | AI Operations | admin@ → `/admin/ai-operations` |
| 9 | Audit Log | admin@ → `/admin/audit`, after Demos 3 to 5 so it has content |

## 5. Known limitations (say them if asked)

- **One topic per question for faculty, HOD and admin Enquiry.** Their query plan has a single intent, so
  a two-part question ("which classes haven't started *and* which students are below 75%") answers one
  part. Ask the parts separately. The student Enquiry Agent does combine several specialists.
- **AI Operations counts missions and agent runs.** Agent chat answers are not missions, so a demo that
  uses only chat shows *No missions yet* there. Mission runs come from the Streamlit console.
- **Events Agent in mock mode** matches on topics, so a vague *"What workshops are coming up?"* can come
  back empty (PARTLY VERIFIED). Use a suggested question such as *"Find workshops related to AI."*
- A **STAFF** account has no workspace. It gets an explicit "No workspace for the Staff role" page and is
  never routed into the admin console.

## 6. If something goes wrong

| Symptom | Fix |
|---|---|
| Launcher: "Port 8000/5173 is already in use" | A previous run is still up. Close it (Ctrl+C in its window) or end the `python`/`node` process. |
| Launcher: "Live mode (groq) was requested but GROQ_API_KEY is not set" | Set the key in the same shell, or start without `-Provider` for mock mode (and say so). |
| Reset: "Could not delete … Stop the API/Streamlit processes" | Stop the running launcher first; Windows cannot delete open files. |
| Faculty: **Start Class** disabled / student sees no current class | The extra class is over. Run `python scripts/schedule_demo_class.py` and refresh. |
| Sign-in: "Incorrect email or password" | Use the password in `data/demo/dev_credentials.txt` from the latest reset. |
| Everyone signed out after an API restart | Expected without `CAMPUSNEXUS_JWT_SECRET`. Sign in again. |
| Live answer shows "provider unavailable" | A Groq rate limit or outage. State is preserved, so wait and retry, or restart in mock mode and announce it. |
| Evidence says no policy found | Embedding mismatch. Restart with `-Reset` and the same `-Embedding` value. |
