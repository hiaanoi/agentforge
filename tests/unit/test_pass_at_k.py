import pytest

from agentforge.evaluation.pass_at_k import (
    PassAtKNotEligibleError,
    estimate_pass_at_k,
)


@pytest.mark.parametrize(
    ("successful", "expected"),
    [(0, 0.0), (1, 1 / 3), (2, 2 / 3), (3, 1.0)],
)
def test_estimate_pass_at_one_matches_success_rate(
    successful: int,
    expected: float,
) -> None:
    assert estimate_pass_at_k(total=3, successful=successful, k=1) == pytest.approx(expected)


@pytest.mark.parametrize("successful", [0, 1, 2, 3])
def test_estimate_pass_at_three_returns_one_when_any_sample_succeeds(
    successful: int,
) -> None:
    expected = 0.0 if successful == 0 else 1.0

    assert estimate_pass_at_k(total=3, successful=successful, k=3) == expected


def test_estimate_pass_at_k_rejects_ineligible_sample_count() -> None:
    with pytest.raises(PassAtKNotEligibleError, match="total >= k"):
        estimate_pass_at_k(total=2, successful=1, k=3)


def test_estimate_pass_at_k_rejects_successful_count_above_total() -> None:
    with pytest.raises(ValueError, match="Invalid pass@k counts"):
        estimate_pass_at_k(total=2, successful=3, k=1)


@pytest.mark.parametrize(
    ("total", "successful", "k"),
    [(-1, 0, 1), (1, -1, 1), (1, 0, 0)],
)
def test_estimate_pass_at_k_rejects_invalid_counts(
    total: int,
    successful: int,
    k: int,
) -> None:
    with pytest.raises(ValueError, match="Invalid pass@k counts"):
        estimate_pass_at_k(total=total, successful=successful, k=k)
