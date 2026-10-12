"""A provider failure is not evidence that generated code passed validation."""

import pytest

from vibesolve.benchmarking.table import derive_light_columns
from vibesolve.models.results import ProblemResult


@pytest.mark.parametrize("phase, expected", [
    ("crash", (False, False)), ("", (False, False)),
    ("compilation", (False, False)), ("runtime", (True, False)),
    ("test", (True, True)),
])
def test_benchmark_never_counts_a_provider_crash_as_validated(phase, expected):
    result = ProblemResult(problem_file="fixture", success=False, total_time_s=0,
        pipeline_time_s=0, validation_time_s=0, fix_iterations=0, error_phases=[],
        final_error_phase=phase, agent_times={}, agent_tokens={}, error="failed")
    assert derive_light_columns(result) == expected
