# Milestone 7-B2.0 Preflight Protocol

## Scope

This protocol decides whether the ten M7-B1 human-approved candidates may enter formal fixture
construction. It does not create a fixture, formal visible or hidden test, reference fix, model
run, prompt change, or M7-B2.1 implementation.

## Evidence layers

1. **Pinned source evidence:** official repository, issue or pull request, buggy revision, fixed
   revision, regression test, and source-project license.
2. **Source execution:** the narrowest trustworthy buggy and fixed check, repeated three times
   in an external disposable environment. Exit class, bounded summary digest, and runtime range
   are recorded; raw output and machine paths are not retained.
3. **Target probe:** Python 3.14, Windows, offline, fixed executable/cwd/argv/environment, and a
   dependency-bounded causal slice. A probe is disposable evidence, not a fixture.
4. **Design review:** visible symptom, hidden invariants, incorrect patches, protected paths,
   expected edit rounds, and leakage controls. Self-built candidates stop at this layer.

Every key conclusion is classified as `VERIFIED`, `INFERRED`, or `UNVERIFIED`. In particular,
self-built failure reproduction and reference-fix execution remain `UNVERIFIED` until B2.1
authors the fixture.

## Gate rules

- `GO`: source/design facts close, deterministic buggy/fixed evidence exists where applicable,
  target execution is local and bounded, and no unresolved admission condition remains.
- `CONDITIONAL`: the core bug is valid, but one or two named B2.1 conditions must pass before
  formal admission.
- `NO_GO`: a hard requirement fails, including license, reproducibility, offline execution,
  semantic crop fidelity, or hidden-test quality.
- `UNVERIFIED`: available evidence cannot support a decision. It is never treated as GO.

Difficulty is reviewed from files, causal distance, patch size, feedback rounds, and hidden-test
complexity only. Model performance is not an input.

## Isolation

All source clones, managed Python 3.8/3.10 environments, Python 3.14 audit environments, and
disposable probes live outside AgentForge. Candidate tests receive a cleared environment with
only Windows runtime directories, temporary directories, UTF-8/no-user-site controls, and an
explicit source path where required. No credential variable is inherited.
