import re
from pathlib import Path

from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import ToolExecutionError
from agentforge.policy.sensitive import SensitiveFilePolicy
from agentforge.tools.mutation.base import MutationLimits
from agentforge.tools.paths import WorkspacePathResolver

_PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
_PROVIDER_TOKEN = re.compile(
    r"(?:sk-[A-Za-z0-9_-]{20,}|gh[opusr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16})"
)
_NAMED_SECRET = re.compile(
    r"\b(?:DEEPSEEK_API_KEY|OPENAI_API_KEY|ANTHROPIC_API_KEY|API_KEY|"
    r"ACCESS_TOKEN|SECRET_KEY)"
    r"\s*[:=]\s*[\"']?[A-Za-z0-9_./+=-]{8,}",
    re.IGNORECASE,
)


class MutationSecurityPolicy:
    def __init__(
        self,
        resolver: WorkspacePathResolver,
        sensitive_files: SensitiveFilePolicy,
        limits: MutationLimits,
    ) -> None:
        self._resolver = resolver
        self._sensitive_files = sensitive_files
        self.limits = limits

    @property
    def resolver(self) -> WorkspacePathResolver:
        return self._resolver

    def resolve_target(self, requested: str) -> Path:
        target = self._resolver.resolve_mutation_target(requested)
        self._sensitive_files.require_allowed(self._resolver.relative(target))
        return target

    def read_text(self, target: Path) -> tuple[str, bytes]:
        try:
            size = target.stat().st_size
            if size > self.limits.max_file_bytes:
                raise ToolExecutionError(
                    ToolErrorCode.FILE_TOO_LARGE,
                    "Mutation target exceeds the configured byte limit",
                )
            data = target.read_bytes()
        except ToolExecutionError:
            raise
        except OSError as exc:
            raise ToolExecutionError(
                ToolErrorCode.TOOL_EXECUTION_ERROR,
                "Unable to read the mutation target",
            ) from exc
        if b"\x00" in data:
            raise ToolExecutionError(
                ToolErrorCode.BINARY_FILE,
                "Binary mutation targets are not supported",
            )
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ToolExecutionError(
                ToolErrorCode.ENCODING_ERROR,
                "Mutation target is not valid UTF-8 text",
            ) from exc
        self.encode_text(text, max_bytes=self.limits.max_file_bytes)
        return text, data

    @staticmethod
    def encode_text(content: str, *, max_bytes: int) -> bytes:
        if "\x00" in content or any(
            (ord(character) < 32 and character not in "\t\n\r") or ord(character) == 127
            for character in content
        ):
            raise ToolExecutionError(
                ToolErrorCode.BINARY_FILE,
                "Mutation content contains binary control characters",
            )
        if (
            _PRIVATE_KEY.search(content)
            or _PROVIDER_TOKEN.search(content)
            or _NAMED_SECRET.search(content)
        ):
            raise ToolExecutionError(
                ToolErrorCode.SENSITIVE_CONTENT,
                "Mutation content matches a high-confidence secret pattern",
            )
        encoded = content.encode("utf-8")
        if len(encoded) > max_bytes:
            raise ToolExecutionError(
                ToolErrorCode.FILE_TOO_LARGE,
                "Mutation content exceeds the configured byte limit",
            )
        return encoded
