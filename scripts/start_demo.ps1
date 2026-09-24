<#
.SYNOPSIS
    Start the CampusNexus AI local demo: preflight, then FastAPI + Streamlit, both bound to 127.0.0.1.

.DESCRIPTION
    Points both processes at the isolated demo environment (data/demo/), runs
    scripts/demo_preflight.py and refuses to start if it fails, then opens the API and the UI
    in two separate PowerShell windows. Close those windows to stop the demo.

    Mock (deterministic demo) mode is the default. For live mode, set these in this shell before running:
        $env:CAMPUSNEXUS_LLM_PROVIDER = "groq"        # or "anthropic"
        $env:GROQ_API_KEY = "<key>"                  # or $env:ANTHROPIC_API_KEY
    The script never switches modes on its own, and a live-provider failure is never replaced by mock output.

.PARAMETER Reset
    Rebuild the demo database and policy store in data/demo/ first (scripts/reset_demo_env.py). Other
    files there (e.g. saved live-LLM reports) are kept. Use before every demo run.

.PARAMETER LiveCheck
    In live mode, let the preflight make one small real LLM request.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\start_demo.ps1 -Reset
#>
param(
    [switch]$Reset,
    [switch]$LiveCheck
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }

$ApiPort = 8000
$UiPort = 8501
$env:CAMPUSNEXUS_DB_PATH = "data/demo/campusnexus_demo.db"
$env:CAMPUSNEXUS_VECTOR_STORE_PATH = "data/demo/chroma"
if (-not $env:CAMPUSNEXUS_EMBEDDING_PROVIDER) { $env:CAMPUSNEXUS_EMBEDDING_PROVIDER = "onnx_minilm" }
$env:CAMPUSNEXUS_API_URL = "http://127.0.0.1:$ApiPort"

foreach ($port in @($ApiPort, $UiPort)) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Write-Host "Port $port is already in use. Close the previous demo windows first." -ForegroundColor Red
        exit 1
    }
}

if ($Reset -or -not (Test-Path "data/demo/campusnexus_demo.db")) {
    Write-Host "Rebuilding the demo environment (data/demo/)..." -ForegroundColor Cyan
    & $Python scripts/reset_demo_env.py --embedding $env:CAMPUSNEXUS_EMBEDDING_PROVIDER
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

Write-Host "Running preflight..." -ForegroundColor Cyan
if ($LiveCheck) { & $Python scripts/demo_preflight.py --live-call } else { & $Python scripts/demo_preflight.py }
if ($LASTEXITCODE -ne 0) {
    Write-Host "Preflight failed; not starting the demo. See docs/DEMO_GUIDE.md section 8." -ForegroundColor Red
    exit 1
}

# Child windows inherit this session's CAMPUSNEXUS_*/ANTHROPIC_* environment.
Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'CampusNexus API'; & '$Python' -m uvicorn app.api.main:app --host 127.0.0.1 --port $ApiPort"
) -WorkingDirectory $RepoRoot

Write-Host "Waiting for the API..." -ForegroundColor Cyan
$health = $null
for ($i = 0; $i -lt 60; $i++) {
    try { $health = Invoke-RestMethod "http://127.0.0.1:$ApiPort/health" -TimeoutSec 2; break } catch { Start-Sleep -Seconds 1 }
}
if ($null -eq $health) {
    Write-Host "The API did not become healthy within 60 s. Check the 'CampusNexus API' window." -ForegroundColor Red
    exit 1
}
$mode = "MOCK (offline, deterministic)"
if ($health.llm.live) { $mode = "LIVE ($($health.llm.provider) / $($health.llm.model))" }
Write-Host "API ready: ready=$($health.ready), LLM mode: $mode" -ForegroundColor Green

Start-Process powershell -ArgumentList @(
    "-NoExit", "-Command",
    "`$Host.UI.RawUI.WindowTitle = 'CampusNexus UI'; & '$Python' -m streamlit run streamlit_app/app.py --server.address 127.0.0.1 --server.port $UiPort"
) -WorkingDirectory $RepoRoot

Write-Host "Waiting for the UI (first start can take ~20 s)..." -ForegroundColor Cyan
$uiUp = $false
for ($i = 0; $i -lt 90; $i++) {
    try { Invoke-WebRequest "http://127.0.0.1:$UiPort/_stcore/health" -UseBasicParsing -TimeoutSec 2 | Out-Null; $uiUp = $true; break } catch { Start-Sleep -Seconds 1 }
}
if (-not $uiUp) {
    Write-Host "The UI did not come up within 90 s. Check the 'CampusNexus UI' window." -ForegroundColor Red
    exit 1
}
Start-Process "http://127.0.0.1:$UiPort"

Write-Host ""
Write-Host "CampusNexus AI demo running:" -ForegroundColor Green
Write-Host "  UI:  http://127.0.0.1:$UiPort"
Write-Host "  API: http://127.0.0.1:$ApiPort/docs"
Write-Host "Close the 'CampusNexus API' and 'CampusNexus UI' windows to stop."
