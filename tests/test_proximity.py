import math

import pytest

from app.core.config import Settings
from app.services.proximity import ProximityService


@pytest.mark.parametrize(
    "distance,confidence,expected",
    [
        (0.45, 1.0, "near"),
        (1.5, 1.0, "near"),
        (1.500001, 1.0, "medium"),
        (2.999999, 1.0, "medium"),
        (3.0, 1.0, "far"),
        (12.0, 1.0, "far"),
        (None, 1.0, "unknown"),
        (math.nan, 1.0, "unknown"),
        (math.inf, 1.0, "unknown"),
        (-0.2, 1.0, "unknown"),
        (0.45, 0.49, "unknown"),
        (0.45, 0.5, "near"),
        (0.45, math.nan, "unknown"),
        (0.0, 1.0, "unknown"),
        (0.45, 1.1, "unknown"),
        (0.45, -0.1, "unknown"),
        (math.nextafter(1.5, math.inf), 1.0, "medium"),
        (math.nextafter(3.0, -math.inf), 1.0, "medium"),
    ],
)
def test_threshold_boundaries_and_unusable_distances(distance, confidence, expected):
    policy = ProximityService(
        Settings(_env_file=None, near_threshold_m=1.5, far_threshold_m=3.0, min_confidence=0.5)
    )
    assert policy.classify(distance, confidence) == expected


def test_thresholds_can_be_configured_without_changing_the_model():
    policy = ProximityService(
        Settings(_env_file=None, near_threshold_m=0.3, far_threshold_m=1.0, min_confidence=0.8)
    )
    assert policy.classify(0.45, 1.0) == "medium"
    assert policy.classify(1.5, 1.0) == "far"
    assert policy.classify(0.2, 0.7) == "unknown"
