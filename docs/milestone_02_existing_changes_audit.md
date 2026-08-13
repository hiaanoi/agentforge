# Milestone 2 Existing Changes Audit

Audit date: 2026-07-13. Baseline: `0a3e1c86376044a65abd67f29a19b99ec657b34e`.
The initial working tree contained 39 changed paths: 16 tracked modifications and 23
untracked paths. Every path is reviewed below. No dependency, lock-file, secret, database,
cache, virtual-environment, wheel, or unrelated generated-file change was present.

## Summary

- The changes form one coherent Milestone 2 implementation. There is one `ToolExecutor`, one
  `PolicyEngine`, and one shared `WorkspacePathResolver`; no duplicate implementation was found.
- The M0/1 public behavior remains compatible. The four requested regression groups pass.
- The initial implementation is substantially complete, but needs changes before acceptance:
  cancellation must produce a terminal audit event, Git subprocess environment/output handling
  needs a tighter boundary, several explicit security cases need direct tests, and documents must
  use the actual baseline and final verification evidence.
- `ToolSpec.allowed_commands` is retained for M0/1 model compatibility but is intentionally unused
  because Milestone 2 has no command tool. `ToolSource.MCP` is only a negative policy value; MCP
  execution is not implemented.
- No empty shell files, hard-coded fixtures in production code, `shell=True`, silent UTF-8 error
  suppression, write tools, network tools, or arbitrary command execution were found.

## Per-file audit

Status values describe the state found before this audit's corrective work.

| # | File | Category and purpose | M2 / status | Duplicate, compatibility, design, security, unused-code findings | Recommendation |
|---:|---|---|---|---|---|
| 1 | `README.md` | Documentation: implemented/deferred boundary | Yes / partial | No duplicate or M0/1 break; claims need final evidence | Modify |
| 2 | `docs/architecture.md` | Documentation: Tool Runtime components and flow | Yes / partial | Correct single-executor design; update final boundaries only | Modify |
| 3 | `docs/implementation_plan.md` | Documentation: milestone status and verification | Yes / partial | Premature completion wording and prefilled counts | Modify |
| 4 | `src/agentforge/domain/enums.py` | Core domain: policy, path, source, and stable error enums | Yes / complete | Additive and compatible; MCP is a denied source, not an implementation | Keep |
| 5 | `src/agentforge/domain/errors.py` | Core domain: typed tool/runtime failures | Yes / partial | No duplicate hierarchy; invalid-spec code lacks a domain exception | Modify |
| 6 | `src/agentforge/domain/models.py` | Core domain and budget: ToolSpec, ToolResult, Run counters | Yes / complete | Additive persistence contract; `allowed_commands` remains intentionally dormant | Keep |
| 7 | `src/agentforge/persistence/repositories.py` | Persistence/audit: save and load tool-call counters | Yes / complete | M0/1 mappings preserved; separate transactions remain documented debt | Keep |
| 8 | `src/agentforge/persistence/tables.py` | Persistence: SQLite columns for tool budget | Yes / complete | Additive schema; migration tooling is outside current scope | Keep |
| 9 | `src/agentforge/runtime/engine.py` | Runtime: delegates tool lifecycle and maps RunBudget | Yes / complete | Removes duplicate tool auditing; M0/1 loop remains compatible | Keep |
| 10 | `src/agentforge/tools/base.py` | ToolRegistry contract: typed sync/async Tool protocol | Yes / complete | One protocol; no shell capability | Keep |
| 11 | `src/agentforge/tools/executor.py` | ToolExecutor: validation, policy, budget, timeout, audit, result bounds | Yes / partial | Single implementation; cancellation lacks terminal audit; key-based sanitization is bounded but best-effort | Modify |
| 12 | `src/agentforge/tools/registry.py` | ToolRegistry: registration, lookup, ordering, schemas, validation | Yes / partial | No duplicate registry; schema mismatch raises generic `ValueError` | Modify |
| 13 | `src/agentforge/tools/safe.py` | Compatibility tools: typed echo and addition | Yes / complete | M0/1 tools adapted without policy duplication | Keep |
| 14 | `tests/integration/test_runtime.py` | Tests: M0/1 loop, checkpoints, isolation, budget mapping | Yes / complete | Real runtime/persistence behavior, not fixture hard-coding | Keep |
| 15 | `tests/unit/test_model_and_tools.py` | Tests: provider/parser and compatibility tool execution | Yes / complete | Existing assertions retained; no interface regression | Keep |
| 16 | `tests/unit/test_persistence.py` | Tests: restart durability and UTC values | Yes / complete | Strengthens M0/1 evidence without weakening tests | Keep |
| 17 | `docs/decisions/0002-workspace-path-resolution.md` | ADR: resolved-path containment and links | Yes / partial | Correct design; align wording with final tests | Modify |
| 18 | `docs/decisions/0003-fixed-git-subprocess.md` | ADR: fixed read-only Git subprocess | Yes / partial | No dependency needed; inherited Git environment and capture bound need tightening | Modify |
| 19 | `docs/decisions/0004-tool-output-bounds.md` | ADR: repository and executor output bounds | Yes / partial | Executor bound is real; Git capture must also be bounded | Modify |
| 20 | `docs/milestone_01_acceptance.md` | Documentation: M0/1 acceptance evidence | Yes / partial | References stale commit `815df43` instead of current baseline | Modify |
| 21 | `docs/milestone_02_report.md` | Documentation: M2 acceptance report | Yes / partial | Prefilled results must be replaced with final evidence | Modify |
| 22 | `docs/security_model.md` | Documentation: trust and security boundaries | Yes / partial | Generally accurate; add cancellation, Git environment, TOCTOU, and sanitization limits | Modify |
| 23 | `src/agentforge/policy/__init__.py` | Policy package marker | Yes / complete | Empty package initializer by design, not a generated shell | Keep |
| 24 | `src/agentforge/policy/engine.py` | Policy Engine: existence, arguments, run, budget, risk, approval, path | Yes / complete | Policy is centralized; tool-side resolver calls are defense in depth, not duplicate policy | Keep |
| 25 | `src/agentforge/policy/models.py` | Policy domain: structured `PolicyDecision` | Yes / complete | Replaces critical raw dict decision data | Keep |
| 26 | `src/agentforge/policy/sensitive.py` | Sensitive-file policy: centralized case-insensitive rules | Yes / complete | Canonical path matching prevents link aliases hiding sensitive targets | Keep |
| 27 | `src/agentforge/tools/paths.py` | WorkspacePathResolver: Windows-aware canonical containment | Yes / partial | Correctly resolves before checking; add explicit empty/invalid/canonical sensitive-link tests | Modify tests, retain code unless tests expose defects |
| 28 | `src/agentforge/tools/repository/__init__.py` | Repository tools: controlled public exports | Yes / complete | No duplicate implementation | Keep |
| 29 | `src/agentforge/tools/repository/common.py` | Repository tools: shared traversal, UTF-8, ignore and sensitive rules | Yes / complete | Shared resolver is used; traversal does not follow symlinks | Keep |
| 30 | `src/agentforge/tools/repository/git_diff.py` | Repository tool: fixed read-only working-tree diff | Yes / partial | `shell=False` and fixed args are correct; inherited `GIT_*` values and unbounded initial capture need correction | Modify |
| 31 | `src/agentforge/tools/repository/list_files.py` | Repository tool: stable bounded file listing | Yes / complete | Reuses resolver/common traversal; no external-link following | Keep |
| 32 | `src/agentforge/tools/repository/read_file.py` | Repository tool: bounded strict UTF-8 file read | Yes / complete | Sensitive and binary failures are standardized; normal filesystem check/open race remains | Keep |
| 33 | `src/agentforge/tools/repository/search_text.py` | Repository tool: Python-only bounded text search | Yes / complete | No grep/rg/shell; skips sensitive, binary, invalid UTF-8, and oversized files | Keep |
| 34 | `tests/conftest.py` | Tests: autouse network denial | Yes / complete | Useful safety fixture; no production coupling | Keep |
| 35 | `tests/security/test_repository_tools.py` | Tests: real read-only repository tool behavior | Yes / partial | Real files/Git are exercised; add explicit encoding, glob/case, Git environment and capture-bound cases | Modify |
| 36 | `tests/security/test_workspace_paths.py` | Tests: path containment, links, and sensitive names | Yes / partial | Real links with explicit Windows skip; add empty, POSIX absolute, `.env.local`, `.key`, and sensitive-link cases | Modify |
| 37 | `tests/unit/test_policy.py` | Tests: structured allow/deny/approval/budget decisions | Yes / partial | Add explicit DANGEROUS and non-local-source denial evidence | Modify |
| 38 | `tests/unit/test_project_contract.py` | Tests: dependency source and honest README claims | Yes / complete | No dependency or lock churn | Keep |
| 39 | `tests/unit/test_tool_runtime.py` | Tests: registry, executor, audit, timeout, budgets, isolation | Yes / partial | Real functions are blocked by policy; add approval execution block, cancellation audit, invalid schema domain error, and stronger sync-timeout evidence | Modify |

## Decision

Retain all 39 original paths. None is proven to be an accidental duplicate or empty generated
artifact, so no deletion is justified. Correct the partial items using test-first changes, then
replace provisional documentation claims with fresh verification results.
