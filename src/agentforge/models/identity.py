from datetime import date


def is_exact_or_dated_openai_snapshot(
    requested_model_id: str,
    response_model_id: str,
) -> bool:
    if response_model_id == requested_model_id:
        return True
    prefix = f"{requested_model_id}-"
    if not response_model_id.startswith(prefix):
        return False
    suffix = response_model_id[len(prefix) :]
    try:
        parsed = date.fromisoformat(suffix)
    except ValueError:
        return False
    return parsed.isoformat() == suffix
