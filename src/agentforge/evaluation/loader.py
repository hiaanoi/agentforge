import json
from collections.abc import Mapping
from pathlib import Path

from pydantic import JsonValue, ValidationError

from agentforge.evaluation.task_definition import EvaluationTaskDefinition


class EvaluationTaskLoader:
    def load(self, path: Path) -> EvaluationTaskDefinition:
        if path.suffix.casefold() != ".json":
            raise ValueError("Evaluation task definitions must use JSON")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Evaluation task definition is invalid") from exc
        if not isinstance(raw, dict):
            raise ValueError("Evaluation task definition is invalid")
        return self.from_mapping(raw)

    def from_mapping(
        self,
        value: Mapping[str, JsonValue],
    ) -> EvaluationTaskDefinition:
        try:
            return EvaluationTaskDefinition.model_validate(dict(value))
        except ValidationError as exc:
            raise ValueError("Evaluation task definition is invalid") from exc

