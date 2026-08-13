# Milestone 7-B1 License and Provenance Audit

## Interpretation boundary

This is an engineering provenance record, not legal advice. A verified license means the named
license file was checked at the pinned upstream revision. B2 must preserve the applicable
copyright, license, notice, modification, and source-distribution obligations.

## Source findings

### QuixBugs

The pinned QuixBugs snapshot contains buggy Python sources, paired corrected Python sources,
pytest tests, and an [MIT license](https://github.com/jkoppel/QuixBugs/blob/4257f44b0ff1181dedaedee6a447e133219fcebf/LICENSE).
Buggy and corrected files coexist at one revision rather than forming separate buggy/fixed
commits. Candidate records therefore use the same pinned snapshot for both fields and cite the
two distinct file paths. Any fixture must retain the MIT notice.

### BugsInPy

The BugsInPy repository snapshot used for metadata does not contain a top-level LICENSE. The
audit therefore does not infer redistribution rights from BugsInPy itself. It verifies each
upstream project at the candidate's buggy commit:

| Candidate project | License finding | Status |
| --- | --- | --- |
| Black | MIT | Verified |
| PySnooper | MIT | Verified |
| HTTPie | BSD-3-Clause | Verified |
| Cookiecutter | BSD-3-Clause | Verified |
| FastAPI | MIT | Verified |
| tqdm historical snapshot | GitHub SPDX `NOASSERTION`; historical `LICENCE` needs manual interpretation | Unverified/rejected |

The BugsInPy `bug.info` and `bug_patch.txt` files establish commit/test metadata; upstream
commit and license URLs establish the source and redistribution facts. B2 must record
attribution for the upstream project and should also credit BugsInPy as the benchmark source.

### SWE-bench Verified

The [SWE-bench repository](https://github.com/SWE-bench/SWE-bench) is MIT licensed, but every
task also inherits the source project's license. The audited candidates are:

| Project | License | Candidate status |
| --- | --- | --- |
| Flask | BSD-3-Clause | Primary eligible |
| pytest | MIT | One primary, one conditional alternate |
| Requests | Apache-2.0 | Conditional alternate |
| Pylint | GPL-2.0 | Conditional alternate |

Apache-2.0 requires preservation of license/notices and marking modified files. A Pylint-derived
fixture would need a deliberate GPL source-distribution plan; its conditional status is not a
license rejection, but the compliance cost weighs against primary selection.

SWE-bench Verified provides the base commit, gold patch, regression tests, and problem statement.
Original issue/PR and merge commits were separately checked. Official SWE-bench reproducibility
uses Docker; that does not prove an AgentForge offline local TestProfile can run a crop.

### Self-built concepts

No code exists for the five self-built concepts. They are marked as original design concepts,
not licensed fixtures. Their technical details are inferred proposals. Before implementation,
B2 must confirm that new code and tests are independently authored and do not copy AgentForge
runtime code or existing tests.

## Open provenance risks

- tqdm remains rejected until a human determines the exact historical license and obligations.
- QuixBugs uses paired files rather than a commit transition; attribution must retain the
  benchmark context.
- Cropping can create a derivative work even when only a small source subset is used.
- Rewriting a task from behavior alone may avoid source copying but can also change bug semantics;
  the choice must be documented per fixture.
- A license verified today does not authorize adding an entire upstream repository; M7-B2 must
  include only the minimal reviewed files and notices.
