from pathlib import Path

from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import SensitivePathError


class SensitiveFilePolicy:
    _EXACT_NAMES = frozenset({"id_rsa", "id_ed25519"})
    _SENSITIVE_SUFFIXES = (".pem", ".key")
    _NAME_KEYWORDS = ("credential", "secret", "token", "private_key")

    def match(self, relative_path: str) -> str | None:
        path = Path(relative_path)
        parts = tuple(part.casefold() for part in path.parts)
        name = path.name.casefold()
        if name == ".env" or name.startswith(".env."):
            return "environment_file"
        if name in self._EXACT_NAMES:
            return "private_key_name"
        if name.endswith(self._SENSITIVE_SUFFIXES):
            return "private_key_suffix"
        if any(keyword in name for keyword in self._NAME_KEYWORDS):
            return "sensitive_name_keyword"
        if len(parts) >= 2 and parts[-2:] in ((".git", "config"), (".git", "credentials")):
            return "git_credentials"
        return None

    def require_allowed(self, relative_path: str) -> None:
        matched = self.match(relative_path)
        if matched is not None:
            raise SensitivePathError(
                ToolErrorCode.SENSITIVE_PATH,
                f"Requested file is blocked by sensitive-file rule {matched!r}",
            )

