<#
.SYNOPSIS
  Offline self-evolution pass (R-3): mine the agent's own history for success/
  failure patterns, synthesize few-shot examples/counterexamples, and stage them.

.DESCRIPTION
  By DEFAULT this only STAGES candidates into self_evolution_candidates
  (status='proposed'); nothing reaches the active few-shot tables until a human
  promotes it. With -Auto (or env DA_SELF_EVOLVE=1) it additionally applies the
  staged candidates, runs the baseline-lock eval suites, and keeps them only if
  the suites pass (no regression), reverting otherwise.

  This is an OFFLINE job — run it by hand or on a schedule, never on the hot path.

.PARAMETER Auto
  Force eval-gated auto-promote (equivalent to DA_SELF_EVOLVE=1).

.PARAMETER List
  List pending candidates and exit (no mining).

.PARAMETER Promote
  Promote a single staged candidate by id.

.PARAMETER Reject
  Reject a single staged candidate by id.

.EXAMPLE
  pwsh -File scripts/run_self_evolve.ps1            # mine + stage for review
  pwsh -File scripts/run_self_evolve.ps1 -List      # show pending candidates
  pwsh -File scripts/run_self_evolve.ps1 -Promote 12
  pwsh -File scripts/run_self_evolve.ps1 -Auto      # eval-gated auto-promote
#>
param([switch]$Auto, [switch]$List, [int]$Promote = -1, [int]$Reject = -1)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = if ($env:VIRTUAL_ENV) { Join-Path $env:VIRTUAL_ENV "Scripts\python.exe" } else { "python" }

$cliArgs = @("-m", "adaptive.self_evolution")
if ($List)        { $cliArgs += "--list" }
if ($Promote -ge 0) { $cliArgs += @("--promote", "$Promote") }
if ($Reject  -ge 0) { $cliArgs += @("--reject",  "$Reject") }
if ($Auto)        { $cliArgs += "--auto" }

& $py @cliArgs
exit $LASTEXITCODE
