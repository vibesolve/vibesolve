# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Working conventions

- **Never commit without explicit user approval.** Always show the proposed commit(s) and wait for the user to say "go ahead" before running `git commit`.
 See also [AGENTS.md](AGENTS.md) — a guide for AI coding agents covering the same pipeline plus generated-project conventions and debugging shortcuts; keep the two in sync when architecture changes.

## Overview

Multi-agent system that generates complete Timefold Solver projects from natural language problem descriptions. Uses OpenAI API with a sequential pipeline (up to 9 agents) where each agent specializes in one aspect of code generation. Generated projects are validated via Docker (Maven compile + execution) and automatically fixed by a feedback loop. An optional interactive user-validation step lets users review and correct the parsed problem spec before code generation begins.

## Setup

```bash
# Create the environment and install the package (do this once, or after adding dependencies)
uv sync --extra dev

# Configure API key
# Create .env.local with OPENAI_API_KEY=your-key

# Build the Docker validator image (first run only)
docker build -t timefold-validator docker/
```

**Important:** This project requires Python 3.11+ for modern type annotations. Use `uv run ...` for Python commands so the project environment is selected automatically.

## Running the Pipeline

```bash
# Single problem (default: user_input/timetable.txt)
uv run vibesolve run

# Specific input file
uv run vibesolve run user_input/hospital-rostering.txt

# Skip the Docker validation/fixer loop
uv run vibesolve run --no-validation-loop

# Pause after parsing to review and correct the problem spec before code generation
uv run vibesolve run --user-validate

# Custom reasoning effort and fixer iterations
uv run vibesolve run --reasoning-effort medium --max-iterations 5

# Emit a Dockerfile + docker-run.sh into the generated project
# (only happens on success; the script is NOT started automatically)
uv run vibesolve run --serve

# Parallel batch across all *.txt in user_input/
uv run vibesolve batch

# Batch with more workers
uv run vibesolve batch --workers 5

# Batch + emit Dockerfiles so the benchmark can also measure "Docker works"
uv run vibesolve batch --serve

```

**Output locations:**
- Logs: `logs/run_<timestamp>/pipeline.log` + individual agent response files
- Results: `results/run_<timestamp>/` containing `ProblemSpec.json`, `ProjectManifest.json`, generated project files, and a `.zip` archive
- `results/run_<timestamp>/problem-spec-review.md` — plain-language spec summary written during `--user-validate`
- `logs/batch_<timestamp>/benchmark-results.csv` + `benchmark.json` — the benchmark table (see below); the table is also appended to `summary.txt`

### Benchmark table (`uv run vibesolve batch`)

Every `uv run vibesolve batch` ends with a benchmark table — the same columns as the vanilla-model benchmark: **Compiles · Solver runs · Quarkus runs · Endpoints work · Docker works · Cost · Tokens**. (`uv run vibesolve run` does not benchmark — batch is the benchmarking entry point; `run` is for solving a single problem.)

- **`Compiles`, `Solver runs`, `Cost`, `Tokens`** are derived for free from the pipeline's own results — `compilation`/`runtime` phase outcomes and `kpi_tracker` token/cost totals. No Docker re-run.
- **`Quarkus runs`, `Endpoints work`, `Docker works`** are always measured by a serial post-batch pass per project — `mvn package` → boot the Quarkus fast-jar → probe every endpoint → build the project's `Dockerfile`. It uses a single host port (`18080`), so it runs after the parallel workers, not alongside them.
- **`--serve` and the Docker column:** "Docker works" builds each project's `Dockerfile`, which only `--serve` emits. Without `--serve` that column is `0` (Docker was never requested) — this is intended, not a failure. The other columns are unaffected.
- **`--no-validation-loop` skips the benchmark entirely.** Without the validation loop a "success" no longer implies the project compiled or the solver ran, so the whole table would be meaningless.
- **Docker daemon required.** The validation loop already needs Docker, so a batch crashes before reaching the benchmark if Docker isn't running.

**Endpoint probe (`Endpoints work`):** a faithful minimal round-trip against the live app — `GET /api/generate` for a real problem payload → `POST /api/solve` to capture the returned jobId → reuse that jobId on `GET /api/solution/{jobId}`, `/status/{jobId}`, etc. → `DELETE /api/stop/{jobId}` to terminate (solving is never awaited). Score per project = 1.0 if all discovered endpoints return 2xx, 0.5 if some do, 0.0 if none (the "Endpoints work" column sums these across projects). Code lives in `src/vibesolve/benchmarking/` (`evaluator.py` = Docker stages, `table.py` = derivation + rendering).

### `--serve`: containerize the generated project

`uv run vibesolve run --serve` (also `uv run vibesolve batch --serve`) emits three extra files into each successfully-generated project directory:

- `Dockerfile` — two-stage build on public `eclipse-temurin:17-jdk-jammy` → `eclipse-temurin:17-jre-jammy`. Produces a Quarkus fast-jar and exposes port 8080. Portable across machines (not coupled to the internal `timefold-validator` image).
- `.dockerignore` — excludes `target/`, `.git/`, IDE files.
- `docker-run.sh` — one-shot helper that builds the image and runs it on port 8080. Override the port with `PORT=9090 ./docker-run.sh` or the image name with `IMAGE=my-name ./docker-run.sh`.

Usage:

```bash
uv run vibesolve run --serve user_input/timetable.txt
# ...pipeline runs, validation passes...

cd results/run_<timestamp>/<project-name>
./docker-run.sh
# → http://localhost:8080/q/swagger-ui
# → http://localhost:8080/api/all
```

Artifacts are only emitted on success. If the fixer loop exhausts retries, no Docker files are written. `--serve` combined with `--no-validation-loop` prints a warning — the emitted files target a project that was never validated.

The pipeline never starts Docker itself; it only writes files. You control when to build and run.

## Configuration

Settings are resolved in this priority order (highest → lowest):

1. **CLI flags** — `--max-iterations`, `--workers`, `--reasoning-effort`, `--no-validation-loop`, `--serve`, `--user-validate`
2. **Environment variables** — `OPENAI_API_KEY`, `MODELS__FIXER`, …
3. **YAML config file** — `config.yaml` (auto-loaded if present) or `--config <path>`
4. **`.env.local`** — API key fallback
5. **Built-in defaults**

### YAML config file

Edit `config.yaml` at the project root. It is loaded automatically on every run.
Pass a different file with `--config`:

```bash
uv run vibesolve run --config path/to/other.yaml
```

Available settings (all optional — omit to use the default):

```yaml
enable_caching: true
enable_docker_validation: true
max_fix_iterations: 10
default_workers: 3

# Per-agent reasoning effort (low | medium | high). Applies to both
# providers. --reasoning-effort overrides every agent at once.
efforts:
  parser:                  low
  model_builder:           low
  constraint_builder:      low
  io:                      low
  integrator:              low
  reviewer:                medium
  fixer:                   high
  user_validator_explain:  low
  user_validator_update:   low

models:
  parser:                  gpt-5-mini
  model_builder:           gpt-5-mini
  constraint_builder:      gpt-5-mini
  io:                      gpt-5-mini
  integrator:              gpt-5-mini
  reviewer:                gpt-5-mini
  fixer:                   gpt-5-mini
  user_validator_explain:  gpt-5-mini   # --user-validate: generates spec summary
  user_validator_update:   gpt-5-mini   # --user-validate: applies user feedback
```

Keep `OPENAI_API_KEY` in `.env.local` — never put it in `config.yaml`.

## Architecture

### Pipeline Flow

```
user_input/*.txt → Parser → [User Validator (--user-validate)] → Model Builder → Constraint Builder → IO Agent → Integrator
                              (explain → review → update loop)                                                        │
                                                                                                        [Optional] Reviewer
                                                                                                                      │
                                                                                                             Docker Validate
                                                                                                           (mvn compile + exec)
                                                                                                              │           │
                                                                                                            Pass        Fail
                                                                                                              │           │
                                                                                                       ProjectManifest   Fixer (max N×)
                                                                                                                         └─▶ Docker Validate
```

Each agent:
1. Receives a system prompt from `src/vibesolve/prompts/<agent>.txt`
2. Takes JSON input (ProblemSpec and/or ProjectManifest from previous agents)
3. Outputs a typed `Delta` (Pydantic model) that is merged into the accumulated manifest

### Agent Responsibilities

| Agent | Input | Output | Prompt File |
|-------|-------|--------|-------------|
| **Parser** | Free-text problem description | `ProblemSpec` | `prompts/parser.txt` |
| **User Validator — Explain** _(optional)_ | `ProblemSpec` | `UserValidationExplanation` (markdown summary) | `prompts/user-validator-explain.txt` |
| **User Validator — Update** _(optional, per feedback round)_ | `ProblemSpec` + user feedback | Updated `ProblemSpec` | `prompts/user-validator-update.txt` |
| **Model Builder** | `ProblemSpec` | `Delta` with Java domain model + Timefold annotations | `prompts/model-builder.txt` |
| **Constraint Builder** | `ProblemSpec` + `ProjectManifest` | `Delta` with ConstraintProvider implementation | `prompts/constraint-builder.txt` |
| **IO Agent** | `ProblemSpec` + `ProjectManifest` | `Delta` with JSON import/export + DataGenerator | `prompts/io.txt` |
| **Integrator** | `ProblemSpec` + `ProjectManifest` | `Delta` with Main class, REST API, pom.xml, tests | `prompts/integrator.txt` |
| **Reviewer** | `ProblemSpec` + `ProjectManifest` | `Delta` with pre-flight fixes + `explanation` field | `prompts/reviewer.txt` |
| **Fixer** | `ProblemSpec` + `ProjectManifest` + `ValidationError` | `Delta` with corrections + `explanation` field | `prompts/fixer.txt` |

### Key Data Structures (Pydantic models in `src/vibesolve/models/`)

**`ProblemSpec`** (`models/domain.py`) — output of the Parser:
```json
{
  "problemType": "scheduling",
  "entities": [...],
  "decisions": [...],
  "constraints": [...],
  "objectives": [...],
  "dataRequirements": [...],
  "assumptions": [...]
}
```

**`ProjectManifest`** (`models/domain.py`) — accumulated through pipeline:
```json
{
  "projectName": "...",
  "basePackage": "...",
  "files": [{ "path": "src/main/java/...", "content": "..." }]
}
```

**`Delta`** (`models/domain.py`) — returned by every agent except Parser and User Validator:
```json
{
  "changed_files": [{ "path": "...", "content": "..." }],
  "deleted_files": ["..."],
  "explanation": "..."
}
```

**`UserValidationExplanation`** (`models/domain.py`) — returned by the User Validator — Explain agent:
```json
{ "markdown": "## What is this problem about?\n..." }
```

## Package Structure

```
agents_arch/
├── src/
│   └── vibesolve/
│       ├── agents/
│       │   ├── client.py        # AgentCaller — OpenAI Responses API wrapper
│       │   └── prompts.py       # Prompt file loader
│       ├── benchmarking/
│       │   ├── evaluator.py     # Docker-heavy benchmark stages (package, Quarkus boot, endpoint round-trip, Docker build)
│       │   └── table.py         # derive Compiles/Solver from results + render benchmark table (text/CSV)
│       ├── cli/
│       │   ├── main.py          # vibesolve entry point (run/batch subcommands)
│       │   ├── run_single.py    # `uv run vibesolve run` command
│       │   ├── run_batch.py     # `uv run vibesolve batch` command (always benchmarks)
│       ├── config/
│       │   └── settings.py      # AppSettings (pydantic-settings)
│       ├── models/
│       │   ├── domain.py        # ProblemSpec, ProjectManifest, Delta, FileEntry, UserValidationExplanation
│       │   └── results.py       # ValidationResult, ProblemResult, BatchSummary, FixAttempt
│       ├── packaging.py        # emit_docker_artifacts() — Dockerfile + docker-run.sh for --serve
│       ├── pipeline/
│       │   ├── runner.py        # run_problem() — orchestrator (calls user_validator when enabled)
│       │   └── user_validator.py  # run_user_validation_loop() — explain/update loop
│       ├── prompts/             # Agent system prompt .txt files
│       │   ├── user-validator-explain.txt  # Generates plain-language spec summary
│       │   └── user-validator-update.txt   # Applies user feedback to ProblemSpec
│       ├── reporting/
│       │   └── kpi_tracker.py   # aggregate_results, generate_report
│       ├── utils/
│       │   ├── logging_config.py  # structlog setup + per-run BoundLogger
│       │   └── patch_utils.py   # apply_delta (Delta → ProjectManifest merge)
│       └── validation/
│           ├── container_pool.py      # DockerContainerPool for parallel batch runs
│           ├── docker_validator.py    # DockerValidator — compile/run/test in container
│           └── feedback_controller.py # Reviewer → validate → fix loop
├── docker/
│   ├── Dockerfile               # eclipse-temurin:17-jdk-jammy + Maven
│   └── pom-warmup.xml           # Pre-bakes Maven deps into Docker image
├── user_input/                  # Problem description .txt files
├── pyproject.toml               # Package metadata + CLI entry points
└── .env.local                   # OPENAI_API_KEY (not committed)
```

## Docker Validation & Feedback Loop

After the Integrator completes, the pipeline runs an automated validation and fix cycle:

1. **[Optional] Reviewer** — Static pre-flight code review; returns a fixed manifest before Docker is involved. Controlled by `FeedbackConfig.enable_pre_review` (default: `True`).
2. **Docker compile** — `mvn clean compile` inside a persistent container (`timefold-validator-persistent`), 5-minute timeout.
3. **Docker execute** — `mvn exec:java` wrapped in `timeout 30` (Linux). Exit code 124 = solver ran 30 s = pass.
4. **Fixer loop** — If either phase fails, the `fixer` agent receives the error context and returns a corrected `Delta`. Repeats up to `MAX_FIX_ITERATIONS`.
5. **Stuck detection** — If the last two iterations produced the same error, the controller logs a warning and the fixer's error history guides it to try a different approach.
6. **Incremental compile** — Skips `mvn clean` when only Java files changed and `pom.xml` is unchanged, reducing iteration time.

## Adding New Problem Types

1. Create a problem description in `user_input/`
2. Run: `uv run vibesolve run user_input/your-problem.txt`

## Timefold-Specific Notes

The prompts contain extensive Timefold documentation covering:
- Domain modeling (`@PlanningEntity`, `@PlanningVariable`, `@PlanningSolution`)
- Constraint Streams API (joiners, collectors, groupBy patterns)
- Common constraint patterns (employee scheduling, routing)

Generated projects use:
- Quarkus framework with REST endpoints
- Timefold Solver v1.31.0
- `HardSoftScore` scoring by default
- 15-second solver termination
