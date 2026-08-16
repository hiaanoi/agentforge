from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Literal
from uuid import UUID

from agentforge.application.product_workspace import ProductWorkspaceCapture
from agentforge.application.views import DoctorCheckStatus, DoctorCheckView, DoctorReportView
from agentforge.persistence.database import Database
from agentforge.tools.testing.profiles import TestProfileRegistry


class Doctor:
    """Read-only, deliberately low-detail installation diagnostics."""

    def __init__(
        self,
        database: Database,
        profiles: TestProfileRegistry | None = None,
        *,
        provider_kind: Literal["openai", "deepseek", "mock"] = "openai",
    ) -> None:
        self._database = database
        self._profiles = profiles
        self._provider_kind = provider_kind

    def report(self, workspace: Path) -> DoctorReportView:
        checks = [
            self._schema(),
            self._workspace(workspace),
            self._source(workspace),
            self._provider_environment(),
            self._git(workspace),
        ]
        checks.append(self._profiles_check())
        return DoctorReportView(
            ready=all(check.status is DoctorCheckStatus.PASS for check in checks),
            checks=tuple(checks),
        )

    def _schema(self) -> DoctorCheckView:
        try:
            path = self._database.path
            if path is None:
                raise OSError("database path is unavailable")
            metadata = path.lstat()
            is_reparse = bool(
                getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            )
            if path.is_symlink() or is_reparse or not stat.S_ISREG(metadata.st_mode):
                raise OSError("database target is unsafe")
            self._database.validate_product_schema_read_only()
        except Exception:
            return DoctorCheckView(
                check="schema",
                status=DoctorCheckStatus.FAIL,
                safe_message="Product schema is unavailable.",
            )
        return DoctorCheckView(
            check="schema",
            status=DoctorCheckStatus.PASS,
            safe_message="Product schema is compatible.",
        )

    @staticmethod
    def _source(workspace: Path) -> DoctorCheckView:
        try:
            ProductWorkspaceCapture().capture(
                workspace,
                task_id="doctor-read-only",
                command_id=UUID(int=0),
            )
        except Exception:
            return DoctorCheckView(
                check="source",
                status=DoctorCheckStatus.FAIL,
                safe_message="Workspace source cannot be captured safely.",
            )
        return DoctorCheckView(
            check="source",
            status=DoctorCheckStatus.PASS,
            safe_message="Workspace source can be captured safely.",
        )

    @staticmethod
    def _workspace(workspace: Path) -> DoctorCheckView:
        try:
            valid = workspace.is_dir() and workspace.resolve(strict=True) == workspace.absolute()
        except OSError:
            valid = False
        return DoctorCheckView(
            check="workspace",
            status=DoctorCheckStatus.PASS if valid else DoctorCheckStatus.FAIL,
            safe_message="Workspace is available." if valid else "Workspace is unavailable.",
        )

    def _provider_environment(self) -> DoctorCheckView:
        if self._provider_kind == "mock":
            return DoctorCheckView(
                check="mock_environment",
                status=DoctorCheckStatus.PASS,
                safe_message="Mock provider requires no remote credentials.",
            )
        environment_name = (
            "OPENAI_API_KEY"
            if self._provider_kind == "openai"
            else "DEEPSEEK_API_KEY"
        )
        provider_name = "OpenAI" if self._provider_kind == "openai" else "DeepSeek"
        present = bool(os.environ.get(environment_name))
        return DoctorCheckView(
            check=f"{self._provider_kind}_environment",
            status=DoctorCheckStatus.PASS if present else DoctorCheckStatus.WARN,
            safe_message=f"{provider_name} credentials are configured."
            if present
            else f"{provider_name} credentials are not configured.",
        )

    @staticmethod
    def _git(workspace: Path) -> DoctorCheckView:
        # Never invoke git here: doctor must not start a subprocess.
        try:
            present = (workspace / ".git").exists()
        except OSError:
            present = False
        return DoctorCheckView(
            check="git",
            status=DoctorCheckStatus.PASS if present else DoctorCheckStatus.WARN,
            safe_message="Git metadata is available."
            if present
            else "Git metadata is not available.",
        )

    def _profiles_check(self) -> DoctorCheckView:
        if self._profiles is None:
            return DoctorCheckView(
                check="profile_bindings",
                status=DoctorCheckStatus.WARN,
                safe_message="No command profiles are configured.",
            )
        try:
            enabled = self._profiles.list_enabled()
        except Exception:
            return DoctorCheckView(
                check="profile_bindings",
                status=DoctorCheckStatus.FAIL,
                safe_message="Profile bindings cannot be read.",
            )
        return DoctorCheckView(
            check="profile_bindings",
            status=DoctorCheckStatus.PASS if enabled else DoctorCheckStatus.WARN,
            safe_message="Profile bindings are available."
            if enabled
            else "No command profiles are enabled.",
        )
