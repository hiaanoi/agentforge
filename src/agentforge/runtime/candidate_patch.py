from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from agentforge.domain.enums import ToolErrorCode, ToolRisk
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.domain.mutations import MutationPlan
from agentforge.tools.mutation.atomic import AtomicMutationWriter, file_sha256
from agentforge.tools.mutation.security import MutationSecurityPolicy
from agentforge.tools.paths import WorkspacePathResolver


@dataclass(frozen=True, slots=True)
class CandidatePatchEntry:
    target_path: str
    data: bytes
    plan: MutationPlan


@dataclass(frozen=True, slots=True)
class CandidatePatch:
    entries: tuple[CandidatePatchEntry, ...]


class CandidatePatchStore:
    _FILENAME = "candidate-patch.json"

    def __init__(self, canonical_root: Path) -> None:
        self._canonical_root = canonical_root.resolve(strict=True)
        self._root = self._canonical_root / ".agentforge" / "candidates"

    def save(self, run_key: str, patch: CandidatePatch) -> Path:
        if not run_key or Path(run_key).name != run_key:
            raise ValueError("Candidate patch run key must be one path component")
        path = self._root / run_key / self._FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "entries": [
                {
                    "target_path": entry.target_path,
                    "data_base64": base64.b64encode(entry.data).decode("ascii"),
                    "plan": entry.plan.model_dump(mode="json"),
                }
                for entry in patch.entries
            ],
        }
        path.write_text(
            json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
        )
        return path

    def path_for(self, run_id: UUID) -> Path:
        return self._root / str(run_id) / self._FILENAME

    def load(self, path: Path) -> CandidatePatch:
        resolved = path.resolve(strict=True)
        try:
            relative = resolved.relative_to(self._root.resolve(strict=True))
        except ValueError as exc:
            raise ValueError("Candidate patch manifest lies outside the candidate store") from exc
        if len(relative.parts) != 2 or relative.name != self._FILENAME:
            raise ValueError("Candidate patch manifest path is invalid")
        try:
            payload = json.loads(resolved.read_text(encoding="utf-8"))
            if payload.get("schema_version") != 1 or not isinstance(payload.get("entries"), list):
                raise ValueError
            entries = tuple(
                CandidatePatchEntry(
                    target_path=item["target_path"],
                    data=base64.b64decode(item["data_base64"], validate=True),
                    plan=MutationPlan.model_validate(item["plan"]),
                )
                for item in payload["entries"]
            )
        except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
            raise ValueError("Candidate patch manifest is invalid") from exc
        for entry in entries:
            if entry.target_path != entry.plan.target_path:
                raise ValueError("Candidate patch manifest target does not match its plan")
            if hashlib.sha256(entry.data).hexdigest() != entry.plan.expected_after_sha256:
                raise ValueError("Candidate patch manifest bytes do not match its plan")
        return CandidatePatch(entries=entries)


class CandidatePatchPublishArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID


class CandidatePatchPublishTool:
    def __init__(self, publisher: CandidatePatchPublisher, store: CandidatePatchStore) -> None:
        self._publisher = publisher
        self._store = store

    @property
    def input_model(self) -> type[BaseModel]:
        return CandidatePatchPublishArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="publish_candidate_patch",
            description="Publish the saved candidate patch after final approval.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.DANGEROUS,
            requires_approval=True,
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = CandidatePatchPublishArguments.model_validate(arguments)
        patch = self._store.load(self._store.path_for(parsed.run_id))
        self._publisher.publish(patch)
        return ToolResult(
            success=True,
            output={"entries": len(patch.entries), "status": "published"},
        )


class CandidatePatchPublisher:
    """Captures a candidate Git worktree and applies its text-file edits on approval.

    The caller deliberately controls the approval boundary: ``capture`` only reads the
    candidate, while ``publish`` is the single operation that changes the canonical
    workspace.
    """

    def __init__(self, *, canonical_root: Path, security: MutationSecurityPolicy) -> None:
        self._canonical_root = canonical_root.resolve(strict=True)
        self._security = security
        if self._security.resolver.workspace != self._canonical_root:
            raise ValueError("Patch security policy must belong to the canonical workspace")
        self._writer = AtomicMutationWriter()

    def capture(self, candidate_root: Path) -> CandidatePatch:
        candidate = candidate_root.resolve(strict=True)
        changed_paths = self._changed_paths(candidate)
        candidate_resolver = WorkspacePathResolver(candidate)
        entries: list[CandidatePatchEntry] = []
        for relative_path in changed_paths:
            candidate_target = candidate_resolver.resolve_mutation_target(relative_path)
            if not candidate_target.is_file():
                raise ToolExecutionError(
                    ToolErrorCode.PATH_TYPE_MISMATCH,
                    "Candidate patch entries must be regular files",
                )
            target = self._security.resolve_target(relative_path)
            data = candidate_target.read_bytes()
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolExecutionError(
                    ToolErrorCode.ENCODING_ERROR,
                    "Candidate patch entries must be UTF-8 text",
                ) from exc
            self._security.encode_text(text, max_bytes=self._security.limits.max_file_bytes)
            existed = target.exists()
            before_sha256 = file_sha256(target) if existed else None
            entries.append(
                CandidatePatchEntry(
                    target_path=relative_path,
                    data=data,
                    plan=MutationPlan(
                        tool_name="publish_candidate_patch",
                        target_path=relative_path,
                        target_existed=existed,
                        before_sha256=before_sha256,
                        expected_after_sha256=hashlib.sha256(data).hexdigest(),
                        bytes_written=len(data),
                    ),
                )
            )
        return CandidatePatch(entries=tuple(entries))

    def publish(self, patch: CandidatePatch) -> None:
        for entry in patch.entries:
            target = self._security.resolve_target(entry.target_path)
            self._writer.apply(target, entry.data, entry.plan)

    @staticmethod
    def _changed_paths(candidate: Path) -> list[str]:
        environment = {
            key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
        }
        try:
            completed = subprocess.run(
                (
                    "git",
                    "-c",
                    "core.pager=cat",
                    "status",
                    "--porcelain=v1",
                    "-z",
                    "--untracked-files=all",
                ),
                cwd=candidate,
                env=environment,
                capture_output=True,
                check=False,
                timeout=10,
            )
        except FileNotFoundError as exc:
            raise ToolExecutionError(
                ToolErrorCode.GIT_COMMAND_FAILED,
                "Git executable is unavailable",
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ToolExecutionError(
                ToolErrorCode.TOOL_TIMEOUT,
                "Candidate patch capture timed out",
            ) from exc
        if completed.returncode != 0:
            raise ToolExecutionError(
                ToolErrorCode.NOT_GIT_REPOSITORY,
                "Candidate workspace is not a Git repository",
            )
        records = [record for record in completed.stdout.split(b"\0") if record]
        paths: list[str] = []
        index = 0
        while index < len(records):
            record = records[index]
            if len(record) < 4 or record[2:3] != b" ":
                raise ToolExecutionError(
                    ToolErrorCode.GIT_COMMAND_FAILED,
                    "Git returned an invalid candidate status record",
                )
            status = record[:2].decode("ascii")
            try:
                path = record[3:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ToolExecutionError(
                    ToolErrorCode.ENCODING_ERROR,
                    "Candidate patch path is not UTF-8",
                ) from exc
            if "R" in status or "C" in status:
                raise ToolExecutionError(
                    ToolErrorCode.TOOL_EXECUTION_ERROR,
                    "Candidate patch publication does not support renames or copies",
                )
            if "D" in status:
                raise ToolExecutionError(
                    ToolErrorCode.TOOL_EXECUTION_ERROR,
                    "Candidate patch publication does not support deletions",
                )
            if status == "??" or "M" in status or "A" in status:
                paths.append(path)
            index += 1
        return sorted(paths)
