<#
.SYNOPSIS
  Opt-in installer for the repo's git hooks (currently: the pre-push behavioral-eval gate).

.DESCRIPTION
  Points git at the tracked hooks directory by setting the local
  `core.hooksPath = scripts/hooks`. This is repo-local (never touches global git
  config) and reversible with -Uninstall. The pre-push hook runs
  scripts/run_evals.ps1: Tier-1 (model-free) blocks on a real regression; Tier-2
  (model-backed) auto-skips when Ollama is down, so it won't block pushes from a
  machine without the GPU box running.

.PARAMETER Uninstall
  Remove the hooksPath override (restores git's default .git/hooks).

.EXAMPLE
  pwsh -File scripts/Install-GitHooks.ps1
  pwsh -File scripts/Install-GitHooks.ps1 -Uninstall
#>
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

if ($Uninstall) {
    git config --unset core.hooksPath 2>$null
    Write-Host "Git hooks uninstalled (core.hooksPath cleared; default .git/hooks restored)." -ForegroundColor Yellow
    exit 0
}

git config core.hooksPath scripts/hooks
Write-Host "Git hooks installed: core.hooksPath -> scripts/hooks" -ForegroundColor Green
Write-Host "  pre-push now runs the behavioral-eval gate (scripts/run_evals.ps1)." -ForegroundColor Green
Write-Host "  Bypass a single push with: DA_SKIP_EVAL_HOOK=1 git push" -ForegroundColor DarkGray
Write-Host "  Uninstall with: pwsh -File scripts/Install-GitHooks.ps1 -Uninstall" -ForegroundColor DarkGray
