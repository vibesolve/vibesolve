# VibeSolve

Describe an optimization problem in plain English. Get a complete, runnable [Timefold Solver](https://timefold.ai/) project (Java domain model, constraints, REST API, tests, and solver config), automatically validated by Docker.

```
I have a school timetabling problem with teachers, classes, lessons, rooms and a 1-week grid. Schedule all lessons such that no teacher teaches 2 lessons at the same time, no room hosts 2 lessons at the same time, and lessons for the same class group are spread across the week (1 per day).
```

→ Ready-to-build Quarkus + Timefold Solver Maven project.

## Setup

```bash
git clone https://github.com/amine-athmani/vibesolve.git
cd vibesolve

conda create -n vibesolve python=3.11 -y
conda activate vibesolve
pip install -e .

# API key - gitignored, never committed
cp .env.example .env.local
# now open .env.local and set OPENAI_API_KEY=sk-...


```

## Usage

Run a single problem, or batch the whole directory:

```bash
conda activate vibesolve   # once per shell
# start Docker — Linux: sudo systemctl start docker  |  macOS/Windows: launch Docker Desktop

vibesolve run                            # the bundled school timetabling problem example
vibesolve run user_input/my-problem.txt  # your own problem file
vibesolve batch                          # every *.txt in user_input/, in parallel
```

Common flags:

```bash
vibesolve run --serve                # also emit a Dockerfile + docker-run.sh
vibesolve run --user-validate        # review the parsed spec before generation
vibesolve run --no-validation-loop   # skip the validation/fixer loop, useful for testing one shot performance
vibesolve batch --workers 5          # use N parallel workers (default 3)
```

Run `vibesolve --help` for the full list, or see the [CLI reference](CONTRIBUTING.md#cli-reference). Generated projects land in `results/run_<timestamp>/`; structured logs in `logs/run_<timestamp>/`.

## Configuration

Settings live in `config.yaml` at the project root, loaded automatically. Pass `--config other.yaml` to use a different file. CLI flags override it; API keys stay in `.env.local`.

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ | via conda recommended |
| Docker 20+ | for automated validation; skippable with `--no-validation-loop` |
| LLM API key | OpenAI ([get one](https://platform.openai.com/api-keys)), or an Anthropic key for `--provider claude` |


## How it works

A pipeline of specialized LLM agents (Parser → Model Builder → Constraint Builder → IO → Integrator → Reviewer → Fixer) builds the project step by step, then compiles and runs it in Docker. If it fails, the Fixer agent corrects it and retries.

See [ARCHITECTURE.md](ARCHITECTURE.md) for the pipeline diagram, agent responsibilities, and design decisions.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for dev setup, code style, and the PR workflow.

## License

[MIT](LICENSE) - free to use, modify, and distribute.
