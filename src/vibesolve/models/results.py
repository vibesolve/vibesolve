from typing import Literal

from pydantic import BaseModel, Field


class ValidationResult(BaseModel):
    success: bool
    compilation_output: str
    runtime_output: str
    exit_code: int
    error_phase: Literal["compilation", "runtime", "test", "none"]
    test_output: str = ""

    def to_dict(self) -> dict:
        return self.model_dump()

    def to_json(self) -> str:
        return self.model_dump_json(indent=2)


class FixAttempt(BaseModel):
    iteration: int
    error_phase: str
    error_summary: str
    fixed: bool


class ProblemResult(BaseModel):
    problem_file: str
    success: bool
    total_time_s: float
    pipeline_time_s: float
    validation_time_s: float
    fix_iterations: int
    error_phases: list[str]
    final_error_phase: str
    agent_times: dict[str, float]
    # Per-agent token usage: agent -> {model, input_tokens, cached_input_tokens, output_tokens}
    agent_tokens: dict[str, dict] = Field(default_factory=dict)
    error: str | None = None


class BatchSummary(BaseModel):
    batch_id: str
    total_problems: int
    success_count: int
    failure_count: int
    success_rate_pct: float
    avg_fix_iterations: float
    avg_iterations_on_success: float
    avg_total_time_s: float
    avg_pipeline_time_s: float
    phase_failure_counts: dict[str, int]
    zero_iteration_successes: int
    failed_problems: list[str]
    # Token totals across all problems in the batch.
    total_input_tokens: int = 0          # total prompt tokens (includes cached)
    total_cached_input_tokens: int = 0   # subset of input that was a cache hit
    total_output_tokens: int = 0
    total_tokens: int = 0                # input + output
    estimated_cost_usd: float | None = None  # None if any used model has no price entry
    # Per-model breakdown: model -> {input_tokens, cached_input_tokens, output_tokens, cost_usd|None}
    tokens_by_model: dict[str, dict] = Field(default_factory=dict)
    problem_results: list[ProblemResult] = Field(default_factory=list)
