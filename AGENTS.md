# AGENTS.md

Guide for AI coding agents working in this repository.

## What this project is

`vibesolve` is a multi-agent pipeline that converts a free-text optimization
problem description (e.g. "schedule deliveries", "assign nurses to shifts") into a complete, runnable [Timefold Solver](https://timefold.ai/) Quarkus project, validated by Docker before it lands on disk.

A user writes a sentence; a sequence of LLM agents (parser → model builder →
constraint builder → IO → integrator, plus optional reviewer/fixer/user
validator) emits Java domain classes, constraint streams, REST endpoints,
`pom.xml`, tests, and `solverConfig.xml`. The output is a Maven project that
compiles and runs in a containerized JDK 17.

This is a **Python tool that generates Java projects** — do not confuse the
Python source under `src/vibesolve/` with the generated artifacts under
`results/`.

## Setup (one-time)

```bash
conda activate vibesolve            # ALWAYS activate first — required for every command
pip install -e ".[dev]"             # installs the package, CLI, and test deps (pytest)
cp .env.example .env.local          # then fill in provider credentials
docker build -t timefold-validator docker/   # pre-bakes Maven deps into the validator image
```

Python 3.11+ is required (modern type annotations). If `conda activate vibesolve` fails, create the env first: `conda create -n vibesolve python=3.11 -y`.

Provider calls go through any-llm. Pass any installed any-llm provider name with
`--provider` (for example `openai`, `anthropic`, `bedrock`); the legacy
`claude` value is kept as an alias for `anthropic`. Credentials come from
provider-specific environment variables or credential chains, or from the
generic `API_KEY` setting where the provider accepts a single API key.

Make sure the Docker daemon is running before the `docker build` (Linux:
`sudo systemctl start docker`; macOS/Windows: launch Docker Desktop).

## CLI

There is a single `vibesolve` command (defined in `pyproject.toml [project.scripts]` as `vibesolve.cli.main:app`) with two subcommands. Run them from the repo root.

| Command | Purpose | Source |
|---|---|---|
| `vibesolve run [input.txt]` | Run the pipeline on one problem | `src/vibesolve/cli/run_single.py` |
| `vibesolve batch [files...]` | Parallel batch over `user_input/*.txt` | `src/vibesolve/cli/run_batch.py` |

`cli/main.py` is the entry point; it registers the two functions as subcommands.
Run `vibesolve --help` (or `vibesolve run --help` / `vibesolve batch --help`) for the full option list.

Flags shared by both subcommands:

- `--config path/to.yaml` — use a different config file (the root `config.yaml` auto-loads otherwise)
- `--provider PROVIDER` — any-llm provider name; `claude` aliases to `anthropic`
- `--no-validation-loop` — skip the Docker validation/fixer loop entirely (prompt-debugging only)
- `--max-iterations N` — cap fixer retries
- `--serve` — on success, emit a portable `Dockerfile` + `docker-run.sh` into the generated project

`run` only:

- `--reasoning-effort none|low|medium|high` — overrides every agent's effort at once (per-agent defaults live beside model names in `provider_models:`; see below)
- `--user-validate` — pause after parsing to let the user review/correct the `ProblemSpec` interactively before code generation

`batch` only:

- `--workers N` — size of the parallel container pool
- `--input-dir DIR` — directory to scan for `*.txt` (default `user_input/`)
- Every batch ends with a benchmark table (Compiles · Solver runs · Quarkus runs · Endpoints work · Docker works · Cost · Tokens). Compiles/Solver/Cost/Tokens are free from pipeline results; Quarkus/Endpoints/Docker are measured by a serial post-batch Docker pass (`src/vibesolve/benchmarking/`). The Docker column needs `--serve` (else 0); `--no-validation-loop` skips the table entirely.

## Pipeline architecture

```
user_input/*.txt
   │
   ▼   Parser (gpt-5-mini)                            → ProblemSpec
   │
   ▼   [User Validator — Explain / Update]            ← --user-validate (optional, interactive)
   │
   ▼   Model Builder      → Delta → ProjectManifest   (domain classes + skeleton pom.xml)
   ▼   Constraint Builder → Delta → ProjectManifest   (ConstraintProvider)
   ▼   IO Agent           → Delta → ProjectManifest   (JsonIO)
   ▼   Integrator         → Delta → ProjectManifest   (Main, REST, solverConfig, tests, full pom.xml)
   │
   ▼   Reviewer            → Delta (pre-flight static fixes; on by default)
   ▼   Docker validate    (mvn clean compile  →  mvn exec:java [timeout 30s]  →  mvn test)
   │
   ├─ PASS  → write ProblemSpec.json, ProjectManifest.json, project dir + .zip
   └─ FAIL  → Fixer (gpt-5-mini, high effort) → re-validate, up to N iterations
```

Each agent except Parser/UserValidator returns a `Delta` (`changed_files`, `deleted_files`, optional `projectName`/`basePackage`, optional `explanation`), which is merged into the accumulated `ProjectManifest` by
`utils.patch_utils.apply_delta`. Agents emit only the files they changed — never the whole project — which keeps later-stage output small.

## Where things live

```
src/vibesolve/
├── agents/
│   ├── client.py          BaseAgentCaller + AnyLLMAgentCaller + compatibility aliases + make_caller_factory
│   └── prompts.py         load_prompt() + _PROMPT_FILES (agent → filename map)
├── cli/                   main.py (entry point) + run_single.py (`run`) + run_batch.py (`batch`)
├── config/settings.py     AppSettings (pydantic-settings) + load_settings(yaml)
├── models/
│   ├── domain.py          ProblemSpec, ProjectManifest, Delta, FileEntry, UserValidationExplanation
│   └── results.py         ValidationResult, ProblemResult, BatchSummary, FixAttempt
├── packaging.py           emit_docker_artifacts() — writes Dockerfile/docker-run.sh for --serve
├── pipeline/
│   ├── runner.py          run_problem() — the orchestrator; GENERATION_STAGES defines the agent order
│   └── user_validator.py  run_user_validation_loop() — explain/update interactive loop
├── prompts/               One .txt file per agent — these ARE the agents
├── reporting/kpi_tracker.py    aggregate_results, generate_report
├── utils/
│   ├── logging_config.py  configure_logging() — structlog
│   └── patch_utils.py     apply_delta(manifest, delta) → manifest
└── validation/
    ├── container_pool.py     DockerContainerPool for parallel workers
    ├── docker_validator.py   DockerValidator (persistent container; compile/run/test phases)
    └── feedback_controller.py FeedbackController — Reviewer → validate → Fixer loop

tests/                     Offline unit/smoke tests (no API key / Docker needed) — run with `pytest`
docker/Dockerfile          eclipse-temurin:17-jdk-jammy + Maven
docker/pom-warmup.xml      warms the Maven cache at image build with the generated projects' dependency set
user_input/*.txt           Problem descriptions — input to the pipeline
config.yaml                Project-level settings (auto-loaded; CLI flags override)
.env.local                 provider credentials — NEVER commit (copy from .env.example)
logs/run_<ts>/             pipeline.log + per-agent raw response files
results/run_<ts>/          ProblemSpec.json, ProjectManifest.json, <project>/, <project>.zip
```

Rough dependency direction (no import cycles): `models → utils → agents →
validation → pipeline → cli`; `config` is a leaf used by `cli`.

## Configuration (priority high → low)

1. CLI flags
2. Environment variables (`OPENAI_API_KEY`, `PROVIDER_MODELS__OPENAI__FIXER__MODEL=gpt-5`, `PROVIDER=bedrock`, …)
3. `config.yaml` at repo root (auto-loaded if present)
4. `.env.local`
5. Built-in defaults in `config/settings.py`

`PROVIDER_MODELS__<PROVIDER>__<AGENT>__MODEL` and
`PROVIDER_MODELS__<PROVIDER>__<AGENT>__EFFORT` env vars override per-agent
model and reasoning-effort settings. Pydantic-settings parses the `__` nesting.

## Modifying agent behavior

- **Change what an agent does** → edit the corresponding `src/vibesolve/prompts/<agent>.txt`. The file content IS the system prompt.
- **Add a new agent** → add a `.txt` to `prompts/`, register it in `agents/prompts.py:_PROMPT_FILES`, add the agent to `config/settings.py:AgentModels` and each default `provider_models` entry with `model` and `effort`, and wire it into `pipeline/runner.py:GENERATION_STAGES` (or `FeedbackController` for a validation-time agent).
- **Output schema** → most agents output `Delta`; Parser outputs `ProblemSpec`; User-Validator-Explain outputs `UserValidationExplanation`. All are Pydantic models in `models/domain.py`.
- **Per-agent reasoning effort** → `provider_models.<provider>.<agent>.effort` in `config.yaml` (defaults: reviewer=medium, fixer=high, everything else none). Read in `agents/client.py` from the same provider config entry as the model name; `--reasoning-effort` overrides every agent at once for a run.

`BaseAgentCaller.call_typed()` retries on JSON-parse failure. Provider calls go
through any-llm's unified completion API. `provider` is passed through to
any-llm after the compatibility alias `claude -> anthropic` is applied.
`_extract_and_repair()` strips code fences and runs `json_repair`.

## Generated-project conventions (encoded in prompts)

These are stable invariants of the generated Java output — relevant when
debugging fixer loops or editing prompts:

- **Stack**: Java 17, Quarkus 3.31.2, Timefold Solver 1.31.0, Maven, `HardSoftScore` (the score type is fixed — do not use another).
- **Package layout**: `com.example.*` (`domain`, `solver`, `io`, `generator`, `rest`).
- **REST resource**: must be `rest/SolverResource.java` implementing the full mandatory endpoint set from the prompt template (`/api/all`, generate/solve/status/stop/analyze). Do NOT name it `MyResource.java`.
- **Imports**: Jakarta EE 10 — `jakarta.ws.rs.*`, never `javax.ws.rs.*` (Quarkus 3.x).
- **Pom dependencies**: only `timefold-solver-quarkus`, `-quarkus-jackson`, `-test`, `-bom`. The artifact `timefold-solver-constraints` does NOT exist (a common fixer footgun).
- **`exec-maven-plugin` 3.6.3** with `<configuration><mainClass>` set to the fully-qualified Main class — required for `mvn exec:java` to find it.
- **Solver termination**: 15 seconds in `solverConfig.xml`.
- **Two solver configs**: `solverConfig.xml` is production (`REPRODUCIBLE` mode, used by the Quarkus REST layer); `solverConfigTest.xml` is test-only (`FULL_ASSERT`, far slower, activated solely under the `%test` profile). Never let FULL_ASSERT run when serving requests.
- **Tests**: generate datasets via `DataGenerator`, run the solver in `FULL_ASSERT`, and assert at least one planning variable is assigned.
- **`@PlanningSolution`** must include a `solverStatus` field of type `SolverStatus` (a standalone top-level class, NOT `SolverManager.SolverStatus`); do not add `@JsonIgnore`.
- **XML comments**: only `<!-- ... -->`; `<!-- ... --->` (triple dash) is a hard XML parse failure.

## Docker validator

The persistent container `timefold-validator-persistent` (image `timefold-validator`) stays running between fix iterations to keep the Maven cache warm — cold start is slow, subsequent compiles are fast. For batch runs, `DockerContainerPool` pre-starts a pool of N persistent containers.

`DockerValidator.validate()` has three phases:
1. `mvn [clean] compile` — compile errors feed the fixer loop
2. `mvn exec:java` wrapped in `timeout 30` — exit code 124 (timed out) counts as **pass** (the solver was running)
3. `mvn test` — tests must pass

`FeedbackController._select_relevant_files()` ships only the files referenced in the compile-error output to the fixer, keeping token usage small. `_pom_changed()` toggles incremental compile (skip `mvn clean`) when only Java changed.

## Output layout

```
logs/run_<ts>/
  pipeline.log                       # structured log (one per run)
  <agent>-response_<id>.txt          # raw LLM response, one file per agent call

results/run_<ts>/
  ProblemSpec.json
  ProjectManifest.json
  problem-spec-review.md             # only when --user-validate was used
  <project-name>/                    # extracted Maven project
    pom.xml
    src/main/java/com/example/...
    Dockerfile  .dockerignore  docker-run.sh   # only with --serve
  <project-name>.zip
```

For batch runs: `logs/batch_<ts>/<problem>/` and `results/batch_<ts>/<problem>/`,
plus `summary.json` + `summary.txt`.

## Iterating quickly

- **Run the offline tests**: `pytest` — no API key or Docker needed.
- **See what an agent returned**: read `logs/run_<ts>/<agent>-response_*.txt` — raw responses, one file per call.
- **Skip Docker to isolate prompt issues**: `vibesolve run --no-validation-loop`.
- **Debug a single problem end-to-end**: `vibesolve run user_input/<file>.txt --max-iterations 3 --reasoning-effort medium`.
- **Run the containerized output**: `vibesolve run --serve`, then `cd results/run_<ts>/<project>/ && ./docker-run.sh` → http://localhost:8080/q/swagger-ui.

## Project rules to respect when editing

- **Never commit** `.env.local`, `logs/`, `results/`, `.validation_temp/` — all gitignored.
- **Don't `git add -A`** — large local-only experiment dirs (`assets/`, `vanilla_api_tests/`, `advent_problems/`) are untracked but NOT gitignored; stage files explicitly.
- **Match the surrounding style** — Pydantic v2 models, structlog logging, typed `pathlib.Path`, no `dict[str, Any]` at boundaries.
- **Prompts are first-class code** — they encode hard-won invariants (Jakarta vs javax, missing artifacts, XML comment rules). Don't simplify prompt instructions without checking whether they fix a real failure mode.
- **`apply_delta` is the only legitimate way to merge agent output** — don't write ad-hoc merging logic.
- **Generated artifacts in `results/` are ephemeral** — never edit a generated `.java`/`pom.xml` to fix a bug; fix the prompt or the fixer instead.
- **Add or update `tests/`** when changing the CLI surface, settings, or model/merge logic.
