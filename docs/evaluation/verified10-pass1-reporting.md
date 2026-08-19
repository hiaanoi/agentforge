# Verified-10 Pass 1: Official Scoring and Reporting

This campaign compares AgentForge and mini-SWE-agent on the same frozen ten
`princeton-nlp/SWE-bench_Verified` tasks. Both arms use one attempt,
`deepseek-v4-flash`, temperature zero, provider-side thinking disabled, and a
1,800-second wall limit. The tracked protocol is
`evaluation/protocols/verified10-deepseek-flash-pass1.json`.

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
