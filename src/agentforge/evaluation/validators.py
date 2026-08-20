from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.models import UtcDatetime, utc_now
from agentforge.domain.repair import (
    DiffViolationKind,
    RepairTaskPolicy,
    SuspiciousFindingKind,
)
from agentforge.evaluation.workspace import (
    FileContentKind,
    WorkspaceBaseline,
    WorkspaceBaselineBuilder,
    WorkspaceFileBaseline,
    WorkspaceScan,
)
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.paths import WorkspacePathResolver


class DiffViolation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: DiffViolationKind
    path: str | None = None
    related_path: str | None = None


class SuspiciousFinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: SuspiciousFindingKind
    path: str
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class DiffValidationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    validation_id: str = Field(default_factory=lambda: str(uuid4()))
    compliant: bool
    baseline_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    final_workspace_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    diff_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    modified_files: tuple[str, ...]
    created_files: tuple[str, ...]
    deleted_files: tuple[str, ...]
    renamed_files: tuple[tuple[str, str], ...]
    type_changed_files: tuple[str, ...]
    changed_file_count: int = Field(ge=0)
    total_changed_bytes: int = Field(ge=0)
    violations: tuple[DiffViolation, ...]
    suspicious_findings: tuple[SuspiciousFinding, ...]
    validation_version: int = 1
    created_at: UtcDatetime = Field(default_factory=utc_now)


class SuspiciousChangeAnalyzer:
    _RULES: tuple[tuple[SuspiciousFindingKind, re.Pattern[str]], ...] = (
        (
            SuspiciousFindingKind.TEST_SKIP,
            re.compile(r"(?:pytest\.skip|unittest\.skip|@pytest\.mark\.skip)"),
        ),
        (
            SuspiciousFindingKind.TEST_XFAIL,
            re.compile(r"(?:pytest\.xfail|@pytest\.mark\.xfail)"),
        ),
        (
            SuspiciousFindingKind.TEST_ENVIRONMENT_BRANCH,
            re.compile(
                r"(?:PYTEST_CURRENT_TEST|[\"']pytest[\"']\s+in\s+sys\.modules|inspect\.stack\s*\()"
            ),
        ),
        (
            SuspiciousFindingKind.SYS_PATH_MANIPULATION,
            re.compile(r"sys\.path\.(?:insert|append|extend)|sys\.path\s*="),
        ),
        (
            SuspiciousFindingKind.DYNAMIC_TEST_IMPORT,
            re.compile(r"(?:import_module|__import__)\s*\([^\n]*(?:tests?|pytest)"),
        ),
        (
            SuspiciousFindingKind.FIXTURE_SPECIFIC_BRANCH,
            re.compile(r"if\s+[^\n]*(?:fixture|test_case|hidden_test)"),
        ),
    )

    def analyze(self, relative_path: str, source: str) -> tuple[SuspiciousFinding, ...]:
        findings: list[SuspiciousFinding] = []
        name = Path(relative_path).name.casefold()
        if name in {"sitecustomize.py", "usercustomize.py"}:
            findings.append(
                self._finding(SuspiciousFindingKind.SITE_CUSTOMIZATION, relative_path, name)
            )
        for kind, pattern in self._RULES:
            match = pattern.search(source)
            if match is not None:
                findings.append(self._finding(kind, relative_path, match.group(0)))
        return tuple(findings)

    @staticmethod
    def _finding(
        kind: SuspiciousFindingKind,
        path: str,
        evidence: str,
    ) -> SuspiciousFinding:
        return SuspiciousFinding(
            kind=kind,
            path=path,
            evidence_digest=hashlib.sha256(evidence.encode("utf-8")).hexdigest(),
        )


class WorkspaceDiffValidator:
    def __init__(
        self, resolver: WorkspacePathResolver, *, scanner: WorkspaceScanner | None = None
    ) -> None:
        self._resolver = resolver
        self._scanner: WorkspaceScanner = scanner or WorkspaceBaselineBuilder(resolver)
        self._sensitive = SensitiveFilePolicy()
        self._suspicious = SuspiciousChangeAnalyzer()

    def validate(
        self,
        baseline: WorkspaceBaseline,
        policy: RepairTaskPolicy,
    ) -> DiffValidationResult:
        scan = self._scanner.scan()
        baseline_root = os.path.normcase(str(Path(baseline.workspace_root).resolve()))
        current_root = os.path.normcase(str(self._resolver.workspace))
        base = {item.relative_path: item for item in baseline.files}
        current = {item.relative_path: item for item in scan.files}
        modified = sorted(
            path
            for path in base.keys() & current.keys()
            if self._metadata_changed(base[path], current[path])
        )
        created = sorted(current.keys() - base.keys())
        deleted = sorted(base.keys() - current.keys())
        type_changed = sorted(
            path
            for path in base.keys() & current.keys()
            if base[path].file_kind != current[path].file_kind
        )
        renamed = self._rename_like(base, current, deleted, created)
        changed_paths = sorted(set(modified) | set(created) | set(deleted))
        total_bytes = sum(self._changed_size(path, base, current) for path in changed_paths)
        violations: list[DiffViolation] = []
        if baseline_root != current_root:
            violations.append(DiffViolation(kind=DiffViolationKind.BASELINE_MISMATCH))
        for path in modified:
            self._path_violations(path, creating=False, policy=policy, target=violations)
        for path in created:
            self._path_violations(path, creating=True, policy=policy, target=violations)
            if self._is_link_or_reparse(current[path]):
                violations.append(
                    DiffViolation(
                        kind=DiffViolationKind.SYMLINK_OR_REPARSE_CREATED,
                        path=path,
                    )
                )
        for path in deleted:
            violations.append(DiffViolation(kind=DiffViolationKind.FILE_DELETED, path=path))
            self._path_violations(path, creating=False, policy=policy, target=violations)
        for old, new in renamed:
            violations.append(
                DiffViolation(kind=DiffViolationKind.FILE_RENAMED, path=old, related_path=new)
            )
        for path in type_changed:
            violations.append(DiffViolation(kind=DiffViolationKind.FILE_TYPE_CHANGED, path=path))
        for path in modified:
            if (
                base[path].file_kind == current[path].file_kind
                and (
                    self._is_link_or_reparse(base[path])
                    or self._is_link_or_reparse(current[path])
                )
            ):
                violations.append(
                    DiffViolation(
                        kind=DiffViolationKind.SYMLINK_OR_REPARSE_CHANGED,
                        path=path,
                    )
                )
        for path in changed_paths:
            if self._sensitive.match(path) is not None:
                violations.append(
                    DiffViolation(kind=DiffViolationKind.SENSITIVE_FILE_TOUCHED, path=path)
                )
            if _is_test_infrastructure(path):
                violations.append(
                    DiffViolation(
                        kind=DiffViolationKind.TEST_INFRASTRUCTURE_MODIFIED,
                        path=path,
                    )
                )
            if self._changed_size(path, base, current) > policy.max_single_file_changed_bytes:
                violations.append(
                    DiffViolation(
                        kind=DiffViolationKind.SINGLE_FILE_CHANGE_TOO_LARGE,
                        path=path,
                    )
                )
        if len(changed_paths) > policy.max_changed_files:
            violations.append(DiffViolation(kind=DiffViolationKind.TOO_MANY_FILES_CHANGED))
        if total_bytes > policy.max_total_changed_bytes:
            violations.append(DiffViolation(kind=DiffViolationKind.CHANGESET_TOO_LARGE))
        if len(created) > policy.max_created_files:
            violations.append(DiffViolation(kind=DiffViolationKind.TOO_MANY_FILES_CHANGED))

        findings = self._analyze_changed_text(current, modified + created)
        for finding in findings:
            violations.append(
                DiffViolation(
                    kind=DiffViolationKind.SUSPICIOUS_TEST_BYPASS,
                    path=finding.path,
                )
            )
        violations = _deduplicate_violations(violations)
        diff_digest = _diff_digest(
            baseline.root_digest,
            scan.root_digest,
            modified,
            created,
            deleted,
            renamed,
            type_changed,
            violations,
            findings,
        )
        return DiffValidationResult(
            compliant=not violations,
            baseline_digest=baseline.root_digest,
            final_workspace_digest=scan.root_digest,
            diff_digest=diff_digest,
            modified_files=tuple(modified),
            created_files=tuple(created),
            deleted_files=tuple(deleted),
            renamed_files=tuple(renamed),
            type_changed_files=tuple(type_changed),
            changed_file_count=len(changed_paths),
            total_changed_bytes=total_bytes,
            violations=tuple(violations),
            suspicious_findings=tuple(findings),
        )

    @staticmethod
    def _metadata_changed(
        before: WorkspaceFileBaseline,
        after: WorkspaceFileBaseline,
    ) -> bool:
        return (
            before.sha256 != after.sha256
            or before.size_bytes != after.size_bytes
            or before.executable_bit != after.executable_bit
            or before.file_kind != after.file_kind
            or before.is_symlink != after.is_symlink
            or before.is_reparse_point != after.is_reparse_point
            or before.content_kind != after.content_kind
        )

    @staticmethod
    def _is_link_or_reparse(entry: WorkspaceFileBaseline) -> bool:
        return entry.is_symlink or entry.is_reparse_point

    @staticmethod
    def _rename_like(
        base: dict[str, WorkspaceFileBaseline],
        current: dict[str, WorkspaceFileBaseline],
        deleted: list[str],
        created: list[str],
    ) -> list[tuple[str, str]]:
        renamed: list[tuple[str, str]] = []
        for old in deleted:
            matches = [
                new
                for new in created
                if (
                    current[new].file_kind == base[old].file_kind
                    and current[new].sha256 == base[old].sha256
                    and current[new].size_bytes == base[old].size_bytes
                )
            ]
            if len(matches) == 1:
                renamed.append((old, matches[0]))
        return sorted(renamed)

    @staticmethod
    def _changed_size(
        path: str,
        base: dict[str, WorkspaceFileBaseline],
        current: dict[str, WorkspaceFileBaseline],
    ) -> int:
        before = base.get(path)
        after = current.get(path)
        return max(
            before.size_bytes if before is not None else 0,
            after.size_bytes if after is not None else 0,
        )

    def _path_violations(
        self,
        path: str,
        *,
        creating: bool,
        policy: RepairTaskPolicy,
        target: list[DiffViolation],
    ) -> None:
        rule = policy.write_rule(path, creating=creating)
        if rule == "FORBIDDEN":
            target.append(DiffViolation(kind=DiffViolationKind.FORBIDDEN_PATH_MODIFIED, path=path))
        elif rule == "PROTECTED":
            target.append(DiffViolation(kind=DiffViolationKind.PROTECTED_FILE_MODIFIED, path=path))
        elif creating and rule != "ALLOWED":
            target.append(
                DiffViolation(kind=DiffViolationKind.UNAUTHORIZED_FILE_CREATED, path=path)
            )
        elif rule != "ALLOWED":
            target.append(
                DiffViolation(kind=DiffViolationKind.EXTERNAL_WORKSPACE_CHANGE, path=path)
            )

    def _analyze_changed_text(
        self,
        current: dict[str, WorkspaceFileBaseline],
        paths: list[str],
    ) -> list[SuspiciousFinding]:
        findings: list[SuspiciousFinding] = []
        for path in paths:
            entry = current.get(path)
            if (
                entry is None
                or entry.is_symlink
                or entry.is_reparse_point
                or entry.content_kind is not FileContentKind.TEXT
            ):
                continue
            if entry.size_bytes > 1_000_000:
                continue
            source = (self._resolver.workspace / Path(path)).read_text(encoding="utf-8")
            findings.extend(self._suspicious.analyze(path, source))
        return findings


def _is_test_infrastructure(path: str) -> bool:
    candidate = Path(path)
    name = candidate.name.casefold()
    parts = tuple(part.casefold() for part in candidate.parts)
    return bool(
        parts[:1] in {("tests",), ("test",)}
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name
        in {
            "conftest.py",
            "pytest.ini",
            "pyproject.toml",
            "setup.cfg",
            "tox.ini",
            "noxfile.py",
            "uv.lock",
            "sitecustomize.py",
            "usercustomize.py",
        }
        or (name.startswith("requirements") and name.endswith(".txt"))
    )


def _deduplicate_violations(items: list[DiffViolation]) -> list[DiffViolation]:
    unique: dict[tuple[str, str | None, str | None], DiffViolation] = {}
    for item in items:
        key = (item.kind.value, item.path, item.related_path)
        unique[key] = item
    return [unique[key] for key in sorted(unique)]


def _diff_digest(
    baseline_digest: str,
    final_digest: str,
    modified: list[str],
    created: list[str],
    deleted: list[str],
    renamed: list[tuple[str, str]],
    type_changed: list[str],
    violations: list[DiffViolation],
    findings: list[SuspiciousFinding],
) -> str:
    payload = {
        "baseline_digest": baseline_digest,
        "final_digest": final_digest,
        "modified": modified,
        "created": created,
        "deleted": deleted,
        "renamed": renamed,
        "type_changed": type_changed,
        "violations": [item.model_dump(mode="json") for item in violations],
        "findings": [item.model_dump(mode="json") for item in findings],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class WorkspaceScanner(Protocol):
    def scan(self) -> WorkspaceScan: ...
