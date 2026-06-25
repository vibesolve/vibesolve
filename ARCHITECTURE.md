# Architecture & Internals

## Pipeline

Seven specialized agents run in sequence. Each one owns a single concern and passes a growing project manifest to the next stage.

```
user_input/*.txt
       │
       ▼
   [Parser]  ──────────────────────────────► ProblemSpec JSON
       │
       ▼  (--user-validate only)
[User Validator] ─────────────────────────► reviewed / corrected ProblemSpec
  (explain → review → update loop)
       │
       ▼
[Model Builder] ──────────────────────────► domain classes + skeleton pom.xml
       │
       ▼
[Constraint Builder] ─────────────────────► ConstraintProvider
       │
       ▼
   [IO Agent] ───────────────────────────► JsonIO + DataGenerator
       │
       ▼
  [Integrator] ─────────────────────────► Main, REST resource, solverConfig, tests, pom.xml
       │
       ▼
  [Reviewer]  (optional pre-flight review)
       │
       ▼
  Docker validate  ──────────────────────► mvn compile  +  mvn exec:java
       │                    │
     PASS                 FAIL
       │                    │
       ▼              [Fixer] ◄── ValidationError + error history
  Final project        │
                       └──► Docker validate  (up to N iterations)
```

Each agent outputs only the files it added or modified (a **delta**). The orchestrator merges deltas into a single accumulated manifest, so agents never waste tokens echoing back unchanged files.

### Agent responsibilities

| Agent | Input | Produces |
|---|---|---|
| **Parser** | Free-text problem description | `ProblemSpec` JSON |
| **User Validator — Explain** _(optional)_ | `ProblemSpec` | Plain-language markdown summary for user review |
| **User Validator — Update** _(optional, per feedback round)_ | `ProblemSpec` + user feedback | Corrected `ProblemSpec` |
| **Model Builder** | `ProblemSpec` | Java domain classes + skeleton `pom.xml` |
| **Constraint Builder** | `ProblemSpec` + manifest | `ConstraintProvider` implementation |
| **IO Agent** | `ProblemSpec` + manifest | `JsonIO` + `DataGenerator` classes |
| **Integrator** | `ProblemSpec` + manifest | `Main`, REST resource, `solverConfig.xml`, tests, complete `pom.xml` |
| **Reviewer** | `ProblemSpec` + manifest | Pre-flight fixes (imports, annotations, dependencies) |
| **Fixer** | `ProblemSpec` + manifest + `ValidationError` | Targeted fixes for compile / runtime / test errors |

---

## Repository structure

```
agents_arch/
├── src/
│   └── vibesolve/
│       ├── agents/
│       │   ├── client.py              # AgentCaller — provider-agnostic OpenAI + Anthropic wrapper
│       │   └── prompts.py             # Prompt file loader
│       ├── benchmarking/
│       │   ├── evaluator.py           # Docker benchmark stages (package, Quarkus boot, endpoint probe, Docker build)
│       │   └── table.py               # derive Compiles/Solver metrics + render benchmark table
│       ├── cli/
│       │   ├── main.py                # vibesolve entry point (run/batch subcommands)
│       │   ├── run_single.py          # `vibesolve run` command
│       │   ├── run_batch.py           # `vibesolve batch` command (always benchmarks)
│       ├── config/
│       │   └── settings.py            # AppSettings (pydantic-settings)
│       ├── models/
│       │   ├── domain.py              # ProblemSpec, ProjectManifest, Delta, FileEntry
│       │   └── results.py             # ValidationResult, ProblemResult, BatchSummary
│       ├── packaging.py               # emit_docker_artifacts() — Dockerfile + docker-run.sh for --serve
│       ├── pipeline/
│       │   ├── runner.py              # run_problem() — orchestrator
│       │   └── user_validator.py      # run_user_validation_loop() — explain/update loop
│       ├── prompts/                   # Agent system prompt .txt files
│       ├── reporting/
│       │   └── kpi_tracker.py         # aggregate_results, generate_report
│       ├── utils/
│       │   ├── logging_config.py      # structlog setup + per-run BoundLogger
│       │   └── patch_utils.py         # apply_delta (Delta → ProjectManifest merge)
│       └── validation/
│           ├── container_pool.py      # DockerContainerPool for parallel batch runs
│           ├── docker_validator.py    # DockerValidator — compile/run/test in container
│           └── feedback_controller.py # Reviewer → validate → fix loop
├── docker/
│   ├── Dockerfile                     # eclipse-temurin:17-jdk-jammy + Maven
│   └── pom-warmup.xml                 # Pre-bakes Maven deps into the validator image (auto-built on first use)
├── docs/                              # Architecture and internals documentation
├── user_input/                        # Problem description .txt files
├── pyproject.toml                     # Package metadata + CLI entry points
└── .env.local                         # OPENAI_API_KEY / ANTHROPIC_API_KEY (not committed)
```

### Import topology (no cycles)

```
models ← utils ← agents ← validation ← pipeline ← cli
config ──────────────────────────────────────────► cli
benchmarking ─────────────────────────────────────► cli
```

---

## Key design decisions

| Decision | Rationale |
|---|---|
| **Delta-based output** | Agents return only changed files, not the full manifest. Saves 60–80% of output tokens on later pipeline stages. |
| **Typed Pydantic models** | All inter-agent data (`ProblemSpec`, `ProjectManifest`, `Delta`) is validated at parse time — no `dict[str, Any]` at boundaries. |
| **Provider-agnostic agent caller** | A common `BaseAgentCaller` interface fronts both OpenAI (Responses API) and Anthropic (Messages API); `--provider` selects the implementation. Per-agent reasoning effort maps to OpenAI `reasoning.effort`, or to the Anthropic thinking mode — adaptive thinking (`output_config.effort`) on Sonnet 4.6 / Opus 4.5+ / Fable 5, and `budget_tokens` on older models. |
| **Structured JSON output** | Agents return guaranteed-valid JSON (OpenAI `json_object` mode), so no regex extraction is needed. |
| **Persistent Docker container** | The validator container stays running between iterations, keeping the Maven cache warm. Cold start ~30 s; subsequent compiles ~5–10 s. |
| **Incremental Maven compile** | `mvn clean` is skipped when only `.java` files changed (not `pom.xml`), cutting iteration time significantly. |
| **Selective file injection** | The fixer receives only files referenced in the error output, not the entire manifest, keeping context small. |
| **structlog with bound context** | Per-worker structured logs tagged by `problem` and `worker` — essential when parallel workers produce interleaved output. |

---

## Generated project stack

Every generated project uses:

- **Timefold Solver 1.31.0** — constraint solving engine
- **Quarkus 3.31.2** — runtime with REST endpoints under `/api/*` (`/api/solve`, `/api/solution/{jobId}`, `/api/status/{jobId}`, `/api/stop/{jobId}`, …)
- **Java 17** — via `eclipse-temurin:17-jdk-jammy` in Docker
- **Maven** — build system; validated via `mvn compile` + `mvn exec:java`
- **HardSoftScore** — default scoring; 15-second solver termination

---

## Benchmarking & containerization

- **`vibesolve batch` always benchmarks.** After a batch completes, every project is scored on two kinds of columns: those derived straight from the pipeline's own results (*Compiles · Solver runs · Cost · Tokens*) and those measured by building each project, starting the app, and calling its endpoints (*Quarkus runs · Endpoints work · Docker works*). Code lives in `benchmarking/` (`evaluator.py` = Docker stages, `table.py` = derivation + rendering).
- **`--serve` containerizes a generated project.** On success, `packaging.py` emits a `Dockerfile`, `.dockerignore`, and `docker-run.sh` into the project so it can be built and run standalone. The "Docker works" benchmark column builds this `Dockerfile`, so it only scores non-zero when `--serve` is set.
