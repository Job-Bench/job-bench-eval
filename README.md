# JobBench

JobBench evaluates agentic CLI tools on professional tasks that workers want
offloaded: reconciling data, cross-referencing records, and tracing citations.
Tasks draw on Workbank's worker-desire survey across **35 professions**. Each
includes source files and a weighted rubric for judging the agent's deliverables.

[Website](https://job-bench.github.io/) ·
[GitHub](https://github.com/Job-Bench/job-bench-eval/) ·
[Hugging Face dataset](https://huggingface.co/datasets/JobBench/job-bench)

The dataset has **65 main tasks** and **63 easy tasks**. Both evaluation routes
default to `main`, the leaderboard split, and use **xAI `grok-4.3`** as the judge.
Use `easy` explicitly for simplified tasks. Some tasks require live web search;
search and browser tools depend on the selected agent.

## Choose an evaluation route

| Route | Workflow | Results |
|---|---|---|
| [Ordinary evaluation](eval/README.md) | Run a repository CLI runner, then the judge | `dataset/<split>/<profession>/taskN/` |
| [Harbor evaluation](harbor/README.md) | Run an agent in an isolated environment, followed by verification | `harbor/jobs/` |

Both routes support OpenCode and other agents. The official reference is
**OpenCode v1.14.18**, pinned in [setup_opencode.sh](setup_opencode.sh).
Harbor's matching npm release is pinned with `--ak version=1.14.18`;
it does not include local checkout edits.

Start from the repository root:

```bash
git clone https://github.com/Job-Bench/job-bench-eval.git
cd job-bench-eval
```

## Ordinary evaluation

Requires `uv`, `jq`, `timeout`, and, for OpenCode, `git` and `bun`.

```bash
# Set up Python, refresh both dataset splits, and prepare OpenCode.
./setup.sh
./setup_opencode.sh

# Authenticate the example model provider and the separate xAI judge.
export OPENAI_API_KEY="your_model_provider_key"
export JUDGE_API_KEY="your_xai_key"

# Generate deliverables, then score outputs.
SPLIT=main BENCHMARK_MODELS="openai/gpt-5.4|gpt-5-4" \
  ./eval/run_benchmark_opencode.sh
SPLIT=main EVAL_MODEL="gpt-5-4" uv run ./eval/run_judge.sh
```

Deliverables, trajectories, and scores are saved under each task in
`model_output/`, `model_traj/`, and `eval_result/`. Claude Code and Codex CLI
are also supported. See the [ordinary evaluation guide](eval/README.md) for
authentication, model options, subsets, run labels, and judge configuration.

## Harbor evaluation

Requires `uv` and Docker. Setup creates its own Python environment and task
generation under `harbor/`.

```bash
# Set up Harbor and refresh both dataset splits.
./harbor/setup.sh

# Authenticate the example model provider and the separate xAI judge.
export OPENAI_API_KEY="your_model_provider_key"
export JUDGE_API_KEY="your_xai_key"

# Generate deliverables and score them in one job.
./harbor/eval.sh --split main \
  -a opencode -m openai/gpt-5.4 --ak version=1.14.18
```

Jobs, deliverables, trajectories, verifier reports, and source provenance are
saved under `harbor/jobs/`. The [OpenCode config](harbor/configs/opencode.yaml)
provides the same version pin. See the [Harbor guide](harbor/README.md) for
subsets, native configs, custom agents, and result details.

## Refreshing tasks and keeping results

Evaluation checks the recorded dataset revision against HF `main` and warns if
an update is available. The check is advisory; evaluation continues.

Rerun the setup command for your chosen route to check the latest Hugging Face
`main` revision. Every setup checks upstream and reuses unchanged cached content;
it does not skip the check because local tasks already exist. Both splits are
downloaded and validated before the new sources are activated.

Ordinary setup updates managed source files, backs up overwritten local task
edits, preserves evaluation history and local extras, and archives removed
Hugging Face tasks outside the active splits. Harbor publishes a new task
generation while retaining old generations and jobs. Either setup accepts
`--revision <hf-commit>` to select a source version.

Refreshing tasks does not rerun evaluation. Ordinary runners resume from
existing model outputs, so use a new `RUN_LABEL` when evaluating changed tasks
and use that unique run label as the judge's `EVAL_MODEL` filter. See
[refreshing and rerunning](eval/README.md#refreshing-and-rerunning) for an example.
