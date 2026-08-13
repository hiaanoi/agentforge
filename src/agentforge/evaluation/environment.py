import os
import sys
from collections.abc import Mapping
from typing import Literal

_FIXED_TEST_ENVIRONMENT = {
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONHASHSEED": "0",
    "PYTHONIOENCODING": "utf-8",
    "PYTHONNOUSERSITE": "1",
    "PYTHONUTF8": "1",
    "PYTEST_ADDOPTS": "-p no:cacheprovider",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
}


def build_fixed_test_environment(
    parent_environment: Mapping[str, str] | None = None,
    *,
    platform: Literal["WINDOWS", "POSIX"] | None = None,
) -> dict[str, str]:
    parent = os.environ if parent_environment is None else parent_environment
    family = platform or ("WINDOWS" if sys.platform == "win32" else "POSIX")
    environment = dict(_FIXED_TEST_ENVIRONMENT)
    if family == "WINDOWS":
        normalized = {name.upper(): value for name, value in parent.items()}
        for name in ("SYSTEMROOT", "WINDIR"):
            value = normalized.get(name)
            if value:
                if "\x00" in value:
                    raise ValueError(
                        "Windows system environment contains an invalid value"
                    )
                environment[name] = value
    return dict(sorted(environment.items()))
