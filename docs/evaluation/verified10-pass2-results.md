# Verified-10 Pass 2 Results

Pass 2 evaluated AgentForge and mini-SWE-agent on the same frozen ten public
SWE-bench Verified tasks with DeepSeek V4 Flash, temperature zero, one attempt,
and a 100-step/model-call ceiling. The official SWE-bench harness at commit
`4e6126978a16bdfebc6538db8f28cacc2c8b77dc` produced the final scores.

| Arm | Submitted | Non-empty patches | Resolved | Score |
| --- | ---: | ---: | ---: | ---: |
| AgentForge | 10 | 4 | 4 | 40% |
| mini-SWE-agent | 10 | 9 | 8 | 80% |

AgentForge resolved:

- `scikit-learn__scikit-learn-13142`
- `matplotlib__matplotlib-24026`
- `pytest-dev__pytest-7571`
- `sympy__sympy-16886`

mini-SWE-agent resolved all four AgentForge successes plus:

- `django__django-12050`
- `django__django-12419`
- `django__django-13343`
- `scikit-learn__scikit-learn-13496`

mini-SWE-agent submitted but did not resolve `django__django-13212`, and
produced an empty patch for `pylint-dev__pylint-8898`.

No arm had an official-harness infrastructure failure, ambiguous failure, or
harness error. AgentForge's four non-empty patches all resolved; however, its
runtime ledger classified every attempt as failed, including those four valid
patches. Six AgentForge tasks exhausted or terminated without a patch. This
shows that AgentForge's governance and patch-preservation path works, while its
repair loop and completion semantics remain materially weaker than the mini
baseline.

## Decision

Retain AgentForge's persistence, approval, audit, workspace provenance, and
official-evaluation boundaries. Replace or incorporate the mini-SWE-agent
repair loop behind those boundaries rather than continuing to increase the
native AgentForge model-call budget. Pass 2 already doubled the ceiling and
mini still resolved twice as many tasks.

## Artifact digests

All digests are SHA-256:

| Artifact | Digest |
| --- | --- |
| Protocol | `972eaa54ac2b9a1b66f0faf85fedfbf56eeacd5cc7a4b7085e99d876914c7171` |
| Campaign state | `7c21b9361878a3e0572492861c9a18eeca3add02074ece1f7e95ecc3a2d2e7b5` |
| AgentForge predictions | `d49e768dd47e24ce957591f5ab3a59621c5a306d957fe0e660fa22de22da544e` |
| mini-SWE-agent predictions | `7a17612f3aa08ad3546f14327382d09a749871b306c2f127b13e63f1fa7ae804` |
| AgentForge official report | `082c034279c86964ed4545720eb756170e7c31a54977901ab533e21f436e6088` |
| mini-SWE-agent official report | `32b786b794975df1108065f0829f79f0ea400169048936de8ee64e77d4c6292e` |
