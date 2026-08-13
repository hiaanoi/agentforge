import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agentforge.domain.enums import ToolErrorCode
from agentforge.domain.errors import ToolExecutionError
from agentforge.domain.mutations import MutationPlan


@dataclass(frozen=True)
class AtomicMutationResult:
    actual_after_sha256: str
    bytes_written: int


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(65_536), b""):
                digest.update(chunk)
    except OSError as exc:
        raise ToolExecutionError(
            ToolErrorCode.TOOL_EXECUTION_ERROR,
            "Unable to read the mutation target",
        ) from exc
    return digest.hexdigest()


class AtomicMutationWriter:
    def apply(
        self,
        target: Path,
        data: bytes,
        plan: MutationPlan,
    ) -> AtomicMutationResult:
        if len(data) != plan.bytes_written or hashlib.sha256(data).hexdigest() != (
            plan.expected_after_sha256
        ):
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_HASH_MISMATCH,
                "Mutation bytes do not match the prepared plan",
            )
        if plan.target_existed:
            return self._replace(target, data, plan)
        return self._create(target, data, plan)

    def _create(
        self,
        target: Path,
        data: bytes,
        plan: MutationPlan,
    ) -> AtomicMutationResult:
        if target.exists() or target.is_symlink():
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_TARGET_EXISTS,
                "CREATE_ONLY target already exists",
            )
        temporary = self._write_temp(target, data, None)
        try:
            if target.exists() or target.is_symlink():
                raise ToolExecutionError(
                    ToolErrorCode.MUTATION_TARGET_EXISTS,
                    "CREATE_ONLY target appeared before publication",
                )
            try:
                os.link(temporary, target)
            except FileExistsError as exc:
                raise ToolExecutionError(
                    ToolErrorCode.MUTATION_TARGET_EXISTS,
                    "CREATE_ONLY target appeared before publication",
                ) from exc
            except OSError as exc:
                raise ToolExecutionError(
                    ToolErrorCode.ATOMIC_WRITE_FAILED,
                    "Filesystem cannot atomically publish the new file",
                ) from exc
            self._fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return self._verify_result(target, plan)

    def _replace(
        self,
        target: Path,
        data: bytes,
        plan: MutationPlan,
    ) -> AtomicMutationResult:
        self._require_before_hash(target, plan)
        try:
            mode = stat.S_IMODE(target.stat().st_mode)
        except OSError as exc:
            raise ToolExecutionError(
                ToolErrorCode.TOOL_EXECUTION_ERROR,
                "Unable to inspect the mutation target",
            ) from exc
        temporary = self._write_temp(target, data, mode)
        try:
            self._require_before_hash(target, plan)
            try:
                os.replace(temporary, target)
            except OSError as exc:
                raise ToolExecutionError(
                    ToolErrorCode.ATOMIC_WRITE_FAILED,
                    "Atomic file replacement failed",
                ) from exc
            self._fsync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
        return self._verify_result(target, plan)

    @staticmethod
    def _require_before_hash(target: Path, plan: MutationPlan) -> None:
        if not target.is_file() or plan.before_sha256 is None:
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_HASH_MISMATCH,
                "Mutation target no longer matches the approved file state",
            )
        if file_sha256(target) != plan.before_sha256:
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_HASH_MISMATCH,
                "Mutation target changed after approval",
            )

    @staticmethod
    def _write_temp(target: Path, data: bytes, mode: int | None) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.agentforge-",
            suffix=".tmp",
        )
        temporary = Path(raw_path)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if mode is not None:
                temporary.chmod(mode)
            return temporary
        except Exception:
            try:
                os.close(descriptor)
            except OSError:
                pass
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _verify_result(target: Path, plan: MutationPlan) -> AtomicMutationResult:
        actual = file_sha256(target)
        if actual != plan.expected_after_sha256:
            raise ToolExecutionError(
                ToolErrorCode.MUTATION_INDETERMINATE,
                "Mutation target hash is uncertain after publication",
            )
        return AtomicMutationResult(
            actual_after_sha256=actual,
            bytes_written=target.stat().st_size,
        )

    @staticmethod
    def _fsync_directory(directory: Path) -> None:
        descriptor: int | None = None
        try:
            descriptor = os.open(directory, os.O_RDONLY)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
