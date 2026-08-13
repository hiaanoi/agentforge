from agentforge.domain.repair import SuspiciousFindingKind
from agentforge.evaluation.validators import SuspiciousChangeAnalyzer


def kinds(source: str, path: str = "src/module.py") -> set[SuspiciousFindingKind]:
    return {finding.kind for finding in SuspiciousChangeAnalyzer().analyze(path, source)}


def test_detects_common_test_bypass_patterns() -> None:
    source = """
import sys
import pytest

pytest.skip("disabled")
pytest.xfail("known")
if "pytest" in sys.modules:
    sys.path.insert(0, "fake")
"""

    found = kinds(source)

    assert SuspiciousFindingKind.TEST_SKIP in found
    assert SuspiciousFindingKind.TEST_XFAIL in found
    assert SuspiciousFindingKind.TEST_ENVIRONMENT_BRANCH in found
    assert SuspiciousFindingKind.SYS_PATH_MANIPULATION in found


def test_detects_site_customization_and_dynamic_test_imports() -> None:
    assert SuspiciousFindingKind.SITE_CUSTOMIZATION in kinds("import os\n", path="sitecustomize.py")
    assert SuspiciousFindingKind.DYNAMIC_TEST_IMPORT in kinds(
        "import importlib\nimportlib.import_module('tests.hidden')\n"
    )


def test_benign_application_code_has_no_findings() -> None:
    source = "def calculate(value: int) -> int:\n    return value + 1\n"

    assert SuspiciousChangeAnalyzer().analyze("src/calculate.py", source) == ()
