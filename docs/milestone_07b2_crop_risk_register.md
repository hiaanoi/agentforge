# Milestone 7-B2.0 Crop Risk Register

| ID | Candidate | Risk | Gate | B2.1 control |
| --- | --- | --- | --- | --- |
| C1 | `quixbugs-shortest-path-length` | A visible one-line relaxation can be too obvious | GO | Keep graph construction and queue behavior in separate modules; hide alternate routes |
| C2 | `quixbugs-topological-ordering` | Upstream tests require one exact valid ordering | CONDITIONAL | Use an edge-order property oracle and prove alternate valid orders pass |
| C3 | `bugsinpy-black-21` | Helper-only crop loses formatter diagnostic context | GO | Preserve caller, diagnostic serialization, and reopen boundary |
| C4 | `bugsinpy-pysnooper-3` | One-token fix collapses into a closure toy | CONDITIONAL | Retain decorator, tracer, writer, and compatibility modules |
| C5 | `bugsinpy-httpie-4` | Replacing Requests may change case-insensitive mapping semantics | CONDITIONAL | Compare local mapping membership, iteration, and duplicate behavior against Requests 2.4.3 |
| C6 | `swebench-flask-5014` | Constructor-only visible test exposes repair location | GO | Expose application registration symptom and keep constructor edges hidden |
| C7 | `swebench-pytest-10051` | Merge contains unrelated changes; over-crop removes aliasing | GO | Use PR first-parent target diff and retain stash/list identity lifecycle |
| C8 | `self-durable-double-consumption` | Names or tests leak AgentForge durable-runtime design | GO | Use parcel-domain terms and independent crash-window tests |
| C9 | `self-policy-priority-shadow` | Domain resembles AgentForge Policy Engine | GO | Use route-rule semantics and an independent ranking contract |
| C10 | `self-async-cancel-cleanup` | Sleep-based cancellation tests become flaky | CONDITIONAL | Require explicit barriers and repeated no-background-task assertions |

No disposable probe is a formal fixture. `GO` means construction is justified, not that crop
fidelity has already been accepted after implementation.
