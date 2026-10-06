# Architecture

VibeSolve is a Python pipeline that generates Java projects. Python builds the
prompts, selects models and validates the results. Pi makes model calls and
reports usage; see [PI_CALLER.md](PI_CALLER.md).

## Pipeline

```text
Original request → Parser → ProblemSpec
                             │
                 Optional user review/corrections
                             │
                 Model Builder → Constraint Builder → IO → Integrator
                             │         Deltas accumulate in ProjectManifest
                          Reviewer
                             │
                  Docker: compile → Main → tests
                             │
                 PASS ───────┴─────── FAIL
                   │                    │
             Write project       IO-model repair, then fixer
             and reports          └─ merge Delta → revalidate
```

| Role | Responsibility / output |
|---|---|
| Parser | Convert the original request into `ProblemSpec`. |
| User Validator — Explain / Update | Explain the spec using `UserValidationExplanation`; apply explicit feedback to a corrected `ProblemSpec`. Optional, interactive. |
| Model Builder | Domain classes, `DataGenerator`, skeleton `pom.xml`. |
| Constraint Builder | `ConstraintProvider` implementation. |
| IO | `JsonIO` and necessary serialization annotations. |
| Integrator | Standalone `Main`, Quarkus REST resource, solver configs, tests and complete `pom.xml`. |
| Reviewer | Check and fix code before Docker validation. |
| Fixer / cheap repair | Targeted changes for compilation, execution or test failures. |

Generation, reviewer and fixer roles return `Delta`, not whole projects.
`utils.patch_utils.apply_delta` is the only merge operation. Parser and optional
user-validation roles return the typed results listed above instead.

## Context and repair

Every non-parser role receives `OriginalRequest` and ordered, exact
`UserClarifications` alongside the derived spec. Explicit corrections override
conflicting original requirements; unchanged requirements override inconsistent
spec details. Truncating repair diagnostics does not truncate this context.

Reviewer runs before Docker by default. After a validation failure, attempt one
uses the IO model (`fixer_cheap`); subsequent attempts use the fixer model. The
default total is two attempts, including no-ops. Only file changes trigger
revalidation. If a model call fails after its retries, repair stops. `FixAttempt`
and run/batch reports record the models used, outcomes and usage.

For compilation errors, the fixer receives compiler-mentioned files, `pom.xml`
and domain declarations. `ProjectFiles` lists every path so omitted contents
are not mistaken for missing files. If no compiler paths can be identified, or
the failure is in execution/tests, it receives the full manifest.

## Validation and output

`DockerValidator` keeps a persistent container to reuse Maven dependencies.
Batch runs allocate a container pool. Validation runs:

1. `mvn [clean] compile`; later iterations skip `clean` when the POM is unchanged.
2. `mvn exec:java` under a 30-second timeout; timeout exit 124 counts as a pass.
3. `mvn test`; tests must pass.

REST and standalone JSON round trips are checked by generated tests, not by a
separate validator. Their coverage depends on what the model generates.

Successful projects are extracted and zipped under `results/run_<timestamp>/`;
logs and raw responses go under `logs/run_<timestamp>/`. Batch runs use
`batch_<timestamp>/<problem>/` and aggregate summaries. Single-run
`RunResult.json` records outcomes and usage. `--serve` also emits portable Docker
artifacts via `packaging.py`.

With validation enabled, `batch` adds a serial benchmark pass: compile/solver
metrics and usage come from pipeline results; Quarkus startup, endpoints and
Docker builds are checked separately by `benchmarking/`. The Docker build
column needs `--serve`. `--no-validation-loop` skips this benchmark table.

## Source map

- `models/`: Pydantic contracts; `config/`: settings and bundled model profiles.
- `agents/` and `pi_worker/`: typed caller, private transport and SDK completions.
- `prompts/`: role behavior and shared intent/API notes.
- `pipeline/`: stage orchestration and optional user review.
- `validation/`: reviewer/repair policy and Docker checks.
- `cli/`, `reporting/`, `benchmarking/`: commands, usage aggregation and probes.

Paths above are relative to `src/vibesolve/`. Generated-stack invariants and
editing rules are in [AGENTS.md](AGENTS.md); contributor checks are in
[CONTRIBUTING.md](CONTRIBUTING.md).
