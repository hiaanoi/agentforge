from math import comb


class PassAtKNotEligibleError(ValueError):
    pass


def estimate_pass_at_k(*, total: int, successful: int, k: int) -> float:
    if total < 0 or successful < 0 or successful > total or k <= 0:
        raise ValueError("Invalid pass@k counts")
    if total < k:
        raise PassAtKNotEligibleError("pass@k requires total >= k")
    if total - successful < k:
        return 1.0
    return 1.0 - comb(total - successful, k) / comb(total, k)
