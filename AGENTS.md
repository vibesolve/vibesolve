# Agent instructions

VibeSolve is a Python tool that turns a natural-language optimization problem
into a Java/Quarkus/Timefold Maven project, validated in Docker. Improve the
generator under `src/vibesolve/`, not ephemeral generated files in `results/`.

Read [README.md](README.md) for setup/usage, [ARCHITECTURE.md](ARCHITECTURE.md)
for the pipeline and validation flow, and [PI_CALLER.md](PI_CALLER.md) for
provider setup and caller limits.

## Development checks

```bash
uv sync --extra dev
uv run pytest
npm ci --prefix src/vibesolve/pi_worker --ignore-scripts
npm test --prefix src/vibesolve/pi_worker
```

These tests are offline; they need neither credentials nor Docker. Node
>=22.19 and npm are required for the worker. For end-to-end checks, configure
credentials as described in the README, start Docker and run a bundled problem:

```bash
uv run vibesolve run user_input/timetable.txt
```

The validator image builds on first use; rebuild with
`docker build -t timefold-validator docker/` when its Dockerfile or warmup POM
changes. `--no-validation-loop` is for prompt debugging, not quality validation.
See [CONTRIBUTING.md](CONTRIBUTING.md) for prompt-evaluation expectations.

## Editing map

All source paths below are relative to `src/vibesolve/`.

| Change | Location |
|---|---|
| Stage order / handoffs | `pipeline/runner.py:GENERATION_STAGES` |
| Interactive explain/update | `pipeline/user_validator.py` |
| Role instructions | `prompts/<role>.txt`; registration in `agents/prompts.py:_PROMPT_FILES` |
| Typed output | `models/domain.py`; outcome/usage records in `models/results.py` |
| Model/effort defaults | `config/provider_models.json`; merging in `config/settings.py` |
| Typed caller / routes / construction | `agents/pi_client.py` |
| Worker installation / transport | `agents/pi_install.py`, `pi_process.py`, `pi_protocol.py`, `pi_worker/` |
| Reviewer and repair policy | `validation/feedback_controller.py` |
| Container checks | `validation/docker_validator.py`, `container_pool.py` |
| CLI / reports | `cli/`, `reporting/`, `benchmarking/` |

To add a role, register its prompt, add the model setting to `AgentModels` and
each bundled profile, and wire its invocation into the appropriate orchestrator.

`load_prompt()` adds role-selected `shared-complement.txt` / `shared-jackson.txt`
API notes, then appends `shared-intent.txt` to non-parser roles. Every downstream
role receives `OriginalRequest` and exact, ordered `UserClarifications`.
Explicit corrections take precedence over unchanged original requirements,
which take precedence over inconsistent spec details. Keep this context outside
diagnostic truncation; `IntentContext` is per-run memory, not versioned storage.

`fixer_cheap` is a prompt/telemetry alias using the IO model, not a separate model
setting. Attempt one uses it; later attempts use `fixer`. The default total
budget is two attempts. An attempt that changes no files still uses up budget
but is not revalidated. An attempt that changes files is revalidated. A caller
error that survives retries ends the repair loop. Keep the recorded route,
outcome and usage of each attempt in single-run and batch reports.

## Caller boundary

`PiAgentCaller.call_typed()` is the only caller entry point. Python owns prompts,
context, routing, Pydantic validation and `apply_delta`; Pi only supplies
completions and usage. Keep inputs, routes and budgets stable across retries.
Always close each problem's worker and private transport session. Do not add
agent tools, conversation continuation, protocol compatibility or custom
provider caching. [PI_CALLER.md](PI_CALLER.md) defines limits and lifecycle.

## Generated-project invariants

These are encoded in the prompts and protect observed failure modes. Do not
simplify them without checking why they exist.

- Java 17, Quarkus 3.31.2, Timefold Solver 1.31.0, Maven and fixed `HardSoftScore`.
- `com.example.*` packages: `domain`, `solver`, `io`, `generator`, `rest`.
- REST lives in `rest/SolverResource.java`, with the full `/api/*` endpoint set
  from the integrator template; never substitute `MyResource.java`.
- Jakarta EE imports (`jakarta.ws.rs.*`), never `javax.ws.rs.*`.
- Timefold dependencies are `timefold-solver-quarkus`,
  `timefold-solver-quarkus-jackson`, `timefold-solver-test`, and the solver BOM;
  `timefold-solver-constraints` does not exist.
- `exec-maven-plugin` 3.6.3 must configure the fully qualified standalone Main
  class. Quarkus owns HTTP startup, not this Main.
- Production `solverConfig.xml` uses `REPRODUCIBLE` and 15-second termination.
  Test-only `solverConfigTest.xml` uses `FULL_ASSERT` solely under `%test`;
  never serve requests in `FULL_ASSERT`.
- Tests generate datasets with `DataGenerator`, solve in `FULL_ASSERT`, and
  assert at least one planning variable is assigned. Round-trip an existing
  solved result through actual standalone JSON IO and assert an unchanged,
  non-null `HardSoftScore`; REST tests do not cover that mapper.
- `@PlanningSolution` exposes a `solverStatus` field of top-level `SolverStatus`,
  not `SolverManager.SolverStatus`; do not add `@JsonIgnore`.
- XML comments end in `-->`, never `--->`.

For compiler failures, repair context includes referenced files, `pom.xml`,
domain declarations and a complete `ProjectFiles` inventory. Runtime/test
failures retain the full manifest. Do not interpret omitted file contents as
permission to delete or recreate files.

## Repository rules

- Never commit `.env.local`, `logs/`, `results/` or `.validation_temp/`.
  Do not copy Pi login credentials into the repository or logs.
- Never use `git add -A`: local-only experiment directories such as `assets/`,
  `vanilla_api_tests/` and `advent_problems/` may be untracked but not ignored.
  Stage explicit paths and preserve unrelated user changes.
- Match the surrounding style: Pydantic v2, structlog, typed `pathlib.Path`,
  no `dict[str, Any]` at boundaries. Avoid import cycles.
- `apply_delta` is the only legitimate merge operation; no ad-hoc file merging.
- Fix the prompt or repair policy, never generated `.java` / `pom.xml` files.
- Add/update tests for CLI, settings, caller, models and merge behavior.
- For debugging, inspect `logs/run_<timestamp>/<agent>-response_*.txt` and
  `pipeline.log`; batch logs are under `logs/batch_<timestamp>/<problem>/`.
  Treat raw responses and input-bearing artifacts as sensitive when sharing.
