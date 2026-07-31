<p align="center">
  <img src="vs-icon.svg" alt="VibeSolve" width="120" />
</p>

# VibeSolve

Describe an optimization problem in plain English. Get a complete, runnable [Timefold Solver](https://timefold.ai/) project (Java domain model, constraints, REST API, tests, and solver config), automatically validated by Docker.

> I have a school timetabling problem with teachers, classes, lessons, rooms and a 1-week grid. Schedule all lessons such that no teacher teaches 2 lessons at the same time, no room hosts 2 lessons at the same time, and lessons for the same class group are spread across the week (1 per day).

→ Ready-to-build Quarkus + Timefold Solver Maven project.

## Setup

```bash
git clone https://github.com/vibesolve/vibesolve.git
cd vibesolve

conda create -n vibesolve python=3.11 -y
conda activate vibesolve
pip install -e .

# API key - gitignored, never committed
cp .env.example .env.local
# now open .env.local and set provider credentials, e.g. OPENAI_API_KEY=sk-...
# for --provider claude/anthropic, set ANTHROPIC_API_KEY=...
```

## Usage

Start Docker on your machine: Linux run `sudo systemctl start docker`, macOS or Windows launch Docker Desktop.

Activate the environment:

```bash
conda activate vibesolve
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

Settings live in `config.yaml` at the project root, loaded automatically. Pass `--config other.yaml` to use a different file. CLI flags override it. API keys stay in `.env.local`. Provider calls are routed through [any-llm](https://github.com/mozilla-ai/any-llm); use any installed any-llm provider name with `--provider`, with `claude` kept as an alias for `anthropic`. Per-agent model IDs and reasoning efforts are configured under `provider_models.<provider>.<agent>`; an optional `_default` key in a provider block sets the model and/or effort for every agent at once, with per-agent entries overriding it. To run without a paid API key, use `--provider openrouter` with a free model from openrouter.ai — see the `openrouter` block in `config.yaml`.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | the setup steps use conda |
| Docker 20+ | for automated validation |
| LLM credentials | Provider-specific credentials for the selected any-llm backend, for example OpenAI API keys, Anthropic API keys, or AWS credentials for Bedrock |

## How it works

A pipeline of specialized LLM agents (Parser → Model Builder → Constraint Builder → IO → Integrator → Reviewer → Fixer) builds the project step by step, then compiles and runs it in Docker. If it fails, the Fixer agent corrects it and retries.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline diagram, agent responsibilities, and design decisions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, code style, and the PR workflow.

## License

[MIT](LICENSE) - free to use, modify, and distribute.
