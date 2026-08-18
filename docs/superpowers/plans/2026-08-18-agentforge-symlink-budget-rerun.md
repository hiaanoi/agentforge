# AgentForge Verified-10 Compatibility and Budget Rerun Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILLS: Use
> `superpowers:executing-plans`, `superpowers:test-driven-development`, and
> `superpowers:verification-before-completion`. Track progress with the checkboxes below.

**Goal:** Admit the ten frozen SWE-bench Verified repositories, give AgentForge and
mini-SWE-agent a matched 50-call DeepSeek Flash budget, rerun both arms once, and produce an
officially scored paired report that is strong enough to decide whether AgentForge's repair loop
should be retained or replaced.

**Architecture:** Extend the existing byte-exact workspace inventory with an inert `SYMLINK`
entry kind that records but never follows safe repository-owned relative links. Reuse that
identity in source capture, mutation recovery, test freshness, diff validation, and verification
capsules. Add one immutable repair budget profile. Build a small typed campaign layer around the
existing SWE-bench prediction exporter so that preparation, both repair arms, official scoring,
and reporting are resumable, hash-bound, and denominator-preserving.

**Tech Stack:** Python 3.14, Pydantic 2, SQLAlchemy 2, SQLite, Docker, official SWE-bench harness,
mini-SWE-agent, DeepSeek OpenAI-compatible API, pytest, Ruff, mypy, uv.

**Effort allocation:** Tasks 1-2 are the bounded symlink compatibility work (~25%); Task 3 is
budget binding (~25%); Tasks 4-7 are campaign automation and comparison (~50%). Task 8 verifies
and documents the complete deliverable. Do not add general link traversal, a shell tool, a new
provider, a migration backend, or unrelated hardening.

**Frozen experiment facts:** Reuse the ten IDs and public-input hashes from
`evaluation/results/verified10-20260818/selection.json`. Both new arms use exactly
`deepseek-v4-flash`, provider-side thinking disabled, temperature zero, one attempt, and a
1,800-second per-instance wall limit. Generation must never receive gold/test patches,
`FAIL_TO_PASS`, `PASS_TO_PASS`, or hints.

---

### Task 1: Record safe repository symlinks and classify link changes

**Files:**
- Modify: `src/agentforge/persistence/source_revisions.py`
- Modify: `src/agentforge/application/product_workspace.py`
- Modify: `src/agentforge/domain/repair.py`
- Modify: `src/agentforge/evaluation/validators.py`
- Modify: `tests/unit/test_source_revisions.py`
- Modify: `tests/unit/test_product_workspace_baseline.py`

- [ ] Replace the current broad entry-link rejection test with failing tests for the intended
  boundary. On POSIX, prove an in-workspace relative link is captured without reading its target,
  changing only the link target text changes the workspace digest, and absolute or lexically
  escaping targets are rejected. Retain the existing root-link rejection test.

```python
target = root / "docs" / "static" / "logo.svg"
target.parent.mkdir(parents=True)
target.write_text("secret target bytes are not the link identity", encoding="utf-8")
alias = root / "docs" / "epub" / "logo.svg"
alias.parent.mkdir(parents=True)
alias.symlink_to("../../static/logo.svg")

snapshot = WorkspaceDigester().snapshot(root)
entry = next(item for item in snapshot.entries if item.relative_path.endswith("epub/logo.svg"))
assert entry.entry_kind == "SYMLINK"
assert entry.size_bytes == len(b"../../static/logo.svg")
```

- [ ] Add failing product-baseline and diff tests. An unchanged link must be present as
  `file_kind="SYMLINK"`, `is_symlink=True`, and produce no violation. A changed target, deletion,
  newly created link, and regular-file/link conversion must each produce a violation; the
  suspicious-text analyzer must not call `read_text()` on a link.
- [ ] Run the focused tests and confirm that they fail because the current digester rejects every
  link and the product mapping drops link metadata:

```powershell
uv run --frozen pytest tests/unit/test_source_revisions.py tests/unit/test_product_workspace_baseline.py -q
```

- [ ] Add `entry_kind: Literal["REGULAR_FILE", "SYMLINK"] = "REGULAR_FILE"` to
  `WorkspaceDigestEntry`. For a POSIX symlink, read the link text with `os.readlink()` only after
  the pre/post `lstat` identity checks. Reject NULs, absolute targets, non-POSIX reparse points,
  and targets whose lexical `normpath(link.parent / target)` leaves the workspace. Do not call
  `resolve()` on the link target.
- [ ] Bind the target string with a domain-separated digest and keep ordinary workspace digests
  backward compatible. Regular files retain the `b"F"` frame; links use `b"L"`:

```python
target_bytes = os.fsencode(target_text)
target_digest = hashlib.sha256(
    b"agentforge-symlink-target-v1\x00" + target_bytes
).hexdigest()
entry = WorkspaceDigestEntry(
    relative_path=relative,
    size_bytes=len(target_bytes),
    content_sha256=target_digest,
    entry_kind="SYMLINK",
)
```

  Count links against the existing entry/file/total-byte limits. Preserve algorithm version 1
  because regular-file framing and all previously accepted workspaces are unchanged; v1 never
  accepted a link workspace.
- [ ] Map `entry_kind`, `is_symlink`, and `is_reparse_point` in
  `ProductWorkspaceCapture._inventory()`. Extend `_metadata_changed()` to compare both link flags.
  Compute `type_changed` from paths present in both manifests whose `file_kind` differs, not from
  `Path.exists()` on deleted paths. Add `SYMLINK_OR_REPARSE_CHANGED` to `DiffViolationKind`, reuse
  `SYMLINK_OR_REPARSE_CREATED` for new links, and keep `FILE_DELETED` for deleted links.
- [ ] Run the two focused suites again; expect PASS. Also prove read/edit traversal remains denied:

```powershell
uv run --frozen pytest tests/unit/test_source_revisions.py tests/unit/test_product_workspace_baseline.py tests/unit/test_paths.py tests/unit/test_repository_tools.py tests/unit/test_mutation_security.py -q
```

- [ ] Commit this bounded slice:

```powershell
git add src/agentforge/persistence/source_revisions.py src/agentforge/application/product_workspace.py src/agentforge/domain/repair.py src/agentforge/evaluation/validators.py tests/unit/test_source_revisions.py tests/unit/test_product_workspace_baseline.py
git commit -m "fix(workspace): record safe internal symlinks"
```

### Task 2: Preserve links in verification capsules and share workspace identity at runtime

**Files:**
- Modify: `src/agentforge/persistence/verification_capsules.py`
- Modify: `src/agentforge/runtime/test_execution.py`
- Modify: `src/agentforge/application/runtime_factory.py`
- Modify: `tests/integration/test_final_verification.py`
- Modify: `tests/integration/test_shared_runtime_factory.py`
- Create: `tests/integration/test_product_symlink_workspace.py`

- [ ] Write failing capsule tests that copy a Django-style relative documentation link into a
  capsule, assert `os.path.islink()` remains true in `capsule.source_root`, and prove capsule
  verification fails if the published link target string is changed. Add unsafe absolute and
  escaping-link rejection cases.
- [ ] Write a failing shared-runtime test that supplies one `WorkspaceDigester` and asserts the
  exact object is held by both `MutationCoordinator` and `TestExecutionCoordinator`. Add an
  optional `digester` constructor parameter to `TestExecutionCoordinator` in the test contract.
- [ ] Add a Linux integration test with representative Django, Matplotlib, and Pylint relative
  links. Capture the baseline, create a runtime, perform a regular-file mutation, run a test
  freshness check, validate the final diff, and assert the links remain unchanged. Mark only the
  OS-dependent link construction test as skipped on platforms that cannot create POSIX links.
- [ ] Run the new/focused tests and confirm the expected capsule and constructor failures:

```powershell
uv run --frozen pytest tests/integration/test_final_verification.py tests/integration/test_shared_runtime_factory.py tests/integration/test_product_symlink_workspace.py -q
```

- [ ] Teach `VerificationCapsuleBuilder._copy_tree()` to recreate an accepted link with its exact
  target string (`os.symlink(target_text, target_path)`), append the same `SYMLINK` digest entry,
  and re-`lstat` both source and destination. Never copy target bytes. Update `_seal_tree()` and
  staging cleanup to use `lstat` and skip `chmod` through links.
- [ ] Inject a single factory-created `WorkspaceDigester` into both coordinators in
  `RuntimeComponentFactory._assemble()`. Pass it from `TestExecutionCoordinator` into its default
  `VerificationCapsuleBuilder`. Keep `WorkspaceDigester()`'s default policy authoritative for
  `ProductWorkspaceCapture`, `SourceRevisionStore`, doctor, run creation, and source prelaunch
  checks so all product paths have the same safe-link semantics; do not relax the legacy
  `WorkspaceBaselineBuilder` used by curated evaluator fixtures.

```python
digester = WorkspaceDigester()
mutation_coordinator = MutationCoordinator(..., digester=digester)
test_coordinator = TestExecutionCoordinator(..., digester=digester)
```

- [ ] Run the focused integration tests, then the source-revision recovery suite; expect PASS and
  no digest disagreement after mutation/test/capsule transitions:

```powershell
uv run --frozen pytest tests/integration/test_final_verification.py tests/integration/test_shared_runtime_factory.py tests/integration/test_product_symlink_workspace.py tests/integration/test_mutation_revision_recovery.py -q
```

- [ ] Commit the completed compatibility seam:

```powershell
git add src/agentforge/persistence/verification_capsules.py src/agentforge/runtime/test_execution.py src/agentforge/application/runtime_factory.py tests/integration/test_final_verification.py tests/integration/test_shared_runtime_factory.py tests/integration/test_product_symlink_workspace.py
git commit -m "fix(verification): preserve inert workspace symlinks"
```

### Task 3: Add and bind the immutable SWE-bench pass-1 budget

**Files:**
- Modify: `src/agentforge/domain/repair.py`
- Modify: `tests/unit/test_repair_task_policy.py`
- Modify: `tests/integration/test_atomic_run_creation.py`
- Modify: `tests/integration/test_repair_runtime_integration.py`

- [ ] Extend the fixed-budget parametrization with a failing
  `BudgetProfile.SWE_BENCH_PASS1` case whose exact tuple is
  `(50, 80, 8, 8, 2, 3, 1800)`. Add a validation test showing a caller cannot submit an individual
  limit that differs from the fixed profile.
- [ ] Add a run-creation integration test binding all three budget layers:

```python
repair = fixed_budget(BudgetProfile.SWE_BENCH_PASS1)
model = ModelBudget(
    max_model_requests=52,
    max_retries=2,
    max_output_tokens_per_request=4096,
    max_total_tokens=600_000,
)
assert command.max_steps == 80
assert repair.max_model_calls == 50
assert model.max_model_requests == 52
```

  Reopen the database and assert the persisted repair policy, run row, and model-runtime row keep
  those values exactly.
- [ ] Run the focused tests and confirm the new enum/profile is missing:

```powershell
uv run --frozen pytest tests/unit/test_repair_task_policy.py tests/integration/test_atomic_run_creation.py tests/integration/test_repair_runtime_integration.py -q
```

- [ ] Add the enum member and `_FIXED_BUDGETS` entry only. Do not change BASIC, ENGINEERING, or
  CHALLENGE. Do not add an API for extending a live budget.
- [ ] Add terminal/audit assertions for model-call, token, wall-time, edit, and test exhaustion
  under the new profile by reusing the existing runtime fixtures. Physical provider retries must
  remain distinct from logical repair model calls.
- [ ] Run the focused suites; expect PASS. Commit:

```powershell
git add src/agentforge/domain/repair.py tests/unit/test_repair_task_policy.py tests/integration/test_atomic_run_creation.py tests/integration/test_repair_runtime_integration.py
git commit -m "feat(evaluation): add fixed SWE-bench pass1 budget"
```

### Task 4: Freeze a typed Verified-10 rerun protocol

**Files:**
- Create: `evaluation/protocols/verified10-deepseek-flash-pass1.json`
- Create: `src/agentforge/evaluation/verified10_campaign.py`
- Create: `tests/unit/test_verified10_campaign.py`

- [ ] Write failing protocol tests that require exactly the prior ten instance IDs, unique IDs,
  `dataset_name="princeton-nlp/SWE-bench_Verified"`, split `test`, the pinned SWE-bench and
  mini-SWE-agent commits, `deepseek-v4-flash`, thinking disabled, one attempt, both 50-step/call
  limits, the AgentForge provider budget, and all six forbidden generation fields.
- [ ] Require every task to carry `repo`, `base_commit`, and `public_task_sha256`; load task text
  from the public dataset at runtime and reject a hash mismatch. Do not duplicate full task text,
  gold data, or secrets in the tracked protocol.
- [ ] Define strict frozen Pydantic models for `Verified10Protocol`, `Verified10Task`,
  `BenchmarkArm`, `AttemptStatus`, and `BenchmarkAttemptRecord`. Canonicalize the protocol with
  sorted compact JSON and expose `protocol_sha256`.

```python
class AttemptStatus(StrEnum):
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class BenchmarkArm(StrEnum):
    AGENTFORGE = "agentforge"
    MINI_SWE_AGENT = "mini-swe-agent"
```

- [ ] Include budget-rationale metadata with official primary-source URLs and the values used for
  comparison:
  - mini-SWE-agent official SWE-bench config: 250 steps and USD 3,
    `https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/config/benchmarks/swebench.yaml`;
  - SWE-agent model default: USD 3 per instance,
    `https://github.com/princeton-nlp/SWE-agent/blob/main/sweagent/agent/models.py`;
  - OpenHands general default: 500 iterations,
    `https://github.com/OpenHands/OpenHands/blob/main/config.template.toml`;
  - this rerun: 50 logical calls/steps, 1,800 seconds, one attempt.
  These values justify the increase; they are not normalized cost claims.
- [ ] Include the prior 14-call facts and artifact hashes as a comparison baseline: mini 1/10,
  AgentForge 0/10, AgentForge admission 4/10, and six compatibility failures. Never infer those
  facts by silently adopting files from an old output directory.
- [ ] Run the new tests; expect PASS after implementation:

```powershell
uv run --frozen pytest tests/unit/test_verified10_campaign.py -q
```

- [ ] Commit the protocol independently so any cloud output can name its exact digest:

```powershell
git add evaluation/protocols/verified10-deepseek-flash-pass1.json src/agentforge/evaluation/verified10_campaign.py tests/unit/test_verified10_campaign.py
git commit -m "feat(evaluation): freeze Verified-10 pass1 protocol"
```

### Task 5: Export a complete ten-record prediction set, including failures

**Files:**
- Modify: `src/agentforge/evaluation/swebench_prediction.py`
- Modify: `tests/unit/test_swebench_prediction.py`
- Modify: `src/agentforge/evaluation/verified10_campaign.py`
- Modify: `tests/unit/test_verified10_campaign.py`

- [ ] Write failing tests for an explicit empty prediction constructor and atomic batch export.
  The existing single-workspace `capture()` must continue rejecting an empty diff because an empty
  success export is usually a caller bug; only the campaign layer may deliberately represent a
  failed/empty attempt.

```python
empty = SWEbenchPrediction.empty(
    binding=binding,
    model_identity="deepseek-v4-flash",
)
assert empty.model_patch == ""
assert empty.patch_sha256 == hashlib.sha256(b"").hexdigest()
```

- [ ] Add a campaign finalizer test with mixed completed, empty, compatibility-failed, model-failed,
  and timed-out attempts. It must export exactly ten standard harness records in protocol order
  and a separate private attempt ledger with statuses/failure classes. Public prediction files
  must not contain exception details, prompts, environment variables, or API keys.
- [ ] Add `save_swebench_predictions(path, predictions)` using one temporary file, `fsync`, and
  `os.replace`. Reject duplicate/missing/unexpected instance IDs against the protocol before
  publishing the array. Keep `save_swebench_prediction()` backward compatible.
- [ ] Add SHA-256 helpers for the prediction array and attempt ledger. Hash the serialized bytes
  actually written, not reconstructed objects.
- [ ] Run the focused tests and commit:

```powershell
uv run --frozen pytest tests/unit/test_swebench_prediction.py tests/unit/test_verified10_campaign.py -q
git add src/agentforge/evaluation/swebench_prediction.py src/agentforge/evaluation/verified10_campaign.py tests/unit/test_swebench_prediction.py tests/unit/test_verified10_campaign.py
git commit -m "feat(evaluation): preserve failed SWE-bench predictions"
```

### Task 6: Automate preparation and both one-attempt repair arms

**Files:**
- Create: `evaluation/run_verified10_comparison.py`
- Create: `tests/integration/test_verified10_comparison_cli.py`
- Modify: `src/agentforge/evaluation/verified10_campaign.py`
- Modify: `tests/unit/test_verified10_campaign.py`

- [ ] Write CLI integration tests around a fake command runner for these subcommands:
  `prepare`, `run-agentforge`, `run-mini`, `status`, and `finalize-predictions`. Assert each command
  requires the tracked protocol, an explicit new output directory, and the matching protocol
  digest. Existing non-empty outputs must be resumed from their ledger or rejected; never deleted,
  overwritten, or silently rerun.
- [ ] Implement `prepare` to load Verified from `HF_ENDPOINT` (supporting the already-tested
  `https://hf-mirror.com`), validate all public-task hashes, pull the official per-instance
  `swebench/sweb.eval.x86_64.*` image, and materialize an independent workspace per arm from
  `/testbed` while preserving links. Verify Git HEAD equals `base_commit`, record image digest,
  workspace digest, admission result, and disk use. Preparation receives no hidden fields.
- [ ] Make preparation stop before model calls unless AgentForge admission is 10/10. Print one
  compact compatibility table and return nonzero if any safe frozen workspace is rejected.
- [ ] Implement `run-agentforge` as a sequential, resumable pass. For each task, generate immutable
  `.agentforge/config.toml` and `runtime.toml` files binding:

```text
model = deepseek-v4-flash
max_steps = 80
repair_budget_profile = SWE_BENCH_PASS1
max_model_requests = 52
max_retries = 2
max_output_tokens_per_request = 4096
max_total_tokens = 600000
wall_time_seconds = 1800
thinking = disabled
temperature = 0
```

  Use the existing product CLI and DeepSeek provider rather than a benchmark-only repair engine.
  Capture run ID, terminal reason, model calls, provider tokens, approvals, edits, tests, wall time,
  trajectory path/hash, and exported patch. Convert every pre-model or runtime failure into an
  attempt record plus an empty prediction.
- [ ] Implement `run-mini` against the pinned official mini-SWE-agent CLI/config, one instance at a
  time, with `step_limit=50`, `cost_limit=0`, `timeout=1800`, `temperature=0`, thinking disabled,
  and the container network disabled. Preserve the same task text and base image. Capture mini's
  trajectory, exit status, steps, provider tokens, wall time, and patch. Do not let a result from
  either arm suppress the other arm.
- [ ] Require the DeepSeek key only through the process environment or an interactive hidden
  prompt; never accept it as a CLI argument, write it to a config, log it, or include it in an
  artifact. Redact provider exception bodies in public output.
- [ ] Run CLI tests with fake commands and one local no-model fixture. Confirm retries resume only
  `PLANNED` attempts, while `FAILED` attempts require an explicit `--retry-failed` and create a new
  attempt record rather than rewriting the first:

```powershell
uv run --frozen pytest tests/unit/test_verified10_campaign.py tests/integration/test_verified10_comparison_cli.py -q
```

- [ ] Commit the runner:

```powershell
git add evaluation/run_verified10_comparison.py src/agentforge/evaluation/verified10_campaign.py tests/unit/test_verified10_campaign.py tests/integration/test_verified10_comparison_cli.py
git commit -m "feat(evaluation): automate matched Verified-10 runs"
```

### Task 7: Run the official scorer and generate the paired decision report

**Files:**
- Modify: `evaluation/run_verified10_comparison.py`
- Modify: `src/agentforge/evaluation/verified10_campaign.py`
- Modify: `tests/unit/test_verified10_campaign.py`
- Modify: `tests/integration/test_verified10_comparison_cli.py`
- Create: `docs/evaluation/verified10-pass1-reporting.md`

- [ ] Add failing tests for `score` and `report`. Scoring must refuse fewer or more than ten
  prediction records, unexpected IDs, protocol/hash mismatch, a dirty harness checkout, or a
  harness commit other than `4e6126978a16bdfebc6538db8f28cacc2c8b77dc`.
- [ ] Implement `score --arm ...` by invoking the pinned official harness separately for each arm:

```text
python -m swebench.harness.run_evaluation
  --dataset_name princeton-nlp/SWE-bench_Verified
  --split test
  --instance_ids <the exact ten IDs>
  --predictions_path <arm predictions.json>
  --max_workers 1
  --timeout 1800
  --run_id <protocol digest + arm>
```

  Preserve raw reports and logs. The runner must not reinterpret tests or award partial credit.
- [ ] Implement strict report ingestion keyed by `instance_id`. Preserve the scorer's resolved,
  unresolved, error, and infrastructure classifications verbatim, and flag a denominator or
  missing-report mismatch as an incomplete campaign.
- [ ] Generate `comparison.json`, `comparison.md`, and `artifact-manifest.json`. Include per arm and
  per task: admission, non-empty patch, official resolved, model calls/steps, provider tokens,
  approvals, edits, tests, wall time, failure class, protocol/prediction/trajectory/report hashes,
  and the paired 14-call-versus-50-call delta.
- [ ] Encode the approved decision rules as data, not prose guessed after results:

```python
if agentforge.admission != 10:
    decision = "COMPATIBILITY_INCOMPLETE"
elif mini.resolved > agentforge.resolved and agentforge.resolved <= 1:
    decision = "DESIGN_REPAIR_ENGINE_MIGRATION"
elif agentforge.resolved > 0 and agentforge.resolved + 1 >= mini.resolved:
    decision = "KEEP_AND_IMPROVE_AGENTFORGE_LOOP"
else:
    decision = "RUN_MODEL_CONTROL_EXPERIMENT"
```

  Also report the minimum evidence gates independently:
  `admission == 10`, `non_empty_patches > 0`, and `resolved > 0`.
- [ ] Document the exact official source links behind budget context and explain that iteration,
  dollar, call, and token caps are different units. The paired rerun changes one declared budget
  regime; it does not claim cost-normalized leaderboard parity.
- [ ] Run scoring/report tests and commit:

```powershell
uv run --frozen pytest tests/unit/test_verified10_campaign.py tests/integration/test_verified10_comparison_cli.py -q
git add evaluation/run_verified10_comparison.py src/agentforge/evaluation/verified10_campaign.py tests/unit/test_verified10_campaign.py tests/integration/test_verified10_comparison_cli.py docs/evaluation/verified10-pass1-reporting.md
git commit -m "feat(evaluation): score and report Verified-10 comparison"
```

### Task 8: Verify locally, preflight on Linux, and hand off the cloud rerun

**Files:**
- Modify: `README.md`
- Modify: `docs/evaluation/verified10-pass1-reporting.md`
- Modify: `CHANGELOG.md`

- [ ] Add concise README instructions that link to the reporting document and show the six cloud
  commands only: checkout pinned commit, install, `prepare`, `run-agentforge`, `run-mini`, `score`,
  and `report`. State expected disk, sequential execution, output paths, and how to resume after a
  Tencent Cloud web-terminal disconnect. Do not paste API keys into command history.
- [ ] Run all focused compatibility/budget/campaign tests together:

```powershell
uv run --frozen pytest tests/unit/test_source_revisions.py tests/unit/test_product_workspace_baseline.py tests/unit/test_repair_task_policy.py tests/unit/test_swebench_prediction.py tests/unit/test_verified10_campaign.py tests/integration/test_product_symlink_workspace.py tests/integration/test_final_verification.py tests/integration/test_shared_runtime_factory.py tests/integration/test_atomic_run_creation.py tests/integration/test_repair_runtime_integration.py tests/integration/test_verified10_comparison_cli.py -q
```

- [ ] Run repository-wide verification. Do not claim completion if any command fails:

```powershell
uv run --frozen ruff check .
uv run --frozen mypy src
uv run --frozen pytest -q
uv run --frozen python -m compileall -q src tests evaluation
uv build
```

- [ ] On the Tencent Ubuntu 22.04 host, run the Linux-only compatibility preflight against all ten
  prepared workspaces before spending model tokens. Required output:

```text
agentforge_admission=10/10
safe_symlink_rejections=0
protocol_sha256=<64 hex>
```

- [ ] Run both model arms sequentially inside `tmux`, then the two official scorer invocations and
  report generation. Copy the final artifact manifest, comparison report, prediction files, raw
  official reports, and private evidence archive off the ephemeral VM before shutdown.
- [ ] Inspect `git diff --check`, `git status --short`, and the full command outputs. Preserve the
  unrelated `.superpowers/` directory and any user changes. Commit documentation only after the
  evidence matches:

```powershell
git add README.md docs/evaluation/verified10-pass1-reporting.md CHANGELOG.md
git commit -m "docs: add Verified-10 pass1 runbook"
```

## Completion criteria

Implementation is complete only when:

1. the full local quality gate passes;
2. Linux admission is 10/10 with no safe-link rejection;
3. both arms contain exactly ten predictions, including empty failures;
4. the pinned official harness scores both arms;
5. every reported aggregate is derivable from per-instance records and bound artifact hashes; and
6. the report selects exactly one approved next decision without hiding the 14-call baseline.

The experiment is allowed to show that AgentForge still performs poorly. A zero or low score is a
valid result; missing tasks, mismatched budgets, unscored predictions, or interpreting a campaign
with incomplete compatibility are not.
