import pytest


def test_ablation_declares_exact_three_public_tasks() -> None:
    from agentforge.evaluation.swe_ablation import SWE_ABLATION_INSTANCE_IDS

    assert SWE_ABLATION_INSTANCE_IDS == (
        "django__django-12419",
        "django__django-13343",
        "matplotlib__matplotlib-24026",
    )


def test_ablation_rejects_task_ids_outside_declared_set() -> None:
    from agentforge.evaluation.swe_ablation import validate_ablation_task_ids
    from agentforge.evaluation.verified10_campaign import CampaignExecutionError

    with pytest.raises(CampaignExecutionError, match="declared ablation"):
        validate_ablation_task_ids(("sympy__sympy-16886",))
