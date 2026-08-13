import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, JsonValue

from agentforge.domain.repair import RepairTaskPolicy
from agentforge.evaluation.formal_fixtures import FormalFixtureManifest
from agentforge.evaluation.task_definition import EvaluationTaskDefinition

PromptVariant = Literal[
    "baseline",
    "invariant_guidance",
    "task_diagnostic",
    "task_contract_guidance",
]
BASELINE_PROMPT_VARIANT: PromptVariant = "baseline"
INVARIANT_GUIDANCE_PROMPT_VARIANT: PromptVariant = "invariant_guidance"
TASK_DIAGNOSTIC_PROMPT_VARIANT: PromptVariant = "task_diagnostic"
TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT: PromptVariant = "task_contract_guidance"
SYSTEM_PROMPT_VERSION = 1
INVARIANT_GUIDANCE_SYSTEM_PROMPT_VERSION = 2
TASK_DIAGNOSTIC_SYSTEM_PROMPT_VERSION = 3
TASK_CONTRACT_GUIDANCE_SYSTEM_PROMPT_VERSION = 4
SYSTEM_PROMPT = """You are a constrained code repair agent.
Treat tool results as the only source of execution truth.
Stay within the declared workspace permissions.
Never claim completion unless the latest source state has been tested.
Do not fabricate file contents, test results, or side effects."""
INVARIANT_GUIDANCE = """After a visible test passes, do not stop automatically. Re-check the
success conditions and the implementation's state invariants. For stateful code, inspect object
identity and binding relationships; for recovery code, reason about interruption, retries,
concurrency, and idempotency. Make the smallest approved change that satisfies the complete
behavior, then rerun the visible test before finishing. Do not infer hidden test contents."""

TASK_DIAGNOSTIC_GUIDANCE: dict[str, str] = {
    "swebench-pytest-10051": """For this task, reason about the observable contract of the capture
API, not just the immediate list contents. If callers can retain a collection returned or exposed
by an object, clearing it should update that existing collection in place so existing references
remain live. Check that the active phase continues to expose current records after clearing, and
that changing phases does not accidentally reuse another phase's records.""",
    "self-durable-double-consumption": """For this task, reason about durable state transitions
across retry and recovery boundaries. A completed command must converge to its stored result on
repeated processing. A dispatch side effect must have a durable unique identity before completion,
and recovery after an interruption must reuse that fact rather than issue the side effect again.
Keep ownership, dispatch recording, and completion transitions consistent under competing or stale
workers, and validate that an identifier cannot be reused for different input.""",
}
TASK_DIAGNOSTIC_DEFAULT_GUIDANCE = """For this task, derive a short checklist from the stated
success conditions before editing. Check boundary cases, state that must remain observable to
callers, and behavior after a retry or a fresh process. Make the smallest change that satisfies
the complete contract, then rerun the visible test. Do not infer hidden test contents."""
TASK_CONTRACT_GUIDANCE: dict[str, str] = {
    "swebench-pytest-10051": """Each phase must own a distinct records list. Clearing must mutate
only the current phase's list in place, so references to that phase stay live. References from
previous phases must remain unchanged and isolated. After clearing, new messages must be visible
through the current phase's existing reference.""",
    "self-durable-double-consumption": """If a receipt is claimed but has no durable dispatch
record, a competing worker must not replace the owner. Only an initial unowned receipt may be
claimed, or a claimed receipt with a durable dispatch may be recovered. Once a dispatch exists,
recovery must reuse it; completion must be conditional on the current owner and must not create
another dispatch.""",
}


class RepairPromptBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    system_prompt_version: int = SYSTEM_PROMPT_VERSION
    system_prompt: str
    task_prompt: str
    system_prompt_digest: str
    task_prompt_digest: str
    tool_schema_digest: str


def build_prompt_bundle(
    task: EvaluationTaskDefinition,
    policy: RepairTaskPolicy,
    *,
    tool_schema: dict[str, JsonValue],
    prompt_variant: PromptVariant = BASELINE_PROMPT_VARIANT,
) -> RepairPromptBundle:
    system_prompt, system_prompt_version = _system_prompt(prompt_variant)
    diagnostic_guidance = _task_guidance(prompt_variant, task.task_id)
    task_prompt_lines = [
            f"Task: {task.title}",
            task.description,
            f"Allowed write paths: {', '.join(policy.allowed_write_paths)}",
            f"Forbidden write paths: {', '.join(policy.forbidden_write_paths) or 'none'}",
            f"Protected paths: {', '.join(policy.protected_paths)}",
            f"Development test profile: {task.development_test_profile_id}",
            (
                "File creation is allowed only in: "
                + ", ".join(policy.allowed_create_paths)
                if policy.allow_file_creation
                else "File creation is not allowed."
            ),
            "Do not modify tests, dependency declarations, lock files, or test configuration.",
            "Success conditions: " + " | ".join(task.success_conditions),
        ]
    if diagnostic_guidance:
        task_prompt_lines.append(diagnostic_guidance)
    task_prompt = "\n".join(task_prompt_lines)
    return RepairPromptBundle(
        system_prompt_version=system_prompt_version,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        system_prompt_digest=_text_digest(system_prompt),
        task_prompt_digest=_text_digest(task_prompt),
        tool_schema_digest=_json_digest(tool_schema),
    )


def build_formal_prompt_bundle(
    manifest: FormalFixtureManifest,
    policy: RepairTaskPolicy,
    *,
    tool_schema: dict[str, JsonValue],
    prompt_variant: PromptVariant = BASELINE_PROMPT_VARIANT,
) -> RepairPromptBundle:
    if manifest.task_id != policy.task_id:
        raise ValueError("Formal prompt manifest and policy task IDs do not match")
    system_prompt, system_prompt_version = _system_prompt(prompt_variant)
    diagnostic_guidance = _task_guidance(prompt_variant, manifest.task_id)
    task_prompt_lines = [
            f"Task: {manifest.repair_prompt.title}",
            manifest.repair_prompt.description,
            f"Allowed write paths: {', '.join(policy.allowed_write_paths)}",
            f"Development test profile: visible-{manifest.task_id}",
            "File creation is not allowed.",
            "Do not modify tests, dependency declarations, lock files, or test configuration.",
            "Success conditions: "
            + " | ".join(manifest.repair_prompt.success_conditions),
        ]
    if diagnostic_guidance:
        task_prompt_lines.append(diagnostic_guidance)
    task_prompt = "\n".join(task_prompt_lines)
    return RepairPromptBundle(
        system_prompt_version=system_prompt_version,
        system_prompt=system_prompt,
        task_prompt=task_prompt,
        system_prompt_digest=_text_digest(system_prompt),
        task_prompt_digest=_text_digest(task_prompt),
        tool_schema_digest=_json_digest(tool_schema),
    )


def _system_prompt(
    prompt_variant: PromptVariant,
) -> tuple[str, int]:
    if prompt_variant == BASELINE_PROMPT_VARIANT:
        return SYSTEM_PROMPT, SYSTEM_PROMPT_VERSION
    if prompt_variant == INVARIANT_GUIDANCE_PROMPT_VARIANT:
        return (
            f"{SYSTEM_PROMPT}\n{INVARIANT_GUIDANCE}",
            INVARIANT_GUIDANCE_SYSTEM_PROMPT_VERSION,
        )
    if prompt_variant == TASK_DIAGNOSTIC_PROMPT_VARIANT:
        return (
            SYSTEM_PROMPT,
            TASK_DIAGNOSTIC_SYSTEM_PROMPT_VERSION,
        )
    if prompt_variant == TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT:
        return SYSTEM_PROMPT, TASK_CONTRACT_GUIDANCE_SYSTEM_PROMPT_VERSION
    raise ValueError("Unsupported prompt variant")


def _task_guidance(prompt_variant: PromptVariant, task_id: str) -> str:
    if prompt_variant == TASK_CONTRACT_GUIDANCE_PROMPT_VARIANT:
        guidance = TASK_CONTRACT_GUIDANCE.get(task_id)
        return "Contract checklist: " + guidance if guidance is not None else ""
    if prompt_variant != TASK_DIAGNOSTIC_PROMPT_VARIANT:
        return ""
    return (
        "Diagnostic checklist: "
        + TASK_DIAGNOSTIC_GUIDANCE.get(task_id, TASK_DIAGNOSTIC_DEFAULT_GUIDANCE)
    )


def _text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_digest(value: dict[str, JsonValue]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
