import tomllib
from pathlib import Path


def test_console_script_is_declared() -> None:
    data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["scripts"]["agentforge"] == "agentforge.cli.main:main"


def test_core_demo_scripts_use_only_public_cli() -> None:
    core_scripts = (
        Path("scripts/demo_core.ps1"),
        Path("scripts/demo_core.sh"),
    )
    for path in core_scripts:
        text = path.read_text(encoding="utf-8")
        assert "exec --workspace" in text
        assert "python -m agentforge" not in text
        assert "trust --workspace" in text
    for path in (
        Path("scripts/demo_recovery.ps1"),
        Path("scripts/demo_recovery.sh"),
    ):
        text = path.read_text(encoding="utf-8")
        assert "python -m agentforge" not in text
        assert "demo_core" in text


def test_core_demo_scripts_install_the_fresh_wheel_before_running_the_cli() -> None:
    powershell = Path("scripts/demo_core.ps1").read_text(encoding="utf-8")
    shell = Path("scripts/demo_core.sh").read_text(encoding="utf-8")

    assert "dist" in powershell
    assert "'pip', 'install'" in powershell
    assert "agentforge.exe" in powershell
    assert "$uvLog" in powershell
    assert "*> $uvLog" in powershell
    assert "Remove-Item -LiteralPath $uvLog" in powershell
    assert "function Invoke-UvSilently" in powershell
    assert "$ErrorActionPreference = 'Continue'" in powershell

    assert "dist" in shell
    assert "uv pip install" in shell
    assert "/bin/agentforge" in shell
    assert ">/dev/null 2>&1" in shell


def test_ci_builds_and_smoke_tests_the_wheel_on_supported_platforms() -> None:
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    for platform in ("windows-latest", "ubuntu-latest"):
        assert platform in workflow
    for python_version in ("3.11", "3.14"):
        assert python_version in workflow
    assert "uv build" in workflow
    assert "agentforge --help" in workflow


def test_demo_fixture_is_deterministic_and_has_no_secret_configuration() -> None:
    root = Path("examples/demo-repair")
    assert (root / "pyproject.toml").is_file()
    assert (root / "src/demo_calc.py").read_text(encoding="utf-8").endswith("- 1\n")
    assert "OPENAI_API_KEY" not in (root / "pyproject.toml").read_text(encoding="utf-8")
