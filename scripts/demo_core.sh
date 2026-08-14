#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/agentforge-core.XXXXXX")
VERIFIER=$(mktemp -d "${TMPDIR:-/tmp}/agentforge-verifier.XXXXXX")
CLI_ENV=$(mktemp -d "${TMPDIR:-/tmp}/agentforge-cli.XXXXXX")
trap 'rm -rf "$WORK" "$VERIFIER" "$CLI_ENV"' EXIT
cp -R "$ROOT/examples/demo-repair/." "$WORK/"
mkdir -p "$WORK/.agentforge"
cp "$WORK/tests/test_demo_calc.py" "$VERIFIER/test_demo_calc.py"
cat >"$VERIFIER/__main__.py" <<'EOF'
import os
import sys
import unittest

sys.path.insert(0, os.getcwd())
from test_demo_calc import DemoCalcTests

result = unittest.TextTestRunner().run(
    unittest.defaultTestLoader.loadTestsFromTestCase(DemoCalcTests)
)
raise SystemExit(not result.wasSuccessful())
EOF
PYTHON=$(command -v python)
WHEEL=$(find "$ROOT/dist" -maxdepth 1 -type f -name 'agentforge_runtime-*.whl' -print -quit)
[ -n "$WHEEL" ] || { printf '%s\n' 'Build the AgentForge wheel with `uv build` before running this demo.' >&2; exit 1; }
uv venv "$CLI_ENV" >/dev/null 2>&1 || { printf '%s\n' 'Failed to create the fresh AgentForge CLI environment.' >&2; exit 1; }
uv pip install --python "$CLI_ENV/bin/python" "$WHEEL" >/dev/null 2>&1 || { printf '%s\n' 'Failed to install the fresh AgentForge wheel.' >&2; exit 1; }
AGENTFORGE="$CLI_ENV/bin/agentforge"
HASH=$(sha256sum "$WORK/src/demo_calc.py" | awk '{print $1}')

cat >"$WORK/.agentforge/config.toml" <<'EOF'
database_path = ".agentforge/agentforge.db"
model = "mock"
profile_ids = ["verify", "visible"]
EOF
cat >"$WORK/.agentforge/runtime.toml" <<EOF
[provider]
kind = "mock"
mock_responses = [
  { type = "tool_call", tool = "edit_file", arguments = { path = "src/demo_calc.py", old_text = "return a + b - 1", new_text = "return a + b", expected_sha256 = "$HASH" } },
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
path_case_sensitive = true

[[profiles]]
profile_id = "visible"
name = "Visible unittest"
description = "Deterministic development tests"
executable = '$PYTHON'
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
executable = '$PYTHON'
argv = ["{VERIFIER}"]
cwd = "."
timeout_seconds = 30
max_output_bytes = 4096
profile_version = 1
purpose = "verification"
verifier_root = "$VERIFIER"
EOF

"$AGENTFORGE" trust --workspace "$WORK" visible --yes
"$AGENTFORGE" trust --workspace "$WORK" verify --yes
OUT=$("$AGENTFORGE" exec --workspace "$WORK" "Fix demo addition" || true)
printf '%s\n' "$OUT"
RUN=$(printf '%s\n' "$OUT" | sed -n 's/.*run_id=\([^ ]*\).*/\1/p' | head -n 1)
[ -n "$RUN" ]
APPROVAL=$("$AGENTFORGE" approvals --workspace "$WORK" --run-id "$RUN" | sed -n 's/.*approval_id=\([^ ]*\).*/\1/p' | tail -n 1)
[ -n "$APPROVAL" ]

while [ -n "$APPROVAL" ]; do
  "$AGENTFORGE" approve --workspace "$WORK" "$APPROVAL" >/dev/null || true
  OUT=$("$AGENTFORGE" resume --workspace "$WORK" "$RUN" || true)
  printf '%s\n' "$OUT"
  APPROVAL=$("$AGENTFORGE" approvals --workspace "$WORK" --run-id "$RUN" | sed -n 's/.*approval_id=\([^ ]*\).*/\1/p' | tail -n 1)
done

grep -q 'return a + b$' "$WORK/src/demo_calc.py"
printf '%s\n' "$OUT" | grep -q 'run_finished.*outcome=VERIFIED'
printf 'Core demo completed for run_id=%s\n' "$RUN"
