"""Make captured external JSON safe for strict JSON encoders."""
import math


def json_safe(value):
    """Replace non-finite native numbers with null, retaining strings verbatim."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    return value
