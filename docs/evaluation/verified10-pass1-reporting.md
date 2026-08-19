# Verified-10 Pass 1: Official Scoring and Reporting

This campaign compares AgentForge and mini-SWE-agent on the same frozen ten
`princeton-nlp/SWE-bench_Verified` tasks. Both arms use one attempt,
`deepseek-v4-flash`, temperature zero, provider-side thinking disabled, and a
1,800-second wall limit. The tracked protocol is
`evaluation/protocols/verified10-deepseek-flash-pass1.json`.

## Tencent Cloud execution

Use Ubuntu 22.04 with Docker, 8 vCPU, 32 GiB RAM, a 200 GiB SSD, and at least
160 GiB free before preparation. Clone three repositories and bind the exact
commits before starting:

```bash
git -C ~/agentforge checkout <published-agentforge-commit>
git -C ~/mini-swe-agent checkout 25941c89cfbc91eb40b3f8756348c91d9977d57e
git -C ~/SWE-bench checkout 4e6126978a16bdfebc6538db8f28cacc2c8b77dc
uv sync --project ~/agentforge --frozen
uv sync --project ~/mini-swe-agent --frozen
uv sync --project ~/SWE-bench --frozen
```

Run inside a persistent terminal and enter the DeepSeek key without echoing it
or placing its value in shell history:

```bash
tmux new -s verified10
read -rsp 'DeepSeek API key: ' DEEPSEEK_API_KEY && echo && export DEEPSEEK_API_KEY
cd ~/agentforge
```

Preparation performs no model calls. It pulls and digest-binds all ten official
images, materializes independent arm workspaces, validates all public task
hashes and Git commits, and runs AgentForge admission. Do not proceed unless its
last three lines are exactly:

```text
agentforge_admission=10/10
safe_symlink_rejections=0
protocol_sha256=d57db5029157ff9eea5f722c8977834ff98e7facd24eec7470e7fbcb48e3d231
```

```bash
python evaluation/run_verified10_comparison.py prepare \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir ~/verified10-pass1

python evaluation/run_verified10_comparison.py run-agentforge \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir ~/verified10-pass1
python evaluation/run_verified10_comparison.py finalize-predictions \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir ~/verified10-pass1 --arm AGENTFORGE

python evaluation/run_verified10_comparison.py run-mini \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir ~/verified10-pass1 --mini-root ~/mini-swe-agent
python evaluation/run_verified10_comparison.py finalize-predictions \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir ~/verified10-pass1 --arm MINI_SWE_AGENT
```

Run the arms sequentially. After a web-terminal disconnect, reconnect and use
`tmux attach -t verified10`. Check state with the `status` subcommand. If state
contains a `RUNNING` task, rerun that arm with `--recover-running`; the protocol
does not permit `--retry-failed` because it fixes one model attempt per task.

Before shutting down the ephemeral VM, copy the whole output directory—or at
minimum `artifact-manifest.json`, both comparison files, both prediction files,
both private ledgers, raw official reports, harness logs, and trajectories—to
persistent storage. Then clear the key from the shell with
`unset DEEPSEEK_API_KEY`.

## What counts as a score

Only the pinned official SWE-bench harness decides whether an instance is
resolved. AgentForge development tests, mini-SWE-agent exit statuses, non-empty
patches, and model self-reports are telemetry—not benchmark credit.

The scorer requires a clean local SWE-bench checkout at commit
`4e6126978a16bdfebc6538db8f28cacc2c8b77dc`. It refuses a dirty checkout, a
different commit, a prediction denominator other than the frozen ten, a mixed
model identity, or an official report whose instance classifications do not
partition all ten submitted IDs.

Run each arm separately after both prediction files have been finalized:

```bash
python evaluation/run_verified10_comparison.py score \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir /absolute/path/to/verified10-pass1 \
  --arm AGENTFORGE \
  --harness-root /absolute/path/to/SWE-bench

python evaluation/run_verified10_comparison.py score \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir /absolute/path/to/verified10-pass1 \
  --arm MINI_SWE_AGENT \
  --harness-root /absolute/path/to/SWE-bench
```

The wrapper invokes `swebench.harness.run_evaluation` with the exact ten IDs,
one worker, a 1,800-second per-instance timeout, and an arm-specific run ID. Raw
official JSON and harness logs are retained and hashed; the wrapper does not
reinterpret test results or award partial credit.

After both official reports exist:

```bash
python evaluation/run_verified10_comparison.py report \
  --protocol evaluation/protocols/verified10-deepseek-flash-pass1.json \
  --output-dir /absolute/path/to/verified10-pass1
```

This writes:

- `comparison.json`: the machine-readable paired result and fixed decision;
- `comparison.md`: a compact human-readable summary; and
- `artifact-manifest.json`: SHA-256 bindings for the protocol, predictions,
  private ledgers, official reports, harness logs, and comparison files.

## Fixed interpretation

AgentForge's minimum evidence gates are reported independently:

1. repository admission is 10/10;
2. at least one non-empty patch is generated; and
3. at least one instance is officially resolved.

The decision is selected before observing results:

- admission below 10/10 → `COMPATIBILITY_INCOMPLETE`;
- mini resolves more while AgentForge resolves at most one →
  `DESIGN_REPAIR_ENGINE_MIGRATION`;
- AgentForge resolves at least one and is within one task of mini →
  `KEEP_AND_IMPROVE_AGENTFORGE_LOOP`;
- otherwise → `RUN_MODEL_CONTROL_EXPERIMENT`.

## Budget context is not cost normalization

Public projects expose different budget units:

- mini-SWE-agent's official SWE-bench configuration uses 250 steps and a USD 3
  cost limit:
  <https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/config/benchmarks/swebench.yaml>
- SWE-agent's model default uses a USD 3 per-instance limit:
  <https://github.com/princeton-nlp/SWE-agent/blob/main/sweagent/agent/models.py>
- OpenHands exposes a 500-iteration general default:
  <https://github.com/OpenHands/OpenHands/blob/main/config.template.toml>

Calls, steps, tokens, wall time, and USD are not interchangeable. This pass
raises both arms from the earlier 14-call smoke regime to a declared 50-call or
50-step regime; it is a paired capability comparison, not a claim of
cost-normalized leaderboard parity.
