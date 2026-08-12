<p align="center">
  <img src="vs-icon.svg" alt="VibeSolve" width="120" />
</p>

# VibeSolve

Describe an optimization problem in plain English. Get a complete, runnable [Timefold Solver](https://timefold.ai/) project (Java domain model, constraints, REST API, tests, and solver config), automatically validated by Docker.

> I have a school timetabling problem with teachers, classes, lessons, rooms and a 1-week grid. Schedule all lessons such that no teacher teaches 2 lessons at the same time, no room hosts 2 lessons at the same time, and lessons for the same class group are spread across the week (1 per day).

→ Ready-to-build Quarkus + Timefold Solver Maven project.

## Setup

```bash
# Install uv (skip if you already have it)
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS / Linux
# Windows (PowerShell): powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

git clone https://github.com/vibesolve/vibesolve.git
cd vibesolve

uv sync                      # creates .venv/ and installs vibesolve
source .venv/bin/activate    # Windows: .venv\Scripts\activate

# API key - gitignored, never committed
cp .env.example .env.local
# now open .env.local and set OPENAI_API_KEY=sk-...
```

With the environment activated, `vibesolve` is on your `PATH` — no prefix needed. Activation lasts for the shell session; in a fresh shell either re-run `source .venv/bin/activate` or prefix a one-off command with `uv run` (e.g. `uv run vibesolve run`).

## Usage

Start Docker on your machine: Linux run `sudo systemctl start docker`, macOS or Windows launch Docker Desktop.

```bash
vibesolve --help
```

Solve the bundled school-timetabling example:

```bash
vibesolve run
```

Solve your own problem file:

```bash
vibesolve run user_input/my-problem.txt
```

Solve every `*.txt` in `user_input/`, in parallel:

```bash
vibesolve batch
```

### Common flags

Also emit a Dockerfile and `docker-run.sh` next to the generated project:

```bash
vibesolve run --serve
```

Review the parsed spec before code generation begins:

```bash
vibesolve run --user-validate
```

Run a batch with more parallel workers (default 3):

```bash
vibesolve batch --workers 5
```

Run `vibesolve --help` for the full list, or see the [CLI reference](CONTRIBUTING.md#cli-reference). Generated projects land in `results/run_<timestamp>/`. Structured logs in `logs/run_<timestamp>/`.

## Configuration

Settings live in `config.yaml` at the project root, loaded automatically. Pass `--config other.yaml` to use a different file. CLI flags override it. API keys stay in `.env.local`.

## Prerequisites

| Requirement | Notes |
|---|---|
| [uv](https://docs.astral.sh/uv/) | manages the environment and installs Python if needed |
| Python 3.11+ | `uv sync` installs a suitable version automatically |
| Docker 20+ | for automated validation; skippable with `--no-validation-loop` |
| LLM API key | OpenAI ([get one](https://platform.openai.com/api-keys)), or an Anthropic key for `--provider claude` |

## How it works

A pipeline of specialized LLM agents (Parser → Model Builder → Constraint Builder → IO → Integrator → Reviewer → Fixer) builds the project step by step, then compiles and runs it in Docker. If it fails, the Fixer agent corrects it and retries.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline diagram, agent responsibilities, and design decisions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, code style, and the PR workflow.

## License

[MIT](LICENSE) - free to use, modify, and distribute.
