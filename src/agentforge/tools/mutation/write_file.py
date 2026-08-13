import hashlib

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agentforge.domain.enums import PathKind, ToolErrorCode, ToolRisk, WriteMode
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.models import ToolResult, ToolSpec
from agentforge.domain.mutations import MutationPlan
from agentforge.tools.mutation.atomic import AtomicMutationWriter
from agentforge.tools.mutation.base import mutation_tool_result
from agentforge.tools.mutation.security import MutationSecurityPolicy


class WriteFileArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1)
    content: str
    mode: WriteMode
    expected_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_expected_hash(self) -> "WriteFileArguments":
        if self.mode is WriteMode.CREATE_ONLY and self.expected_sha256 is not None:
            raise ValueError("CREATE_ONLY forbids expected_sha256")
        if self.mode is WriteMode.EXPECTED_HASH_REPLACE and self.expected_sha256 is None:
            raise ValueError("EXPECTED_HASH_REPLACE requires expected_sha256")
        return self


class WriteFileTool:
    def __init__(
        self,
        security: MutationSecurityPolicy,
        writer: AtomicMutationWriter | None = None,
    ) -> None:
        self._security = security
        self._writer = writer or AtomicMutationWriter()

    @property
    def input_model(self) -> type[BaseModel]:
        return WriteFileArguments

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(
            name="write_file",
            description="Create or hash-conditionally replace one bounded UTF-8 workspace file.",
            input_schema=self.input_model.model_json_schema(),
            risk_level=ToolRisk.WRITE,
            requires_approval=True,
            path_argument="path",
            path_kind=PathKind.ANY,
            protect_sensitive_path=True,
        )

    def prepare(self, arguments: BaseModel) -> MutationPlan:
        parsed = WriteFileArguments.model_validate(arguments)
        target = self._security.resolve_target(parsed.path)
        data = self._security.encode_text(
            parsed.content,
            max_bytes=self._security.limits.max_file_bytes,
        )
        relative = self._security.resolver.relative(target)
        expected_after = hashlib.sha256(data).hexdigest()
        if parsed.mode is WriteMode.CREATE_ONLY:
            if target.exists() or target.is_symlink():
                raise ToolExecutionError(
                    ToolErrorCode.MUTATION_TARGET_EXISTS,
                    "CREATE_ONLY target already exists",
                )
            before = None
        else:
            if not target.is_file():
                raise ToolExecutionError(
                    ToolErrorCode.PATH_NOT_FOUND,
                    "Replacement target does not exist",
                )
            _, current = self._security.read_text(target)
            before = hashlib.sha256(current).hexdigest()
            if before != parsed.expected_sha256:
                raise ToolExecutionError(
                    ToolErrorCode.MUTATION_HASH_MISMATCH,
                    "Mutation target does not match expected_sha256",
                )
        return MutationPlan(
            tool_name=self.spec.name,
            target_path=relative,
            target_existed=before is not None,
            before_sha256=before,
            expected_after_sha256=expected_after,
            bytes_written=len(data),
        )

    def execute(self, arguments: BaseModel) -> ToolResult:
        parsed = WriteFileArguments.model_validate(arguments)
        plan = self.prepare(parsed)
        target = self._security.resolve_target(parsed.path)
        data = self._security.encode_text(
            parsed.content,
            max_bytes=self._security.limits.max_file_bytes,
        )
        result = self._writer.apply(target, data, plan)
        return mutation_tool_result(plan, result, operation=parsed.mode.value)
