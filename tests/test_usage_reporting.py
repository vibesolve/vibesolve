"""Reports sum Pi's per-response cost estimates."""

import pytest

from vibesolve.models.results import ProblemResult
from vibesolve.reporting.kpi_tracker import aggregate_results, aggregate_token_usage


def problem(usage):
    return ProblemResult(problem_file="fixture", success=False, total_time_s=0, pipeline_time_s=0,
        validation_time_s=0, fix_iterations=0, error_phases=[], final_error_phase="crash",
        agent_times={}, agent_tokens=usage, error="failed after consuming tokens")


def test_report_sums_per_response_prices_and_cache_writes_including_failed_runs():
    roles = {
        "io": {"model": "model", "input_tokens": 100, "cached_input_tokens": 40,
               "cache_write_tokens": 20, "output_tokens": 10, "estimated_cost_usd": .15},
        "fixer_cheap": {"model": "model", "input_tokens": 200, "cached_input_tokens": 80,
               "cache_write_tokens": 40, "output_tokens": 20, "estimated_cost_usd": .4},
    }
    result = problem(roles)
    totals = aggregate_token_usage([result])
    assert totals["total_input_tokens"] == 300
    assert totals["total_cached_input_tokens"] == 120
    assert totals["total_cache_write_tokens"] == 60
    assert totals["total_tokens"] == 330  # Cache counters are subsets, not additions.
    assert totals["estimated_cost_usd"] == .55
    assert totals["tokens_by_model"]["model"]["cost_usd"] == pytest.approx(.55)
    summary = aggregate_results([result], "fixture")
    assert summary.total_cache_write_tokens == 60
    assert summary.estimated_cost_usd == .55


def test_missing_price_is_not_zero_or_silently_dropped():
    result = problem({
        "io": {"model": "model", "input_tokens": 100, "estimated_cost_usd": .1},
        "fixer_cheap": {"model": "model", "input_tokens": 200},
    })
    totals = aggregate_token_usage([result])
    assert totals["total_input_tokens"] == 300
    assert totals["tokens_by_model"]["model"]["cost_usd"] is None
    assert totals["estimated_cost_usd"] is None
