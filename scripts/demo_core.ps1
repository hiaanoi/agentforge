$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$work = Join-Path ([IO.Path]::GetTempPath()) ("agentforge-core-" + [guid]::NewGuid())
$verifier = Join-Path ([IO.Path]::GetTempPath()) ("agentforge-verifier-" + [guid]::NewGuid())
$cliEnvironment = Join-Path ([IO.Path]::GetTempPath()) ("agentforge-cli-" + [guid]::NewGuid())
$uvLog = Join-Path ([IO.Path]::GetTempPath()) ("agentforge-uv-" + [guid]::NewGuid() + '.log')
Copy-Item (Join-Path $root 'examples/demo-repair') $work -Recurse
New-Item -ItemType Directory -Force -Path (Join-Path $work '.agentforge'), $verifier | Out-Null
Copy-Item (Join-Path $work 'tests/test_demo_calc.py') (Join-Path $verifier 'test_demo_calc.py')
@'
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
from test_demo_calc import DemoCalcTests

result = unittest.TextTestRunner().run(
    unittest.defaultTestLoader.loadTestsFromTestCase(DemoCalcTests)
)
raise SystemExit(not result.wasSuccessful())
'@ | Set-Content (Join-Path $verifier '__main__.py') -NoNewline
$python = (Get-Command python -ErrorAction Stop).Source.Replace('\', '/')
$uv = (Get-Command uv -ErrorAction Stop).Source
$wheel = Get-ChildItem -LiteralPath (Join-Path $root 'dist') -Filter 'agentforge_runtime-*.whl' |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if ($null -eq $wheel) { throw 'Build the AgentForge wheel with `uv build` before running this demo.' }

function Invoke-UvSilently {
    param(
        [string[]]$UvArguments,
        [string]$FailureMessage
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $uv @UvArguments *> $uvLog
        $uvExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($uvExitCode -ne 0) {
        Get-Content -LiteralPath $uvLog | Write-Error
        throw $FailureMessage
    }
}

Invoke-UvSilently -UvArguments @('venv', $cliEnvironment) -FailureMessage 'Failed to create the fresh AgentForge CLI environment.'
$cliPython = Join-Path $cliEnvironment 'Scripts/python.exe'
Invoke-UvSilently -UvArguments @('pip', 'install', '--python', $cliPython, $wheel.FullName) -FailureMessage 'Failed to install the fresh AgentForge wheel.'
Remove-Item -LiteralPath $uvLog -Force -ErrorAction SilentlyContinue
$agentforge = Join-Path $cliEnvironment 'Scripts/agentforge.exe'
$hash = (Get-FileHash (Join-Path $work 'src/demo_calc.py') -Algorithm SHA256).Hash.ToLower()

@'
database_path = ".agentforge/agentforge.db"
model = "mock"
profile_ids = ["verify", "visible"]
'@ | Set-Content (Join-Path $work '.agentforge/config.toml') -NoNewline

@"
[provider]
kind = "mock"
mock_responses = [
  { type = "tool_call", tool = "edit_file", arguments = { path = "src/demo_calc.py", old_text = "return a + b - 1", new_text = "return a + b", expected_sha256 = "$hash" } },
  { type = "tool_call", tool = "run_tests", arguments = { profile_id = "visible" } },
  { type = "final", answer = "fixed" }
]

[policy]
task_id = "demo-repair"
policy_version = 1
difficulty = "ENGINEERING"
budget_profile = "ENGINEERING"
allowed_write_paths = ["src/**"]
forbidden_write_paths = [".git/**"]
protected_paths = ["tests/**"]
allowed_development_test_profiles = ["visible"]
final_verification_profile_id = "verify"
allow_file_creation = true
allowed_create_paths = ["src/**"]
max_created_files = 2
max_changed_files = 4
max_total_changed_bytes = 1048576
max_single_file_changed_bytes = 1048576
path_case_sensitive = false

[[profiles]]
profile_id = "visible"
name = "Visible unittest"
description = "Deterministic development tests"
executable = '$python'
argv = ["-m", "unittest", "discover", "tests"]
cwd = "."
timeout_seconds = 30
max_output_bytes = 4096
profile_version = 1
purpose = "development"
allowed_env = { PYTHONDONTWRITEBYTECODE = "1" }

[[profiles]]
profile_id = "verify"
name = "Verifier unittest"
description = "Deterministic final verification"
executable = '$python'
argv = ["{VERIFIER}"]
cwd = "."
timeout_seconds = 30
max_output_bytes = 4096
profile_version = 1
purpose = "verification"
verifier_root = "$($verifier.Replace('\', '/'))"
"@ | Set-Content (Join-Path $work '.agentforge/runtime.toml') -NoNewline

try {
    & $agentforge trust --workspace $work visible --yes
    & $agentforge trust --workspace $work verify --yes
    $out = @(& $agentforge exec --workspace $work 'Fix demo addition' 2>&1)
    $out | Write-Output
    $run = ($out | Select-String -Pattern 'run_id=([^ ]+)' | Select-Object -First 1).Matches.Groups[1].Value
    $pending = @(& $agentforge approvals --workspace $work --run-id $run 2>&1)
    $match = $pending | Select-String -Pattern 'approval_id=([^ ]+)' | Select-Object -Last 1
    $approval = if ($null -eq $match) { '' } else { $match.Matches.Groups[1].Value }
    if (!$run -or !$approval) { throw 'Demo did not produce durable identifiers.' }
    while ($approval) {
        & $agentforge approve --workspace $work $approval | Out-Null
        $out = @(& $agentforge resume --workspace $work $run 2>&1)
        $out | Write-Output
        $pending = @(& $agentforge approvals --workspace $work --run-id $run 2>&1)
        $match = $pending | Select-String -Pattern 'approval_id=([^ ]+)' | Select-Object -Last 1
        $approval = if ($null -eq $match) { '' } else { $match.Matches.Groups[1].Value }
    }
    if (!(Select-String -Path (Join-Path $work 'src/demo_calc.py') -Pattern '^    return a \+ b$' -Quiet)) { throw 'Expected mutation is absent.' }
    if (!($out -match 'run_finished.*outcome=VERIFIED')) { throw 'Final verification did not succeed.' }
    Write-Output "Core demo completed for run_id=$run"
}
finally {
    Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $verifier -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $cliEnvironment -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $uvLog -Force -ErrorAction SilentlyContinue
}
