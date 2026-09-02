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
# now open .env.local and set provider credentials, e.g. OPENAI_API_KEY=sk-...
# for --provider claude/anthropic, set ANTHROPIC_API_KEY=...
# for --provider gemini, set GEMINI_API_KEY=...
# MISTRAL_API_KEY, COHERE_API_KEY, and DEEPSEEK_API_KEY work the same way
```

With the environment activated, `vibesolve` is on your `PATH` — no prefix needed. Activation lasts for the shell session; in a fresh shell either re-run `source .venv/bin/activate` or prefix a one-off command with `uv run` (e.g. `uv run vibesolve run`).

When a selected any-llm provider needs an optional SDK, VibeSolve installs that
provider extra into the active environment on first use. For example,
`--provider cohere` installs the version-matched `any-llm-sdk[cohere]` extra;
other optional provider extras are not installed. Transitive versions come from
the same `uv.lock` used by VibeSolve. VibeSolve then restarts the command once
so every dependency is loaded from the updated environment.

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

Run on the Gemini Developer API free tier after adding a key from
[Google AI Studio](https://aistudio.google.com/apikey) to `.env.local` as
`GEMINI_API_KEY`:

```bash
uv sync
vibesolve run --provider gemini
```

Free-tier requests are quota-limited, and Google may use free-tier content to
improve its products.

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

Settings live in `config.yaml` at the project root, loaded automatically. Pass `--config other.yaml` to use a different file. CLI flags override it. API keys stay in `.env.local`. Provider calls are routed through [any-llm](https://github.com/mozilla-ai/any-llm); use any supported any-llm provider name with `--provider`, with `claude` kept as an alias for `anthropic`. Missing optional provider SDKs are installed on demand from any-llm's own advertised extras. Built-in provider profiles live in `src/vibesolve/config/provider_models.json`, which is packaged in the wheel; `config.yaml` only overrides them. Custom providers must define `_default.model` or explicitly configure every agent, and per-agent entries override `_default` field by field. The `auto` effort omits the reasoning parameter and lets the provider/model choose; explicit efforts are preserved across retries.

## Prerequisites

| Requirement | Notes |
|---|---|
| [uv](https://docs.astral.sh/uv/) | manages the environment and installs Python if needed |
| Python 3.11+ | `uv sync` installs a suitable version automatically |
| Docker 20+ | for automated validation; skippable with `--no-validation-loop` |
| LLM API key | Provider-specific credentials for the selected any-llm backend, for example OpenAI API keys, Anthropic API keys, or AWS credentials for Bedrock |

## How it works

A pipeline of specialized LLM agents (Parser → Model Builder → Constraint Builder → IO → Integrator → Reviewer → Fixer) builds the project step by step, then compiles and runs it in Docker. If it fails, the Fixer agent corrects it and retries.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline diagram, agent responsibilities, and design decisions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, code style, and the PR workflow.

## License

[MIT](LICENSE) - free to use, modify, and distribute.
