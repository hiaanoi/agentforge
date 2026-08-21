"""Immutable task selection for the bounded SWE repair-budget ablation."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

from agentforge.evaluation.swebench_prediction import (
    SWEbenchPrediction,
    save_swebench_predictions,
)
from agentforge.evaluation.verified10_campaign import (
    AttemptStatus,
    BenchmarkArm,
    CampaignExecutionError,
)
from agentforge.evaluation.verified10_runner import Verified10Campaign

SWE_ABLATION_INSTANCE_IDS = (
    "django__django-12419",
    "django__django-13343",
    "matplotlib__matplotlib-24026",
)


def validate_ablation_task_ids(instance_ids: Iterable[str]) -> tuple[str, ...]:
    selected = tuple(instance_ids)
    if selected != SWE_ABLATION_INSTANCE_IDS:
        raise CampaignExecutionError("Task IDs do not match the declared ablation set")
    return selected


class SWEAblationCampaign(Verified10Campaign):
    """Three-task AgentForge-only extension of the frozen Verified-10 protocol."""

    _agentforge_budget_profile = "SWE_BENCH_ABLATION_100"
    _agentforge_max_steps = 100
    _agentforge_max_model_requests = 102
    _agentforge_max_total_tokens = 1_200_000

    def __init__(self, protocol_path: str | Path, output_dir: str | Path, **kwargs: Any) -> None:
        super().__init__(protocol_path, output_dir, **kwargs)
        self._agentforge_budget_profile = "SWE_BENCH_ABLATION_100"
        self._agentforge_max_steps = 100
        self._agentforge_max_model_requests = 102
        self._agentforge_max_total_tokens = 1_200_000

    def run_agentforge(
        self,
        *,
        recover_running: bool = False,
        retry_failed: bool = False,
        task_ids: tuple[str, ...] | None = None,
        repair_engine: Literal["native", "mini_linear"] = "native",
    ) -> None:
        if task_ids is not None:
            validate_ablation_task_ids(task_ids)
        super().run_agentforge(
            recover_running=recover_running,
            retry_failed=retry_failed,
            task_ids=SWE_ABLATION_INSTANCE_IDS,
            repair_engine=repair_engine,
        )

    def finalize_ablation_predictions(self) -> Path:
        state = self._load()
        self._require_prepared(state)
        records = self._records(state, BenchmarkArm.AGENTFORGE)
        if set(records) != set(SWE_ABLATION_INSTANCE_IDS) or any(
            record.status not in {AttemptStatus.COMPLETED, AttemptStatus.FAILED}
            for record in records.values()
        ):
            raise CampaignExecutionError("Ablation finalization requires three terminal attempts")
        tasks = {task.instance_id: task for task in self.protocol.tasks}
        predictions = tuple(
            SWEbenchPrediction(
                instance_id=task_id,
                model_name_or_path="agentforge:" + self.protocol.model,
                model_patch=records[task_id].model_patch,
                base_commit=tasks[task_id].base_commit,
                patch_sha256=hashlib.sha256(records[task_id].model_patch.encode()).hexdigest(),
            )
            for task_id in SWE_ABLATION_INSTANCE_IDS
        )
        destination = self.root / "agentforge-predictions.json"
        save_swebench_predictions(
            destination,
            predictions,
            expected_instance_ids=SWE_ABLATION_INSTANCE_IDS,
        )
        return destination
