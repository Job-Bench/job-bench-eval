# JobBench

JobBench evaluates agentic CLI tools (Claude Code, Codex CLI, OpenCode) on the tedious, multi-source pre-processing that experienced professionals most want offloaded: reconciling contradictory data, cross-referencing records, tracing citations. Tasks are sourced from Workbank, a worker-desire survey across 35 white-collar occupations, shifting the question from *what can be automated* to *what workers actually want automated*. Each task ships with a working directory and a weighted rubric; after the agent finishes, an LLM judge scores the deliverables against that rubric.

- 🌐 [Project Website](https://job-bench.github.io/) - Learn more about JobBench
- 🔧 [Github Repo](https://github.com/Job-Bench/job-bench-eval/) - Access the eval scirpt of JobBench
- 🤗 HF Datasets - Find all JobBench datasets
  - [JobBench (Main)](https://huggingface.co/datasets/JobBench/job-bench)

The dataset has **65 main tasks** and **63 easy tasks**. Both evaluation routes
default to `main`, the leaderboard split, and use **xAI `grok-4.3`** as the judge.
Use `easy` explicitly for simplified tasks. Some tasks require live web search;
search and browser tools depend on the selected agent.

## Choose an evaluation route

| Route | Workflow | Results |
|---|---|---|
| [Ordinary evaluation](eval/README.md) | Run a repository CLI runner, then the judge | `dataset/<split>/<profession>/taskN/` |
| [Harbor evaluation](harbor/README.md) | Run an agent in an isolated environment, followed by verification | `harbor/jobs/` |

Start from the repository root:

```bash
git clone https://github.com/Job-Bench/job-bench-eval.git
cd job-bench-eval
```

## Ordinary evaluation

Supports **Claude Code, Codex CLI, and OpenCode**. Install and authenticate your
chosen CLI using the [setup guide](eval/README.md#setup-and-authentication).
Shared requirements are `uv`, `jq`, `timeout`, and Docker for Excel calculation.

```bash
# Set up Python, the calculator, and both dataset splits.
./setup.sh
```

Choose one runner:

| Agent | Example command |
|---|---|
| Claude Code | `BENCHMARK_MODELS="claude-sonnet-4-6_cc" ./eval/run_benchmark_claude_code_cli.sh` |
| Codex CLI | `BENCHMARK_MODELS="gpt-5.4" ./eval/run_benchmark_codex_cli.sh` |
| OpenCode | `BENCHMARK_MODELS="openai/gpt-5.4\|gpt-5-4" ./eval/run_benchmark_opencode.sh` |

For OpenCode, install `git` and `bun`, then run
[`./setup_opencode.sh`](setup_opencode.sh), which pins `v1.14.18`.

After generation, score the outputs:

```bash
JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

Deliverables, trajectories, and scores are saved under each task in
`model_output/`, `model_traj/`, and `eval_result/`.
See the [ordinary evaluation guide](eval/README.md) for
authentication, model options, subsets, run labels, and judge configuration.
Both evaluation routes read saved Notebook outputs and PPTX tables, and use the
same pinned LibreOffice runtime for supported Excel formulas. Extraction records
identify the evidence and any limitations; see [judge evidence](eval/README.md#judge-evidence).

## Harbor evaluation

Supports Harbor's native agents and custom agents through the same interface.
Requires `uv` and Docker. Select an agent with `-a` and a model with `-m`;
the example below uses Terminus-2.

```bash
# Set up Harbor and refresh both dataset splits.
./harbor/setup.sh

# Authenticate the example model provider and the separate xAI judge.
export OPENAI_API_KEY="your_model_provider_key"
export JUDGE_API_KEY="your_xai_key"

# Generate deliverables and score them in one job.
./harbor/eval.sh --split main \
  -a terminus-2 -m openai/gpt-5.4
```

Jobs, deliverables, trajectories, verifier reports, and source provenance are
saved under `harbor/jobs/`. See the [Harbor guide](harbor/README.md) for
subsets, native configs, custom agents, and result details.

## Refreshing tasks and keeping results

Rerun `./setup.sh` or `./harbor/setup.sh` to refresh tasks from Hugging Face.
Existing results are preserved, and evaluation warns when local tasks are stale.
To evaluate updated tasks again, use a new ordinary `RUN_LABEL` or a new Harbor
job. See [rerunning](eval/README.md#refreshing-and-rerunning) for details.
