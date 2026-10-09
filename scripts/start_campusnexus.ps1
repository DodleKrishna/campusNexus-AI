<#
.SYNOPSIS
    Start CampusNexus AI for a demo: FastAPI (127.0.0.1:8000) + React/Vite (127.0.0.1:5173) on the demo environment.

.DESCRIPTION
    One launcher for the React app (Phase 19). In order it:
      1. optionally rebuilds the demo environment (data/demo/) with -Reset
      2. checks the Python virtual environment (.venv)
      3. checks the frontend dependencies (installs them with npm ci if missing)
      4. checks the demo database, policy corpus and policy store
      5. runs scripts/demo_preflight.py and refuses to start if it fails
      6. starts FastAPI on 127.0.0.1:8000
      7. starts the React/Vite dev server on 127.0.0.1:5173
      8. opens the browser (skip with -NoBrowser)
      9. prints the LLM mode the running API reports
     10. stops both processes (and their child processes) on Ctrl+C, or when either one exits

    LLM mode: mock (offline, deterministic) unless -Provider or $env:CAMPUSNEXUS_LLM_PROVIDER selects
    groq or anthropic. Live mode needs the provider's key in the environment ($env:GROQ_API_KEY or
    $env:ANTHROPIC_API_KEY). Without it the launcher stops with an error; it never falls back to mock.
    No key, secret or password is ever printed.

    Database (Phase 21): if $env:CAMPUSNEXUS_DATABASE_URL is set (PostgreSQL / Supabase), the API uses it;
    otherwise the local SQLite demo database (data/demo/campusnexus_demo.db). The launcher prints only
    "Database: PostgreSQL" or "Database: SQLite", never the URL. With PostgreSQL, -Reset rebuilds only the
    local policy store and then creates missing tables and seeds idempotently (scripts/init_postgres_db.py,
    scripts/seed_database.py --demo-classes); it never deletes anything in PostgreSQL.

    Logs: data/demo/logs/api*.log and data/demo/logs/web*.log.

.PARAMETER Reset
    Rebuild the demo database, policy store and dev sign-in accounts first (scripts/reset_demo_env.py).
    With CAMPUSNEXUS_DATABASE_URL set: rebuild the policy store, then initialize and seed PostgreSQL
    idempotently (no PostgreSQL data is deleted).

.PARAMETER Provider
    mock | groq | anthropic. Default: $env:CAMPUSNEXUS_LLM_PROVIDER, else mock.

.PARAMETER Model
    Model id for a live provider. Default: $env:CAMPUSNEXUS_LLM_MODEL, else the provider's default.

.PARAMETER Embedding
    onnx_minilm | deterministic. Default: $env:CAMPUSNEXUS_EMBEDDING_PROVIDER, else onnx_minilm.
    Must match the embedding the demo store was built with (use it together with -Reset when changing).

.PARAMETER LiveCheck
    In live mode, let the preflight spend one small real LLM request.

.PARAMETER NoBrowser
    Do not open the browser.

.PARAMETER NoWorker
    Do not start the autonomous-mission worker. By default the launcher also runs
    scripts/process_due_missions.py --loop --interval 15 (Guardian wake-ups, absence detection and
    communication delivery; one bounded batch every 15 s, safe to stop at any time: work is claimed with
    leases). Logs: data/demo/logs/worker*.log.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_campusnexus.ps1 -Reset
.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_campusnexus.ps1
.EXAMPLE
    $env:GROQ_API_KEY = "<key>"; powershell -ExecutionPolicy Bypass -File scripts\start_campusnexus.ps1 -Provider groq -LiveCheck
#>
param(
    [switch]$Reset,
    [ValidateSet("", "mock", "groq", "anthropic")][string]$Provider = "",
    [string]$Model = "",
    [ValidateSet("", "onnx_minilm", "deterministic")][string]$Embedding = "",
    [switch]$LiveCheck,
    [switch]$NoBrowser,
    [switch]$NoWorker
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$ApiPort = 8000
$WebPort = 5173
$DemoDb = "data/demo/campusnexus_demo.db"
$DemoChroma = "data/demo/chroma"
$LogDir = Join-Path $RepoRoot "data\demo\logs"

function Step([string]$text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Fail([string]$text) { Write-Host "ERROR: $text" -ForegroundColor Red; exit 1 }

# --- LLM mode (decided before anything starts; never silently switched) -----------------------------
if (-not $Provider) { $Provider = if ($env:CAMPUSNEXUS_LLM_PROVIDER) { $env:CAMPUSNEXUS_LLM_PROVIDER.Trim().ToLower() } else { "mock" } }
if ($Provider -notin @("mock", "groq", "anthropic")) { Fail "Unknown LLM provider '$Provider' (expected mock, groq or anthropic)." }
if ($Provider -eq "groq" -and -not $env:GROQ_API_KEY) {
    Fail "Live mode (groq) was requested but GROQ_API_KEY is not set in this shell. Set it, or start without -Provider for mock mode."
}
if ($Provider -eq "anthropic" -and -not $env:ANTHROPIC_API_KEY) {
    Fail "Live mode (anthropic) was requested but ANTHROPIC_API_KEY is not set in this shell. Set it, or start without -Provider for mock mode."
}
$env:CAMPUSNEXUS_LLM_PROVIDER = $Provider
if ($Model) { $env:CAMPUSNEXUS_LLM_MODEL = $Model }

if (-not $Embedding) { $Embedding = if ($env:CAMPUSNEXUS_EMBEDDING_PROVIDER) { $env:CAMPUSNEXUS_EMBEDDING_PROVIDER } else { "onnx_minilm" } }
$env:CAMPUSNEXUS_DB_PATH = $DemoDb
# Phase 21: CAMPUSNEXUS_DATABASE_URL (PostgreSQL / Supabase) outranks CAMPUSNEXUS_DB_PATH in the API.
$UsePostgres = [bool]("$env:CAMPUSNEXUS_DATABASE_URL".Trim()) -or ("$env:CAMPUSNEXUS_DATABASE_MODE".Trim() -eq "postgres")
if ($UsePostgres) { $DatabaseLabel = "PostgreSQL" } else { $DatabaseLabel = "SQLite" }
Write-Host "Database: $DatabaseLabel"
$env:CAMPUSNEXUS_VECTOR_STORE_PATH = $DemoChroma
$env:CAMPUSNEXUS_EMBEDDING_PROVIDER = $Embedding
$env:CAMPUSNEXUS_API_URL = "http://127.0.0.1:$ApiPort"
# The worker signs call tokens and the API verifies them: both must share one secret (never printed).
if (-not $env:CAMPUSNEXUS_VOICE_TOKEN_SECRET) {
    $secretBytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($secretBytes)
    $env:CAMPUSNEXUS_VOICE_TOKEN_SECRET = [Convert]::ToBase64String($secretBytes)
}

# --- 1-2. Python environment -------------------------------------------------------------------------
Step "Checking the Python environment (.venv)"
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Fail ".venv not found. Run: python -m venv .venv; .\.venv\Scripts\Activate.ps1; pip install -e "".[dev,app]"""
}
& $Python -c "import fastapi, uvicorn, sqlalchemy, chromadb, bcrypt, jwt" 2>$null
if ($LASTEXITCODE -ne 0) { Fail "The .venv is missing app dependencies. Run: .\.venv\Scripts\pip install -e "".[dev,app]""" }
foreach ($port in @($ApiPort, $WebPort)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Fail "Port $port is already in use. Stop the previous CampusNexus run first."
    }
}

if ($Reset) {
    Step "Rebuilding the demo environment (data/demo/)"
    & $Python scripts/reset_demo_env.py --embedding $Embedding
    if ($LASTEXITCODE -ne 0) { Fail "reset_demo_env.py failed." }
    if ($UsePostgres) {
        Step "Initializing and seeding PostgreSQL (idempotent; nothing is deleted)"
        & $Python scripts/init_postgres_db.py
        if ($LASTEXITCODE -ne 0) { Fail "init_postgres_db.py failed." }
        & $Python scripts/seed_database.py --demo-classes
        if ($LASTEXITCODE -ne 0) { Fail "seed_database.py failed." }
    }
}
if ($UsePostgres) {
    # Phase 2.5: the API only verifies PostgreSQL at startup (never upgrades it); check driver, schema and lockdown first.
    Step "Checking PostgreSQL (driver, connectivity, schema, Data API lockdown)"
    & $Python scripts/database_preflight.py
    if ($LASTEXITCODE -ne 0) { Fail "Database preflight failed. Install the driver your URL names (pip install -e "".[dev,app,postgres]"") and/or run: python scripts/upgrade_database.py" }
}

# --- 3. Frontend dependencies ------------------------------------------------------------------------
Step "Checking frontend dependencies"
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) { Fail "npm was not found. Install Node.js 20+ and re-run." }
if (-not (Test-Path "frontend\node_modules\.bin\vite.cmd")) {
    Write-Host "    frontend/node_modules is missing; running npm ci (one time)..."
    Push-Location frontend
    try { & npm ci; if ($LASTEXITCODE -ne 0) { Fail "npm ci failed." } } finally { Pop-Location }
}

# --- 4. Demo data --------------------------------------------------------------------------------------
Step "Checking demo data"
if (-not $UsePostgres -and -not (Test-Path $DemoDb)) { Fail "$DemoDb does not exist. Re-run with -Reset." }
if (-not (Test-Path $DemoChroma)) { Fail "$DemoChroma does not exist. Re-run with -Reset." }
if (-not (Get-ChildItem "data/policies" -File -ErrorAction SilentlyContinue)) { Fail "data/policies is empty; the policy corpus is missing." }
if (-not (Test-Path "data/demo/dev_credentials.txt") -and -not $env:CAMPUSNEXUS_DEMO_PASSWORD) {
    Write-Host "    data/demo/dev_credentials.txt not found; sign-in passwords come from the last reset." -ForegroundColor Yellow
}

# --- 5. Preflight --------------------------------------------------------------------------------------
Step "Running preflight"
if ($LiveCheck) { & $Python scripts/demo_preflight.py --live-call } else { & $Python scripts/demo_preflight.py }
if ($LASTEXITCODE -ne 0) { Fail "Preflight failed; not starting. See docs/DEMO_GUIDE.md (Troubleshooting)." }

# --- 6-7. Start both processes -------------------------------------------------------------------------
New-Item -ItemType Directory -Force $LogDir | Out-Null
$failed = $false
$api = $null
$web = $null
$worker = $null

function Stop-Tree($proc, [string]$name) {
    if ($null -ne $proc -and -not $proc.HasExited) {
        & taskkill.exe /PID $proc.Id /T /F 2>$null | Out-Null
        Write-Host "    stopped $name (pid $($proc.Id))"
    }
}

try {
    Step "Starting FastAPI on http://127.0.0.1:$ApiPort"
    $api = Start-Process -FilePath $Python -PassThru -WindowStyle Hidden -WorkingDirectory $RepoRoot `
        -ArgumentList @("-m", "uvicorn", "app.api.main:app", "--host", "127.0.0.1", "--port", "$ApiPort") `
        -RedirectStandardOutput (Join-Path $LogDir "api.log") -RedirectStandardError (Join-Path $LogDir "api.err.log")

    $health = $null
    for ($i = 0; $i -lt 90 -and $null -eq $health; $i++) {
        if ($api.HasExited) { throw "The API exited during startup. See data/demo/logs/api.err.log." }
        try { $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2 } catch { Start-Sleep -Seconds 1 }
    }
    if ($null -eq $health) { throw "The API did not answer /health within 90 s. See data/demo/logs/api.err.log." }
    if (-not $health.ready) { throw "The API started but reports ready=false. Check /health and re-run with -Reset." }

    if (-not $NoWorker) {
        Step "Starting the autonomous-mission worker (one bounded batch every 15 s)"
        $worker = Start-Process -FilePath $Python -PassThru -WindowStyle Hidden -WorkingDirectory $RepoRoot `
            -ArgumentList @("scripts/process_due_missions.py", "--loop", "--interval", "15") `
            -RedirectStandardOutput (Join-Path $LogDir "worker.log") -RedirectStandardError (Join-Path $LogDir "worker.err.log")
    }

    Step "Starting React/Vite on http://127.0.0.1:$WebPort"
    $web = Start-Process -FilePath "cmd.exe" -PassThru -WindowStyle Hidden -WorkingDirectory (Join-Path $RepoRoot "frontend") `
        -ArgumentList @("/c", "npm run dev") `
        -RedirectStandardOutput (Join-Path $LogDir "web.log") -RedirectStandardError (Join-Path $LogDir "web.err.log")
    $webUp = $false
    for ($i = 0; $i -lt 60 -and -not $webUp; $i++) {
        if ($web.HasExited) { throw "The frontend exited during startup. See data/demo/logs/web.err.log." }
        try { Invoke-WebRequest "http://127.0.0.1:$WebPort/" -UseBasicParsing -TimeoutSec 2 | Out-Null; $webUp = $true } catch { Start-Sleep -Seconds 1 }
    }
    if (-not $webUp) { throw "The frontend did not come up within 60 s. See data/demo/logs/web.err.log." }

    # --- 8-9. Report and open ------------------------------------------------------------------------
    $mode = "MOCK (offline, deterministic; no AI model is called)"
    if ($health.llm.live) { $mode = "LIVE ($($health.llm.provider) / $($health.llm.model))" }
    Write-Host ""
    Write-Host "CampusNexus AI is running" -ForegroundColor Green
    Write-Host "  App:       http://127.0.0.1:$WebPort"
    Write-Host "  API docs:  http://127.0.0.1:$ApiPort/docs"
    Write-Host "  LLM mode:  $mode"
    Write-Host "  Database:  $DatabaseLabel"
    Write-Host "  Sign in:   student@ / faculty@ / hod@ / admin@campusnexus.local"
    Write-Host "             password: data/demo/dev_credentials.txt (or `$env:CAMPUSNEXUS_DEMO_PASSWORD set before -Reset)"
    Write-Host "  Worker:    $(if ($NoWorker) { 'not started (-NoWorker)' } else { 'running (Guardians wake on their own)' })"
    Write-Host "  Calls:     $($health.communication.voice_call)$(if ($health.communication.voice_call_reason) { ' (' + $health.communication.voice_call_reason + ')' })"
    Write-Host "  Logs:      data/demo/logs/"
    Write-Host ""
    Write-Host "Press Ctrl+C to stop everything." -ForegroundColor Yellow
    if (-not $NoBrowser) { Start-Process "http://127.0.0.1:$WebPort" }

    # --- 10. Wait; stop both if either exits -------------------------------------------------------------
    while ($true) {
        if ($api.HasExited) { Write-Host "The API stopped (see data/demo/logs/api.err.log)." -ForegroundColor Red; $failed = $true; break }
        if ($web.HasExited) { Write-Host "The frontend stopped (see data/demo/logs/web.err.log)." -ForegroundColor Red; $failed = $true; break }
        if ($null -ne $worker -and $worker.HasExited) { Write-Host "The worker stopped (see data/demo/logs/worker.err.log)." -ForegroundColor Red; $failed = $true; break }
        Start-Sleep -Seconds 1
    }
}
catch {
    Write-Host "ERROR: $($_.Exception.Message)" -ForegroundColor Red
    $failed = $true
}
finally {
    Write-Host "Shutting down CampusNexus..." -ForegroundColor Cyan
    Stop-Tree $web "frontend"
    Stop-Tree $worker "worker"
    Stop-Tree $api "API"
}
if ($failed) { exit 1 }
