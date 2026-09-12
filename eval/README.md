# Ordinary JobBench evaluation

Run all commands from the repository root. The runners execute a local CLI agent
in a temporary task workspace and collect its deliverables and trajectory. Run
the judge separately afterward. For the other route, see
[JobBench with Harbor](../harbor/README.md).

## Setup and authentication

Install `uv`, `jq`, `timeout`, and Docker, then run:

```bash
./setup.sh
```

This installs Python dependencies, prepares the Excel calculator, and downloads
both splits from [Hugging Face](https://huggingface.co/datasets/JobBench/job-bench)
to `dataset/<split>/`. Agents run locally; Docker is used for Excel calculation.
For data only, use `JOBBENCH_DATASET_ONLY=1 ./setup.sh`; prepare the calculator
separately with `./eval/setup_judge.sh`.

## Configure an agent

### Claude Code

Install `claude` and log in, then run:

```bash
BENCHMARK_MODELS="claude-sonnet-4-6_cc" ./eval/run_benchmark_claude_code_cli.sh
```

The `_cc` suffix labels outputs; the runner removes it before calling the CLI.

### Codex CLI

Have `codex` or `npx @openai/codex` available and authenticated. The runner
defaults to ChatGPT subscription login:

```bash
BENCHMARK_MODELS="gpt-5.4 gpt-5.3-codex" ./eval/run_benchmark_codex_cli.sh
```

For an OpenAI-compatible endpoint:

```bash
OPENAI_BASE_URL="https://your-endpoint.example/v1" CODEX_API_KEY="your-key" \
  BENCHMARK_MODELS="your-model" ./eval/run_benchmark_codex_cli.sh
```

### OpenCode

Install `git` and `bun`, then run `./setup_opencode.sh`.
[The setup script](../setup_opencode.sh) pins `v1.14.18` via `OPENCODE_COMMIT`
and installs into `./opencode`. Override `OPENCODE_COMMIT` or `OPENCODE_DIR`
to use another revision or location.

Set the selected provider's credentials, such as `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, or `XAI_API_KEY`, or log in interactively:

```bash
bun run --cwd opencode/packages/opencode --conditions=browser src/index.ts auth login
```

Adjust this path if using `OPENCODE_DIR`. Pass space-separated
`model_id|short_name` pairs; the short name labels outputs. Without one, `/` and
`.` in the model ID become `_` and `-` respectively.

```bash
BENCHMARK_MODELS="anthropic/claude-sonnet-4-6|sonnet-4-6 openai/gpt-5.4|gpt-5-4" \
  ./eval/run_benchmark_opencode.sh
```

Declare custom models in `OPENCODE_CONFIG_CONTENT`. Set each model's
`limit.context` and `limit.output` there to control context and output budgets;
reasoning tokens count toward the output limit. The runner fills missing or zero
context limits with `OPENCODE_DEFAULT_CONTEXT` (1,000,000) so auto-compaction works.

## Run tasks

Runners and judge default to the leaderboard split, `main`. Use `SPLIT=easy`
for simplified tasks, setting it for both generation and judging.

| Variable | Default | Purpose |
|---|---|---|
| `SPLIT` | `main` | `main` or `easy` |
| `BENCHMARK_MODELS` | Built-in defaults for Claude/Codex; required for OpenCode | Space-separated model list |
| `TASKS_BASE_DIR` | `<repo_root>/dataset/<SPLIT>` | Task root |
| `RUN_LABEL` | Empty | Suffix for output and trajectory directories |
| `MAX_CONCURRENT_PER_MODEL` | Claude: 2; Codex: 4; OpenCode: 6 | Parallel tasks per model |
| `TIMEOUT_PER_TASK` | Claude/Codex: 3600; OpenCode: 7200 | Seconds per task attempt |

For a subset, copy selected tasks while keeping the `<profession>/taskN/`
layout. Use `TASKS_BASE_DIR` for generation and `TARGET_DIR` for judging:

```bash
TASKS_BASE_DIR=/path/to/my_subset \
  BENCHMARK_MODELS="gpt-5.4" ./eval/run_benchmark_codex_cli.sh
TARGET_DIR=/path/to/my_subset EVAL_MODEL="gpt-5-4" \
  JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

## Judge and results

The default judge is **xAI `grok-4.3`**, separate from the agent's model:

```bash
JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

This scores all available model outputs. Set `EVAL_MODEL` to filter output labels;
the filter matches when either it or the directory label contains the other.
Check the selected directories in the log.

| Variable | Default | Purpose |
|---|---|---|
| `JUDGE_API_KEY` | Required | Judge provider key |
| `JUDGE_MODEL` | `grok-4.3` | Judge model ID |
| `JUDGE_API_BASE` | `https://api.x.ai/v1` | OpenAI-compatible endpoint |
| `EVAL_MODEL` | All outputs | Output label filter |
| `TARGET_DIR` | `<repo_root>/dataset/<SPLIT>` | Task root |
| `MAX_CONCURRENT` | `10` | Rubric concurrency per model output |
| `JUDGE_RUN_LABEL` | Empty | Suffix for a separate set of judge reports |

To use another judge:

```bash
JUDGE_API_BASE="https://your-endpoint.example/v1" \
JUDGE_API_KEY="your-key" JUDGE_MODEL="your-judge-model" \
  uv run ./eval/run_judge.sh
```

Visual rubrics require image input support. Changing the judge may change scores.
See [run_judge.sh](run_judge.sh) for worker, timeout, retry, and multi-judge options.

Results are stored under each task:

```text
dataset/<split>/<profession>/taskN/
├── model_output/<model-label>/      # deliverables
├── model_traj/<model-label>/        # trajectories
└── eval_result/eval_<model-label>/
    └── grok-4-3_judge.json          # rubric results and score
```

Logs are in `eval/logs/`. In each report, `total_score / max_score` is the weighted
score; `pass_rate` is the unweighted percentage of rubrics fully passed. To combine
tasks, divide the sum of `total_score` by the sum of `max_score`.

## Judge evidence

Both evaluation routes share the same extraction code. It reads documents, PDFs,
databases, saved notebook text/rich outputs, and PowerPoint text/tables. Visual
rubrics also receive up to eight extracted raster images. Text is capped at
200,000 characters per file, except SQLite, which uses a row limit.

For XLSX, the judge retains formulas and saved values, and recalculates supported
formulas on temporary copies using pinned LibreOffice
([runtime definition](calc-runtime/Dockerfile)). Unsupported formulas or those
requiring external data are marked as not recalculated; legacy XLS uses saved
values only.
Calculation errors remain visible, and a failed calculator is an evaluation
error rather than a zero score.

The neighboring `*.extraction.json` records the evidence sent to the judge,
formula results, and truncation diagnostics. Inspect extraction without API keys:

```bash
.venv/bin/python eval/judge.py --output-dir /path/to/model_output/run \
  --extract-only --extraction-file /tmp/jobbench-extraction.json
```

## Refreshing and rerunning

Rerun `./setup.sh` to fetch current HF tasks. Existing results are preserved;
replaced local task sources and removed tasks are backed up in `dataset/.setup/`.
Use `--revision <hf-commit>` to pin a source version. Before evaluation, runners
and judge warn if tasks are outdated or freshness cannot be verified, and continue.
Set `JOBBENCH_SKIP_HF_CHECK=1` to skip that check for offline or pinned runs.

Existing nonempty output directories and complete judge reports are skipped,
even after task updates. Use a fresh `RUN_LABEL` to evaluate updated tasks and
filter the judge to that run:

```bash
RUN_LABEL="refresh-20260912" BENCHMARK_MODELS="gpt-5.4" \
  ./eval/run_benchmark_codex_cli.sh
EVAL_MODEL="refresh-20260912" JUDGE_API_KEY="your_xai_key" \
  uv run ./eval/run_judge.sh
```

To rejudge existing deliverables without replacing earlier scores, use a fresh
`JUDGE_RUN_LABEL`:

```bash
EVAL_MODEL="refresh-20260912" JUDGE_RUN_LABEL="rejudge-v2" \
  JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

Reuse labels only to resume the same inputs and judge implementation.
