# Milestone 7-B1 Selection Protocol

## Decision order

Selection uses four ordered gates. A high score cannot override an earlier gate.

1. **Provenance and license:** source, buggy state, fix source, test evidence, and redistribution
   terms must be traceable. Unknown licenses are rejected from the primary set.
2. **Runtime admissibility:** the proposed crop must be local, offline, deterministic, bounded,
   and compatible with an immutable TestProfile without runtime installation.
3. **Evaluation quality:** visible and hidden tests must separate diagnosis from validation and
   provide a credible anti-hardcoding strategy.
4. **Portfolio composition:** source quota, bug type, difficulty, multi-module reasoning, test
   feedback, and demo value are considered after individual eligibility.

`CONDITIONAL` means a named gate is unresolved; it does not mean the candidate is almost
eligible. `REJECTED` means the current evidence violates a hard gate or has insufficient value
to justify further work.

## Frozen scoring rubric

| Category | Maximum | Audit question |
| --- | ---: | --- |
| Reproducibility | 20 | Are buggy behavior, fix, and deterministic tests traceable? |
| Runtime compatibility | 20 | Can a fixed offline TestProfile execute the proposed crop? |
| Engineering realism | 15 | Does diagnosis cross meaningful contracts or modules? |
| Hidden-test strength | 15 | Can hidden tests detect overfitting and partial fixes? |
| Adaptation cost | 10 | Can the task remain small without semantic drift? |
| Diversity contribution | 10 | Does it add a distinct bug/source/reasoning pattern? |
| Main demo value | 5 | Is the symptom and repair story clear to an observer? |
| Provenance confidence | 5 | Are source and license claims first-party and pinned? |

Each score object records the score, maximum, evidence basis, uncertainty, and deduction basis.
Scores are audit judgments and are therefore marked `INFERRED`, even when they rely on verified
source facts.

## Primary composition

The proposed primary source mix is fixed at:

- 2 QuixBugs adaptations
- 3 BugsInPy crops
- 2 SWE-bench Verified crops
- 3 self-built engineering fixtures

The resulting difficulty proposal is 6 BASIC, 3 ENGINEERING, and 1 CHALLENGE. The labels are
based on expected repair behavior after adaptation, not upstream benchmark difficulty alone.
M7-B2 must revise a label if the built fixture materially changes expected edit rounds, test
feedback, or cross-module reasoning.

## Asset integrity rules

- `candidates.json` is canonical; CSV and selection files are generated projections.
- Candidate IDs are globally unique and every selection list is disjoint.
- Primary candidates must be `ELIGIBLE` and `license_verified=true`.
- Every field carries `VERIFIED`, `INFERRED`, or `UNVERIFIED` evidence metadata.
- Checked-in candidate assets must contain no local absolute path, credential-like value, or
  third-party source archive.
- `artifact_registry.json` records SHA-256 and size for every candidate asset except itself.

## Human decision record

The user accepted the ten-item primary list without replacements, accepted the 6/3/1 difficulty
mix, chose `self-durable-double-consumption` as the main demo, and registered
`self-terminal-state-cas` as the demo backup. CONDITIONAL candidates will be validated in
source-tiered order. The decision authorizes M7-B2.0 preflight only.

No source download into AgentForge, formal fixture implementation, hidden test, reference fix,
model pilot, budget/prompt change, or M7-B2.1 work is authorized by this audit.
