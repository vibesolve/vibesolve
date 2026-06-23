"""
KPI Tracker for Batch Pipeline Runs.

Re-exports ProblemResult and BatchSummary from models.results for backward
compatibility, and provides aggregation and reporting functions.
"""

# Re-export so existing callers (run_batch.py) can still import from here
from vibesolve.models.results import BatchSummary, ProblemResult

__all__ = [
    "ProblemResult",
    "BatchSummary",
    "MODEL_PRICING",
    "aggregate_token_usage",
    "aggregate_results",
    "generate_report",
]


# USD per 1,000,000 tokens. VERIFY against current OpenAI/Anthropic pricing —
# rates change and these are best-effort. Token COUNTS reported below are exact;
# only the dollar figure depends on this table. Add/edit models as needed. A model
# not listed here yields cost=None (the batch's whole estimate becomes "n/a").
#   input        = fresh (non-cached) prompt tokens
#   cached_input = prompt tokens served from cache (billed cheaper)
#   output       = completion tokens
# OpenAI cached_input = the published cached-input rate (≈0.1× input).
# Anthropic cached_input = the cache-READ rate (0.1× input); cache writes (1.25–2×)
#   are not modeled separately.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI — GPT-5 series
    "gpt-5":           {"input": 1.25, "cached_input": 0.125, "output": 10.00},
    "gpt-5-mini":      {"input": 0.25, "cached_input": 0.025, "output": 2.00},
    "gpt-5-nano":      {"input": 0.05, "cached_input": 0.005, "output": 0.40},
    # OpenAI — GPT-5.4 series
    "gpt-5.4":         {"input": 2.50, "cached_input": 0.25, "output": 15.00},
    "gpt-5.4-mini":    {"input": 0.75, "cached_input": 0.075, "output": 4.50},
    "gpt-5.4-nano":    {"input": 0.20, "cached_input": 0.02, "output": 1.25},
    "gpt-5.4-pro":     {"input": 30.00, "cached_input": 30.00, "output": 180.00},
    # OpenAI — GPT-5.5 series
    "gpt-5.5":         {"input": 5.00, "cached_input": 0.50, "output": 30.00},
    "gpt-5.5-pro":     {"input": 30.00, "cached_input": 30.00, "output": 180.00},
    # Anthropic — Claude
    "claude-fable-5":    {"input": 10.00, "cached_input": 1.00, "output": 50.00},
    "claude-opus-4-8":   {"input": 5.00, "cached_input": 0.50, "output": 25.00},
    "claude-opus-4-7":   {"input": 5.00, "cached_input": 0.50, "output": 25.00},
    "claude-opus-4-6":   {"input": 5.00, "cached_input": 0.50, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "cached_input": 0.30, "output": 15.00},
    "claude-haiku-4-5":  {"input": 1.00, "cached_input": 0.10, "output": 5.00},
}


def _cost_for_model(model: str, input_t: int, cached_t: int, output_t: int) -> float | None:
    """Cost in USD for one model's usage, or None if the model has no price entry."""
    rates = MODEL_PRICING.get(model)
    if rates is None:
        return None
    fresh = max(input_t - cached_t, 0)  # input_tokens includes the cached subset
    return (
        fresh * rates["input"]
        + cached_t * rates["cached_input"]
        + output_t * rates["output"]
    ) / 1_000_000


def aggregate_token_usage(results: list[ProblemResult]) -> dict:
    """
    Sum token usage across all problem results, grouped by model.

    Returns a dict with grand totals and a per-model breakdown (including a
    per-model cost_usd, or None when the model isn't in MODEL_PRICING). The
    grand-total estimated_cost_usd is None if ANY used model lacks a price.
    """
    by_model: dict[str, dict] = {}
    for r in results:
        for usage in r.agent_tokens.values():
            model = usage.get("model", "unknown")
            agg = by_model.setdefault(
                model,
                {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0},
            )
            agg["input_tokens"] += usage.get("input_tokens", 0)
            agg["cached_input_tokens"] += usage.get("cached_input_tokens", 0)
            agg["output_tokens"] += usage.get("output_tokens", 0)

    total_input = total_cached = total_output = 0
    total_cost = 0.0
    cost_known = True
    for model, agg in by_model.items():
        total_input += agg["input_tokens"]
        total_cached += agg["cached_input_tokens"]
        total_output += agg["output_tokens"]
        cost = _cost_for_model(
            model, agg["input_tokens"], agg["cached_input_tokens"], agg["output_tokens"]
        )
        agg["cost_usd"] = cost
        if cost is None:
            cost_known = False
        else:
            total_cost += cost

    return {
        "total_input_tokens": total_input,
        "total_cached_input_tokens": total_cached,
        "total_output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "estimated_cost_usd": round(total_cost, 4) if cost_known else None,
        "tokens_by_model": by_model,
    }


def aggregate_results(results: list[ProblemResult], batch_id: str) -> BatchSummary:
    """Aggregate a list of ProblemResult into a BatchSummary."""
    total = len(results)
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    success_count = len(successes)
    failure_count = len(failures)
    success_rate_pct = (success_count / total * 100) if total > 0 else 0.0

    avg_fix_iterations = (
        sum(r.fix_iterations for r in results) / total if total > 0 else 0.0
    )
    avg_iterations_on_success = (
        sum(r.fix_iterations for r in successes) / success_count
        if success_count > 0 else 0.0
    )
    avg_total_time_s = sum(r.total_time_s for r in results) / total if total > 0 else 0.0
    avg_pipeline_time_s = sum(r.pipeline_time_s for r in results) / total if total > 0 else 0.0

    phase_failure_counts: dict[str, int] = {}
    for r in results:
        for phase in r.error_phases:
            phase_failure_counts[phase] = phase_failure_counts.get(phase, 0) + 1

    zero_iteration_successes = sum(1 for r in successes if r.fix_iterations == 0)

    tokens = aggregate_token_usage(results)

    return BatchSummary(
        batch_id=batch_id,
        total_problems=total,
        success_count=success_count,
        failure_count=failure_count,
        success_rate_pct=success_rate_pct,
        avg_fix_iterations=avg_fix_iterations,
        avg_iterations_on_success=avg_iterations_on_success,
        avg_total_time_s=avg_total_time_s,
        avg_pipeline_time_s=avg_pipeline_time_s,
        phase_failure_counts=phase_failure_counts,
        zero_iteration_successes=zero_iteration_successes,
        failed_problems=[r.problem_file for r in failures],
        total_input_tokens=tokens["total_input_tokens"],
        total_cached_input_tokens=tokens["total_cached_input_tokens"],
        total_output_tokens=tokens["total_output_tokens"],
        total_tokens=tokens["total_tokens"],
        estimated_cost_usd=tokens["estimated_cost_usd"],
        tokens_by_model=tokens["tokens_by_model"],
        problem_results=results,
    )


def generate_report(summary: BatchSummary) -> str:
    """Generate a human-readable text report from a BatchSummary."""
    lines = []
    sep = "─" * 58

    lines.append(f"BATCH REPORT  [{summary.batch_id}]")
    lines.append(sep)
    lines.append(f"Problems      : {summary.total_problems}")
    lines.append(
        f"Success rate  : {summary.success_count}/{summary.total_problems}"
        f"  ({summary.success_rate_pct:.0f}%)"
    )
    lines.append(
        f"Avg iterations: {summary.avg_fix_iterations:.1f}"
        f"   ({summary.avg_iterations_on_success:.1f} on success)"
    )
    lines.append(f"Avg total time: {summary.avg_total_time_s:.1f}s")
    lines.append(f"Avg agent time: {summary.avg_pipeline_time_s:.1f}s")
    lines.append(
        f"Zero-fix wins : {summary.zero_iteration_successes}/{summary.total_problems}"
        f"  (passed first try)"
    )

    if summary.phase_failure_counts:
        phase_str = "  ".join(
            f"{phase}={count}"
            for phase, count in sorted(summary.phase_failure_counts.items())
        )
        lines.append(f"Phase failures: {phase_str}")
    else:
        lines.append("Phase failures: none")

    if summary.failed_problems:
        lines.append(f"Failed        : {', '.join(summary.failed_problems)}")
    else:
        lines.append("Failed        : none")

    # NOTE: the TOKENS & COST section is intentionally omitted from the printed
    # report for now. The figures are still computed and live on BatchSummary
    # (total_*_tokens, estimated_cost_usd, tokens_by_model) — used by the
    # benchmark table and available to re-add here later.

    lines.append(sep)
    lines.append("PER-PROBLEM BREAKDOWN")
    lines.append(sep)

    for r in summary.problem_results:
        status = "pass" if r.success else "FAIL"
        phase = "" if r.success else f"  ({r.final_error_phase})"
        lines.append(
            f"  {'✓' if r.success else '✗'}  {r.problem_file:<30}"
            f"  {status}  {r.fix_iterations} iter"
            f"  {r.total_time_s:.0f}s{phase}"
        )
        if r.error:
            lines.append(f"      ERROR: {r.error[:120]}")

    lines.append(sep)
    return "\n".join(lines)
