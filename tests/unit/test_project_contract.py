import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _readme_section(readme: str, heading: str) -> str:
    heading_match = re.search(rf"(?m)^## .*{re.escape(heading)}.*$", readme)
    assert heading_match is not None
    start = heading_match.end()
    remainder = readme[start:]
    next_heading = re.search(r"(?m)^## .+$", remainder)
    return remainder if next_heading is None else remainder[: next_heading.start()]


def _resolve_repo_link(target: str) -> Path:
    candidate = Path(target)
    assert not candidate.is_absolute()
    resolved = (PROJECT_ROOT / candidate).resolve()
    resolved.relative_to(PROJECT_ROOT.resolve())
    return resolved


def test_lock_uses_official_pypi_and_covers_project_dependencies() -> None:
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    lock_text = (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8")
    dependencies = pyproject["project"]["dependencies"]

    assert any(dependency.startswith("pydantic") for dependency in dependencies)
    assert any(dependency.startswith("sqlalchemy") for dependency in dependencies)
    assert 'registry = "https://pypi.org/simple"' in lock_text
    assert "mirrors.aliyun.com" not in lock_text


def test_readme_distinguishes_implemented_and_deferred_capabilities() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for implemented in (
        "list_files",
        "read_file",
        "search_text",
        "get_git_diff",
        "OpenAI Responses",
        "write_file",
        "edit_file",
        "run_tests",
    ):
        assert implemented.casefold() in readme.casefold()
    for deferred in (
        "arbitrary shell",
        "automatic bug repair",
        "MCP server",
        "web API",
        "additional model providers",
    ):
        assert deferred.casefold() in readme.casefold()


def test_readme_evidence_section_publishes_only_schema_v2_contract() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    evidence = _readme_section(readme, "Evidence")
    evidence_text = " ".join(evidence.casefold().split())

    for legacy_claim in (
        "12/12 slots",
        "pass@1 and pass@3 of 4/4",
        "pass@1/pass@3 4/4",
        "3/3 successful",
        "first repetition",
    ):
        assert legacy_claim not in evidence_text

    assert "schema v2" in evidence_text
    assert "standard pass@k estimator" in evidence_text
    assert "pass@1 = c / n" in evidence_text
    assert "n = 3" in evidence_text
    assert "pass@3" in evidence_text
    assert "first-attempt success" in evidence_text
    assert "ineligible" in evidence_text
    assert "curated" in evidence_text
    assert "non-official" in evidence_text
    assert "完整、校验通过的 schema-v2 evidence bundle" in evidence_text

    links = re.findall(r"\[[^\]]+\]\(([^)]+)\)", evidence)
    legacy_links = [
        target
        for target in links
        if target.replace("\\", "/").casefold() == "evaluation/results/legacy.md"
    ]
    assert len(legacy_links) == 1
    assert _resolve_repo_link(legacy_links[0]).is_file()


def test_project_contract_rejects_repo_link_path_traversal() -> None:
    try:
        _resolve_repo_link("../outside.md")
    except ValueError:
        pass
    else:
        raise AssertionError("repository documentation links must stay inside the repository")


def test_public_release_has_mit_license_and_package_metadata() -> None:
    license_text = (PROJECT_ROOT / "LICENSE").read_text(encoding="utf-8")
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert license_text.startswith("MIT License\n")
    assert "Copyright (c) 2026 hiaanoi" in license_text
    assert "THE SOFTWARE IS PROVIDED \"AS IS\"" in license_text
    assert pyproject["project"]["license"] == "MIT"
    assert pyproject["project"]["urls"]["Repository"] == "https://github.com/hiaanoi/agentforge"


def test_ci_runs_the_complete_offline_suite() -> None:
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    runner = (PROJECT_ROOT / "scripts" / "run_ci_tests.py").read_text(encoding="utf-8")

    assert "Prepare link-free project interpreter (Linux)" in workflow
    assert workflow.count("scripts/run_ci_tests.py") == 2
    assert ".venv/bin/python-link-free scripts/run_ci_tests.py" in workflow
    assert "mv --force .venv/bin/python-link-free" not in workflow
    assert "mv --force .wheel-venv/bin/python-link-free" in workflow
    assert "ruff check src tests scripts" in workflow
    assert "mypy --platform win32 src scripts" in workflow
    for suite in ("unit", "integration", "cli", "docs", "evaluation", "process", "security"):
        assert f'"tests" / "{suite}"' in runner or f'"tests/{suite}"' in runner
    assert 'PYTEST_ARGS = ("-m", "not live", "-q")' in runner
    assert "tests/unit tests/integration tests/cli" not in workflow


def test_recovery_demo_invokes_core_demo_through_posix_shell() -> None:
    recovery_demo = (PROJECT_ROOT / "scripts" / "demo_recovery.sh").read_text(
        encoding="utf-8"
    )

    assert 'exec sh "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)/demo_core.sh"' in recovery_demo


def test_readme_python_requirement_matches_package_metadata() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["project"]["requires-python"] == ">=3.11"
    assert "Python 3.11+" in readme
