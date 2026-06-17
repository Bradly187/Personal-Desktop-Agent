<#
.SYNOPSIS
  Behavioral-eval gate - runs next to pytest. Used by the pre-push hook and runnable
  by hand.

.DESCRIPTION
  Two tiers:
    1. ALWAYS (model-free, fast): the eval-harness logic tests + the deterministic
       DomainClassifier router gate. A regression here blocks the push.
    2. MODEL-BACKED (needs Ollama): command / trajectory / judge regression gates.
       evals/run.py fails SAFE - exit 2 when the backend is unreachable - so these
       are SKIPPED (warning, push allowed) when Ollama is down, and only BLOCK the
       push on a genuine regression (exit 1).

  Exit 0 = all gates passed or safely skipped. Exit 1 = a real regression.

.PARAMETER SkipModel
  Run only the model-free tier (logic tests + router gate).

.PARAMETER Execution
  Also run the live-DevAgent execution gate (dev_execution suite, --mode
  execution). Off by default because it runs real plans end-to-end (slow): it is
  NOT part of the pre-push hook. Run it by hand or on a schedule.

.EXAMPLE
  pwsh -File scripts/run_evals.ps1
  pwsh -File scripts/run_evals.ps1 -SkipModel
  pwsh -File scripts/run_evals.ps1 -Execution
#>
param([switch]$SkipModel, [switch]$Execution)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" } else { "python" }
$fail = 0

function Invoke-Gate {
    param([string]$Name, [string[]]$EvalArgs, [bool]$ModelBacked)
    Write-Host ""
    Write-Host "=== $Name ===" -ForegroundColor Cyan
    & $py -m evals.run @EvalArgs
    $code = $LASTEXITCODE
    if ($code -eq 0) {
        Write-Host "PASS: $Name" -ForegroundColor Green
    } elseif ($ModelBacked -and $code -eq 2) {
        Write-Host "SKIP: $Name (model backend unreachable)" -ForegroundColor Yellow
    } else {
        Write-Host "FAIL: $Name (exit $code)" -ForegroundColor Red
        $script:fail = 1
    }
}

# --- Tier 1: model-free (always) ------------------------------------------- #
Write-Host "=== eval-harness logic tests ===" -ForegroundColor Cyan
$logicTests = @("tests/test_evals.py", "tests/test_evals_trajectory.py",
                "tests/test_evals_judge.py", "tests/test_evals_router.py",
                "tests/test_evals_skill_trigger.py")
& $py -m pytest @logicTests -q
if ($LASTEXITCODE -ne 0) { $fail = 1 }

Invoke-Gate "router gate" @("--suite", "router_domains", "--predictor", "router", "--check") $false
Invoke-Gate "skill-trigger gate" @("--suite", "skill_triggers", "--predictor", "skill_trigger", "--check") $false

# Static always-loaded skill-metadata token budget (model-free; its own module).
Write-Host ""
Write-Host "=== token-budget gate ===" -ForegroundColor Cyan
& $py -m evals.token_budget
if ($LASTEXITCODE -eq 0) {
    Write-Host "PASS: token-budget gate" -ForegroundColor Green
} else {
    Write-Host "FAIL: token-budget gate (skill metadata over budget)" -ForegroundColor Red
    $fail = 1
}

# --- Tier 2: model-backed (skip if Ollama down) ---------------------------- #
if (-not $SkipModel) {
    Invoke-Gate "command gate" @("--suite", "command_verbs", "--check") $true
    Invoke-Gate "slots gate" @("--suite", "pain_journal_slots", "--predictor", "slots", "--check") $true
    # 180s timeout: qwen3-coder cold-loads (~15s) + thinking trace on first call;
    # too short and a single timed-out case drops accuracy and flakes the gate.
    Invoke-Gate "trajectory gate" @("--suite", "dev_trajectory", "--mode", "trajectory", "--model", "qwen3-coder:30b", "--timeout", "180", "--check") $true
    Invoke-Gate "judge gate" @("--suite", "explain_quality", "--mode", "judge", "--model", "gemma4:12b", "--judge-model", "gemma3:27b", "--timeout", "90", "--check") $true
    # Live-DevAgent execution gate — opt-in (slow: real plans run end-to-end).
    if ($Execution) {
        Invoke-Gate "execution gate" @("--suite", "dev_execution", "--mode", "execution", "--timeout", "200", "--check") $true
    }
} else {
    Write-Host ""
    Write-Host "(model-backed gates skipped: -SkipModel)" -ForegroundColor Yellow
}

Write-Host ""
if ($fail -ne 0) {
    Write-Host "EVAL GATE FAILED - a behavioral baseline regressed." -ForegroundColor Red
    exit 1
}
Write-Host "Eval gate OK." -ForegroundColor Green
exit 0
