# Milestone 7-B1 Risk Register

| ID | Risk | Affected candidates | Current control | B2 gate |
| --- | --- | --- | --- | --- |
| R1 | Historical license is ambiguous | `bugsinpy-tqdm-8` | Rejected; no source redistribution | Human license determination |
| R2 | Crop changes the original bug semantics | All public candidates; highest for FastAPI, Cookiecutter, pytest 5840 | CONDITIONAL where adaptation dominates | Compare buggy behavior and reference patch before/after crop |
| R3 | Historical dependencies do not support Python 3.14 | FastAPI, Pylint, pytest 5840, original Black/HTTPie suites | Plan minimal pre-provisioned subset; no runtime install | Build isolated dependency trial before fixture approval |
| R4 | Windows behavior is not reproduced faithfully | Black, pytest 5840 | Mark compatibility inferred | Run on real Windows; no simulated case-insensitive filesystem claim |
| R5 | Visible tests reveal the repair | One-line QuixBugs/BugsInPy tasks, Flask | Keep root-cause cases hidden; expose symptom only | Review visible/hidden split before model access |
| R6 | Hidden tests overfit the reference patch | All candidates | Use semantic matrices, properties, and multiple inputs | Mutation/negative-patch review |
| R7 | Self-built tasks copy AgentForge internals or tests | All self-built concepts | Require distinct domains, names, states, and independently authored tests | Manual leakage review |
| R8 | Official benchmark execution needs Docker or shell | SWE-bench and some BugsInPy harnesses | Never import harness as-is; propose fixed local TestProfile | Demonstrate offline execution without Docker/shell |
| R9 | Fixture includes excess third-party code | All public candidates | M7-B1 stores metadata only; artifact size tests | File-by-file inclusion and attribution review |
| R10 | Source/difficulty quotas override quality | Entire selection | Hard gates precede score and quotas | User may leave a quota unfilled rather than accept a weak task |
| R11 | Two graph QuixBugs tasks reduce diversity | QuixBugs primary pair | Explicitly disclosed | Consider one faithful non-graph alternate, not a forced swap |
| R12 | Durable self-built tasks overlap | Durable consumption and terminal-state CAS | Keep terminal-state task as alternate | Select at most one unless domains prove meaningfully distinct |

## Highest-priority B2 experiments

1. Prove the pytest caplog aliasing/lifecycle crop preserves the real failure and fix.
2. Prove Black and HTTPie focused tests run on Windows with no network and no old full-suite
   dependency installation.
3. Decide whether Cookiecutter can replace script execution with a recording dispatcher without
   changing the multiple-hook bug.
4. Reject or isolate FastAPI unless Pydantic-1 response semantics can be reproduced without a
   fragile Python 3.14 dependency environment.
5. Perform a leakage review of every self-built brief before any code is authored.

No risk in this register is evidence that a fixture has already passed. M7-B1 records candidate
quality and uncertainty only.
