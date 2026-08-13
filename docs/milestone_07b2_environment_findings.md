# Milestone 7-B2.0 Environment Findings

## Execution matrix

| Candidate | Source environment | Buggy 3x | Fixed 3x | Python 3.14 target evidence | Runtime range |
| --- | --- | --- | --- | --- | ---: |
| `quixbugs-shortest-path-length` | Python 3.14.3 | 2 failed / 2 passed each run | 4 passed each run | Original tests | 1083-1549 ms |
| `quixbugs-topological-ordering` | Python 3.14.3 | 3 failed each run | 3 passed each run | Original tests | 1111-1521 ms |
| `bugsinpy-black-21` | Python 3.8.20 | GBK `UnicodeEncodeError` | Exit 0 | Exact source helper also separated 3x | 842-1476 ms |
| `bugsinpy-pysnooper-3` | Python 3.8.20 | `NameError` | Exit 0 | Exact extracted function separated 3x | 250-487 ms |
| `bugsinpy-httpie-4` | Python 3.8.20, Requests 2.4.3 | Duplicate logical Host assertion | Exit 0 | Exact class with bounded mapping probe separated 3x | 206-908 ms |
| `swebench-flask-5014` | Python 3.10.20 | Official regression failed | Official regression passed | Direct source behavior separated 3x | 1168-2484 ms |
| `swebench-pytest-10051` | Python 3.10.20 | Official regression failed | Official regression passed | Direct capture lifecycle separated 3x | 918-1608 ms |

The runtime range measures whole process startup plus the bounded check. Digests are stored only
as execution evidence identifiers in machine assets; timing text makes some pytest output digests
differ while exit class and assertion summaries remain deterministic.

## Dependency findings

- QuixBugs executes directly on AgentForge's existing Python 3.14 test runtime.
- Black and PySnooper source revisions target the Python 3.6-3.8 era. Their causal logic remains
  executable on 3.14, but B2.1 must not carry the full historical dependency tree.
- HTTPie requires Requests 2.4.3 to reproduce the old source integration. The formal task should
  use a small attributed support mapping only after equivalence checks; the upstream regression's
  DNS and HTTP calls are forbidden.
- Flask's old full test harness uses pytest private APIs incompatible with pytest 9. The target
  source behavior itself runs on Python 3.14, so B2.1 should use a bounded application crop.
- pytest 7.2 can be installed from both pinned source states on Python 3.14. Its full historical
  suite is not the target; the capture-handler/stash lifecycle probe is sufficient for preflight.

Original dependency installation was external and disposable. It does not authorize runtime
installation in a formal TestProfile.
