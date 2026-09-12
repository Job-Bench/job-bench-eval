# Ordinary JobBench evaluation

Run these commands from the repository root. Repository runners copy each task's
working directory into `/tmp`, run the selected CLI agent, and collect final
deliverables and trajectories under that task. The judge scores the deliverables
in a separate step. For the other route, see [JobBench with Harbor](../harbor/README.md).

## Setup and authentication

Install `uv`, `jq`, `timeout`, and Docker, then prepare Python, the judge's
spreadsheet calculator, and both dataset splits:

```bash
./setup.sh
```

Setup builds the pinned calculation image once; later calls reuse Docker's build
cache. To refresh task data without preparing a judge runtime, use
`JOBBENCH_DATASET_ONLY=1 ./setup.sh`. Prepare just the calculator with
`./eval/setup_judge.sh`. Ordinary agent runners still run as before; only Excel
formula calculation uses this container.

Setup uses the raw task files from
[`JobBench/job-bench`](https://huggingface.co/datasets/JobBench/job-bench).
Each task lives at `dataset/<split>/<profession>/taskN/`, with source materials in
`task_folder/`, a weighted `RUBRICS.json`, and a human-readable `task_card.md`.
The agent sees the task materials, not the rubric or task card. Main tasks may
also have withheld search references in `files_required_to_search/`.

Both runners and judge default to `SPLIT=main`, the leaderboard split. Set
`SPLIT=easy` explicitly for simplified tasks.

### OpenCode (reference agent)

Install `git` and `bun`, then run:

```bash
./setup_opencode.sh
```

The official reference is OpenCode **v1.14.18**, commit
`23fb5e0516c99ac04a1aa46c193efda2e1b9bb24`. The pin is the `OPENCODE_COMMIT`
default in [setup_opencode.sh](../setup_opencode.sh). The default checkout is
`./opencode`; set `OPENCODE_DIR` to use another location or `OPENCODE_COMMIT`
to test another revision.

Configure provider credentials using environment variables such as
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or `XAI_API_KEY`, or use interactive login
for OAuth or saved API keys:

```bash
bun run --cwd opencode/packages/opencode --conditions=browser src/index.ts auth login
bun run --cwd opencode/packages/opencode --conditions=browser src/index.ts auth list
```

Adjust the path above if you set `OPENCODE_DIR`. Credentials must match the
provider in the selected model ID.

Pass space-separated `model_id|short_name` pairs. The short name labels the
output directories; omitting it uses the model ID with `/` replaced by `_`
and `.` replaced by `-`.

```bash
BENCHMARK_MODELS="anthropic/claude-sonnet-4-6|sonnet-4-6 openai/gpt-5.4|gpt-5-4" \
  ./eval/run_benchmark_opencode.sh
```

Models outside OpenCode's catalog must be declared in `OPENCODE_CONFIG_CONTENT`.
Give them a suitable `limit.context`: zero disables auto-compaction, so the
runner fills missing or zero values with `OPENCODE_DEFAULT_CONTEXT` (1,000,000).
Leave `limit.output` unset unless you know the required budget; reasoning tokens
also count toward that limit.

The source pin identifies the base revision. Record local provider/model patches
separately with your run results:

```bash
git -C "${OPENCODE_DIR:-./opencode}" rev-parse HEAD
git -C "${OPENCODE_DIR:-./opencode}" describe --tags --exact-match
git -C "${OPENCODE_DIR:-./opencode}" status --short
git -C "${OPENCODE_DIR:-./opencode}" diff
```

### Claude Code

Install `claude` and log in, then run:

```bash
BENCHMARK_MODELS="claude-sonnet-4-6_cc" ./eval/run_benchmark_claude_code_cli.sh
```

The `_cc` suffix is an output label; the runner strips it before calling the CLI.

### Codex CLI

Have `codex` or `npx @openai/codex` available and authenticated. The runner
defaults to ChatGPT subscription login:

```bash
BENCHMARK_MODELS="gpt-5.4 gpt-5.3-codex" ./eval/run_benchmark_codex_cli.sh
```

To use an OpenAI-compatible endpoint:

```bash
export OPENAI_BASE_URL="https://your-endpoint.example/v1"
export CODEX_API_KEY="sk-your-key"
BENCHMARK_MODELS="your-model" ./eval/run_benchmark_codex_cli.sh
```

## Runner options and subsets

| Variable | Default | Purpose |
|---|---|---|
| `SPLIT` | `main` | `main` or `easy` |
| `TASKS_BASE_DIR` | `<repo_root>/dataset/<SPLIT>` | Task root, including a custom subset |
| `BENCHMARK_MODELS` | Built-in defaults for Claude/Codex; required for OpenCode | Space-separated model list |
| `RUN_LABEL` | Empty | Suffix appended to output and trajectory directory names |
| `MAX_CONCURRENT_PER_MODEL` | Runner default | Parallel tasks per model |
| `TIMEOUT_PER_TASK` | Runner default | Wall-clock limit per task, in seconds |

For a subset, copy selected tasks to another directory while keeping the
`<profession>/taskN/` layout. Set `TASKS_BASE_DIR` for generation and `TARGET_DIR`
for judging:

```bash
TASKS_BASE_DIR=/path/to/my_subset \
  BENCHMARK_MODELS="openai/gpt-5.4|gpt-5-4" ./eval/run_benchmark_opencode.sh
TARGET_DIR=/path/to/my_subset EVAL_MODEL="gpt-5-4" \
  JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

To use the easy split:

```bash
SPLIT=easy BENCHMARK_MODELS="openai/gpt-5.4|gpt-5-4" ./eval/run_benchmark_opencode.sh
SPLIT=easy EVAL_MODEL="gpt-5-4" JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

## Running the judge

The judge extracts text from deliverables such as spreadsheets, documents,
PDFs, notebooks, and databases, then sends one chat completion per rubric.
Extracted text is capped at 200,000 characters per file, except SQLite, which
uses its existing row limit. Rubrics mentioning
visual features such as plots or figures also attach images from the output
directory, so those rubrics need a judge that accepts multimodal input.

The default is **xAI `grok-4.3`** at `https://api.x.ai/v1`:

```bash
JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

By default, the judge scores all available model output directories. Filter to
one output label with `EVAL_MODEL`:

```bash
JUDGE_API_KEY="your_xai_key" EVAL_MODEL="gpt-5-4" uv run ./eval/run_judge.sh
```

Use a different OpenAI-compatible endpoint with:

```bash
JUDGE_API_BASE="https://your-endpoint.example/v1" \
JUDGE_API_KEY="your-key" JUDGE_MODEL="your-judge-model" \
  uv run ./eval/run_judge.sh
```

Other judge models have not been validated against these rubrics and may produce
different scores.

| Variable | Default | Purpose |
|---|---|---|
| `JUDGE_MODEL` | `grok-4.3` | Judge model ID |
| `JUDGE_API_BASE` | `https://api.x.ai/v1` | OpenAI-compatible endpoint |
| `JUDGE_API_KEY` | Required | Judge provider key |
| `EVAL_MODEL` | All outputs | Match a model output directory label |
| `SPLIT` | `main` | Split to judge |
| `TARGET_DIR` | `<repo_root>/dataset/<SPLIT>` | Task root, including a custom subset |
| `MAX_CONCURRENT` | `10` | Default rubric concurrency |

See [run_judge.sh](run_judge.sh) for worker, timeout, retry, and multi-judge
settings. `EVAL_MODEL` matches when either the filter or directory label contains
the other. A full output label can also select a shorter base label or a longer
label; check the selected directories in the judge log.

## Judge evidence

Notebook extraction reads saved sources, streams, errors, and text, Markdown,
HTML/table, LaTeX and JSON outputs without executing cells. PowerPoint extraction
includes grouped text and table grids, preserving merged-cell origins. Image
handling is unchanged: matching visual rubrics receive up to eight deduplicated
standalone, DOCX or notebook-output raster images.

For XLSX, the judge preserves cell positions, formulas and saved caches, and
explicitly recalculates supported formulas with LibreOffice **24.2.7.2**, Ubuntu
package `4:24.2.7-0ubuntu0.24.04.6`. The base images and the Ubuntu package snapshot
are pinned in [calc-runtime/Dockerfile](calc-runtime/Dockerfile). Formula caches
may be absent, stale, or placeholder zeros, so calculation is not limited to
empty caches. Original submissions are never saved or modified by the calculator.

The calculator has no model credentials, disables networking and macros, and
does not refresh external links. Workbooks with external links, volatile or
environment-dependent functions, macros, or unsupported formula structures
(including array/data-table formulas) retain
their saved evidence with an explicit "not recalculated" warning. Legacy XLS
provides saved values only. Calculation errors remain visible beside the formula;
an unavailable or timed-out runtime is an evaluation error, not a zero score.

Each new details report has a neighboring `*.extraction.json` containing the text
sent to the judge, full Excel formula/cache/calculation records, input hashes,
implementation identity, and truncation diagnostics. Large workbooks use compact
value grids and exact formula ranges, with space shared across sheets and omitted
rows explicitly marked. Inspect evidence without a
model or judge key:

```bash
.venv/bin/python eval/judge.py --output-dir /path/to/model_output/run \
  --extract-only --extraction-file /tmp/jobbench-extraction.json
```

Changing the extractor can change scores. Existing complete reports are preserved
and warn if their implementation differs; incompatible partial reports cannot be
resumed. Use a fresh `JUDGE_RUN_LABEL` to score existing deliverables again while
keeping earlier scores:

```bash
EVAL_MODEL="gpt-5-4" JUDGE_RUN_LABEL="extraction-v2" \
  JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

This creates `grok-4-3_extraction-v2_judge.json` beside the original report. Reuse
the label only to resume the same inputs and judge implementation.

## Refreshing and rerunning

Each runner and the judge check the recorded HF revision once before evaluation.
If HF `main` differs, a warning shows both revisions and suggests `./setup.sh`.
An unavailable network or missing version record produces an "unable to verify"
warning. Evaluation continues in all cases. Custom task roots need their own
setup record to be checked; they are not assumed to match the default dataset.
Set `JOBBENCH_SKIP_HF_CHECK=1` to skip this advisory for an offline or deliberately
pinned run.

Rerun `./setup.sh` to resolve the latest Hugging Face `main` commit and refresh
both splits. Setup checks upstream every time and reuses unchanged cached
content. It stages and validates both splits before syncing managed task
sources, backs up overwritten local edits, and preserves model outputs,
trajectories, judge results, and local extra files. Tasks removed from Hugging
Face are archived outside the active splits with their history.

To select a source version, use `./setup.sh --revision <hf-commit>`. Use
`--repo-id organization/dataset` for a compatible repository with raw
`dataset/<profession>/taskN/` and `dataset_easy/<profession>/taskN/` layouts.
`dataset/.setup/state.json` records the source revision and managed files;
`dataset/.setup/backups/` holds saved local edits and retired task directories.

On the first refresh of an existing checkout, setup adopts `RUBRICS.json`,
`task_card.md`, `task_folder/`, and `files_required_to_search/` as managed sources.
Local differences there, including files absent from Hugging Face, are backed up
before replacement or removal. Later refreshes use the manifest to distinguish
managed files from newly added local extras.

Refreshing sources does not rerun the agents or judge. Runners skip tasks whose
model output directory is already nonempty, and the judge skips reports with
the expected rubric count. These checks do not detect changed task inputs or
rubrics. Use a new, unique `RUN_LABEL` to evaluate a new dataset version while
retaining the previous run, then use that run label as the judge filter:

```bash
RUN_LABEL="refresh-20260911" BENCHMARK_MODELS="openai/gpt-5.4|gpt-5-4" \
  ./eval/run_benchmark_opencode.sh
EVAL_MODEL="refresh-20260911" JUDGE_API_KEY="your_xai_key" \
  uv run ./eval/run_judge.sh
```

This filter can select several models if they share the run label. Choose a
fresh label for each new evaluation. Reuse the same label to resume an
interrupted run against the same source version.

## Output layout and scores

```text
dataset/<split>/<profession>/taskN/
├── RUBRICS.json
├── task_card.md
├── task_folder/
├── files_required_to_search/        # main split, where present
├── model_output/<model-label>/      # final deliverables
├── model_traj/<model-label>/        # structured trajectories
└── eval_result/eval_<model-label>/
    └── grok-4-3_judge.json          # detailed rubric results and score
```

Runner and judge logs live in `eval/logs/`. Judge report filenames use the
judge model name with `.` replaced by `-` and any provider prefix removed.
The JSON contains evidence and per-rubric results alongside these fields:

```json
{
  "evaluated_model": "gpt-5-4",
  "judge_model": "grok-4.3",
  "total_score": 18,
  "max_score": 25,
  "pass_rate": "66%",
  "passed_count": 6,
  "total_count": 9
}
```

`total_score / max_score` is the weighted score. `pass_rate` is the unweighted
percentage of rubrics fully passed.
