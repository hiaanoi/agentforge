import hashlib

from pydantic import BaseModel, ConfigDict, Field

from agentforge.domain.enums import PathKind, ToolErrorCode, ToolRisk
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.domain.mutations import MutationPlan
from agentforge.tools.mutation.atomic import AtomicMutationWriter
from agentforge.tools.mutation.base import mutation_tool_result
from agentforge.tools.mutation.security import MutationSecurityPolicy


class EditFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    old_text: str = Field(min_length=1)
    new_text: str
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EditFileTool:
    def __init__(
        self,
        security: MutationSecurityPolicy,
        writer: AtomicMutationWriter | None = None,
    ) -> None:
        self._security = security
        self._writer = writer or AtomicMutationWriter()

    @property
    def input_model(self) -> type[BaseModel]:
        return EditFileArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="edit_file",
            description="Replace one exact text occurrence in a hash-bound workspace file.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.WRITE,
            requires_approval=True,
            path_argument="path",
            path_kind=PathKind.FILE,
            protect_sensitive_path=True,
        )

    def prepare(self, arguments: BaseModel) -> MutationPlan:
        parsed = EditFileArguments.model_validate(arguments)
        target = self._security.resolve_target(parsed.path)
        if not target.is_file():
            raise ToolExecutionError(
                ToolErrorCode.PATH_NOT_FOUND,
                "Edit target does not exist",
            )
        self._security.encode_text(
            parsed.old_text,
            max_bytes=self._security.limits.max_old_text_bytes,
        )
        self._security.encode_text(
            parsed.new_text,
            max_bytes=self._security.limits.max_new_text_bytes,
        )
        current_text, current = self._security.read_text(target)
        before = hashlib.sha256(current).hexdigest()
        if before != parsed.expected_sha256:
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_HASH_MISMATCH,
                "Mutation target does not match expected_sha256",
            )
        if current_text.count(parsed.old_text) != 1:
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_TEXT_MISMATCH,
                "old_text must occur exactly once",
            )
        updated = current_text.replace(parsed.old_text, parsed.new_text, 1)
        if not updated:
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_TEXT_MISMATCH,
                "edit_file cannot delete the complete file",
            )
        data = self._security.encode_text(
            updated,
            max_bytes=self._security.limits.max_file_bytes,
        )
        return MutationPlan(
            tool_name=self.spec.name,
            target_path=self._security.resolver.relative(target),
            target_existed=True,
            before_sha256=before,
            expected_after_sha256=hashlib.sha256(data).hexdigest(),
            bytes_written=len(data),
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = EditFileArguments.model_validate(arguments)
        plan = self.prepare(parsed)
        target = self._security.resolve_target(parsed.path)
        current_text, _ = self._security.read_text(target)
        updated = current_text.replace(parsed.old_text, parsed.new_text, 1)
        data = self._security.encode_text(
            updated,
            max_bytes=self._security.limits.max_file_bytes,
        )
        result = self._writer.apply(target, data, plan)
        return mutation_tool_result(plan, result, operation="EXACT_REPLACE")
