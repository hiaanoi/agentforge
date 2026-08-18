# AgentForge Verified-10 Compatibility and Budget Rerun Design

**Date:** 2026-08-18

**Status:** Approved for implementation planning

## 1. Goal

Remove the two known confounders from the first public comparison before deciding
whether to replace AgentForge's repair engine:

1. allow real repositories containing unchanged internal symlinks to enter the
   AgentForge runtime; and
2. replace the 14-call smoke budget with a bounded 50-call SWE-bench budget for both
   AgentForge and mini-SWE-agent.

Then rerun the exact same ten deterministic SWE-bench Verified tasks, once per system,
and score every planned task with the pinned official harness.

This is a capability experiment, not a new security milestone. Symlink support is one
bounded compatibility change, not the center of the project.

## 2. Evidence motivating the change

The frozen 14-call pilot produced:

- mini-SWE-agent: 1/10 resolved, one non-empty patch, nine empty patches;
- AgentForge: 0/10 resolved, ten empty patches;
- AgentForge repository admission: 4/10;
- AgentForge pre-model symlink compatibility failures: 6/10.

The six rejected workspaces contained only repository-owned relative symlinks such as
Django documentation icons, Matplotlib image aliases, and Pylint symlink fixtures.
None of the observed links was absolute or escaped its workspace.

The 14-call budget was also substantially below public project defaults:

- mini-SWE-agent's official SWE-bench configuration uses 250 steps and a USD 3 cost
  limit;
- SWE-agent defaults to a USD 3 per-instance limit with no fixed call limit; and
- OpenHands exposes a 500-iteration general default, normally narrowed by a benchmark
  configuration.

The next pilot therefore uses 50 logical model calls: enough to permit a normal
inspect-edit-test-submit sequence while remaining one fifth of mini's public default.

## 3. Scope and effort allocation

The implementation has three workstreams:

1. **Repository admission compatibility (about 25%)**
   - record safe pre-existing internal symlinks without following them;
   - keep model read/write access through symlink paths denied;
   - verify links remain unchanged through completion and verification.
2. **Benchmark budget profile (about 25%)**
   - add one immutable `SWE_BENCH_PASS1` budget;
   - bind model, repair, wall-time, token, edit, and test limits consistently;
   - expose the profile in typed configuration without runtime budget extension.
3. **Repeatable comparison and official scoring (about 50%)**
   - prepare all ten workspaces automatically;
   - run both systems once at the new limit;
   - preserve empty patches and failures in the denominator;
   - generate official reports and a paired comparison against the 14-call result.

No general sandbox, new provider, new custom benchmark, migration backend, or broad
security refactor is included.

## 4. Symlink compatibility design

### 4.1 Accepted links

A workspace entry may be recorded as an inert symlink only when:

- it is a real POSIX symlink rather than another reparse-point type;
- its target is relative;
- lexical normalization of the target from the link's parent stays inside the
  workspace; and
- the workspace root and every parent component of the root remain ordinary
  directories.

The scanner records the link path, entry kind, target byte length, and a domain-tagged
SHA-256 of the target string. It never reads the target file through the link.

### 4.2 Rejected links and operations

The runtime continues to reject:

- absolute symlink targets;
- relative targets escaping the workspace;
- junctions and non-symlink reparse points;
- workspace roots or root parent chains containing links;
- model reads or writes through a symlink path component;
- model-created links; and
- changed, deleted, or type-converted baseline links.

This preserves the existing mutation path policy. The feature admits inert repository
metadata; it does not add a symlink tool or link mutation capability.

### 4.3 Shared workspace identity

One configured workspace digester must be used by product capture, source revision,
mutation recovery, test freshness, and diff validation. Otherwise a workspace could
be admitted by one layer and rejected after the first mutation or test.

The baseline manifest maps the digester's entry kind and link flags into the existing
`WorkspaceFileBaseline` fields. Diff validation compares those fields explicitly.

### 4.4 Verification capsule

Verification capsules preserve accepted internal relative symlinks as links, without
dereferencing them. Capsule capture and verification bind the link target string and
entry kind before and after the verifier process. Unsafe links continue to fail capsule
creation.

Legacy curated evaluation fixtures may retain their current symlink-free admission
rule; this change is required for product and SWE-bench workspaces.

## 5. Benchmark budget design

Add one fixed profile:

```text
BudgetProfile.SWE_BENCH_PASS1

max_model_calls = 50
max_read_calls = 80
max_edit_attempts = 8
max_test_runs = 8
max_completion_corrections = 2
max_policy_violations = 3
max_wall_time_seconds = 1800
```

The matching provider budget is:

```text
max_model_requests = 52
max_retries = 2
max_output_tokens_per_request = 4096
max_total_tokens = 600000
```

Product maximum steps for the rerun are fixed at 80. The model cannot extend any
limit interactively. Token totals and physical retries remain separately reported.

The mini-SWE-agent control uses:

```text
step_limit = 50
wall_time_limit_seconds = 1800
attempts = 1
temperature = 0
network = none
```

Both arms use the exact account model `deepseek-v4-flash` with provider-side thinking
disabled.

## 6. Rerun protocol

The rerun keeps the previous deterministic ten instance IDs and their public task
inputs. It creates a new protocol digest and new output directory. No previous run is
overwritten or adopted.

Generation receives only:

- `instance_id`;
- `repo`;
- `base_commit`; and
- `problem_statement`.

Generation does not receive gold patches, test patches, `FAIL_TO_PASS`, `PASS_TO_PASS`,
or hints. Official scoring remains a separate offline process.

Each system gets exactly one new attempt per instance. The runner records all ten
predictions, including empty patches and pre-model failures. Only the official
SWE-bench harness determines resolved status.

The rerun report includes:

- repository admission count;
- non-empty patch count;
- official resolved count;
- model calls and provider-reported tokens;
- approval, edit, and test counts;
- wall time;
- compatibility, model, policy, and infrastructure failures;
- prediction, protocol, trajectory, and official-report hashes; and
- a paired per-instance comparison with mini-SWE-agent and the prior 14-call run.

## 7. Testing strategy

Implementation proceeds test-first.

### Symlink tests

- product capture accepts an unchanged internal relative symlink;
- source digest changes when the link target string changes;
- absolute and escaping targets are rejected;
- root and parent-chain symlinks remain rejected;
- mutation/read tools remain unable to traverse links;
- unchanged links produce no diff violation;
- created, changed, deleted, or type-converted links are violations;
- verification capsules preserve and revalidate safe internal links; and
- Linux integration reproduces the Django-style documentation link layout.

### Budget tests

- the new profile binds every fixed repair limit;
- configuration cannot override an individual fixed limit;
- persisted run/model state receives the intended limits;
- call, token, wall-time, edit, and test exhaustion remain terminal and auditable;
- existing BASIC, ENGINEERING, and CHALLENGE behavior remains unchanged.

### Rerun tests

- selection contains exactly the frozen ten IDs;
- every arm exports exactly ten predictions;
- empty and compatibility failures remain in the denominator;
- existing results are never silently rerun or overwritten;
- official report ingestion preserves the scorer's raw values; and
- paired summaries match per-instance facts.

The full pytest suite, Ruff, mypy, compileall, and diff checks must pass before cloud
deployment.

## 8. Acceptance and decision rules

The rerun may begin only when all ten workspaces pass AgentForge source admission and
profile binding on Linux.

Minimum AgentForge capability evidence is:

```text
repository admission = 10/10
non-empty patches > 0
official resolved > 0
```

Decision after the rerun:

- if mini improves while AgentForge remains at or near zero, design the repair-engine
  migration;
- if both improve and AgentForge is reasonably close, keep the current repair loop and
  address only evidence-backed execution bottlenecks;
- if both remain near zero, run a separate model-control experiment before attributing
  failure to either scaffold; and
- if AgentForge still rejects any of the ten workspaces because of unchanged safe
  symlinks, the compatibility implementation is incomplete and the model comparison is
  not interpreted.

## 9. Non-goals

- No migration to mini-SWE-agent or SWE-ReX in this change.
- No arbitrary host shell in AgentForge.
- No new provider or model.
- No full 50/500-instance campaign.
- No new aggregate safety score.
- No per-repository manual verifier.
- No prompt tuning based on the prior official outcomes.
- No attempt to support arbitrary filesystem links or Windows junctions.
