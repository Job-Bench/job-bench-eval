# JobBench

JobBench evaluates agentic CLI tools (Claude Code, Codex CLI, OpenCode) on the tedious, multi-source pre-processing that experienced professionals most want offloaded: reconciling contradictory data, cross-referencing records, tracing citations. Tasks are sourced from Workbank, a worker-desire survey across 35 white-collar occupations, shifting the question from *what can be automated* to *what workers actually want automated*. Each task ships with a working directory and a weighted rubric; after the agent finishes, an LLM judge scores the deliverables against that rubric.

- 🌐 [Project Website](https://job-bench.github.io/) - Learn more about JobBench
- 🔧 [Github Repo](https://github.com/Job-Bench/job-bench-eval/) - Access the eval scirpt of JobBench
- 🤗 HF Datasets - Find all JobBench datasets
  - [JobBench (Main)](https://huggingface.co/datasets/JobBench/job-bench)

## What's inside

- **Two splits** across the same 35 professions (biostatistician, lawyer, mechanical engineer, reporter, supply chain manager, web admin, …):
  - **`main`** — 65 full tasks, including ground-truth materials the agent must discover via search.
  - **`easy`** — 63 simplified tasks (shorter prompts, no `files_required_to_search/`). Useful for cheaper smoke tests.
- **Dataset**: hosted on Hugging Face at [`JobBench/job-bench`](https://huggingface.co/datasets/JobBench/job-bench); `./setup.sh` pulls both splits into `dataset/main/` and `dataset/easy/`.
- Each task lives at `dataset/<split>/<profession>/taskN/` and contains:
  - `task_folder/` — the working directory the agent operates in
  - `RUBRICS.json` — weighted pass/fail criteria used for scoring
  - `task_card.md` — human-readable task brief (sourced from ONET; not seen by the agent)
- **Supported agents**: Claude Code, Codex CLI, OpenCode
- **Judge**: any OpenAI-compatible endpoint; default is `grok-4-1-fast` on xAI. Measured score deviation vs. `claude-opus-4.5` stays within 1%, while a full judge pass costs ~$2 vs. ~$60 — a ~30× cost saving.

## Prerequisites

System tools:

- `uv`, `jq`, `timeout`

Per-agent authentication:

- **Claude Code** — `claude` installed and logged in
- **Codex CLI** — `codex` (or `npx @openai/codex`) works in your shell; ChatGPT subscription login is fine, or set `CODEX_API_KEY` + `OPENAI_BASE_URL` for any OpenAI-compatible endpoint
- **OpenCode** — install `bun` (`curl -fsSL https://bun.sh/install | bash`, then open a new shell), then run `./setup_opencode.sh` from the repo root. This clones OpenCode into `<repo_root>/opencode`, pinned to `v1.14.18`.

  Then configure provider credentials one of two ways:

  - **Env vars** — e.g. `XAI_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`. Works for any provider with an `env` field in OpenCode's catalog; easiest for CI.
  - **Interactive login** — OAuth (ChatGPT subscription) or saved API keys:

    ```bash
    bun run --cwd opencode/packages/opencode --conditions=browser src/index.ts auth login
    bun run --cwd opencode/packages/opencode --conditions=browser src/index.ts auth list  # verify
    ```

  Without credentials for the chosen provider, every task fails with `ProviderModelNotFoundError`.

## Quick Start

```bash
# 1. Clone and enter the repo
git clone https://github.com/Job-Bench/job-bench-eval.git
cd job-bench-eval

# 2. Install Python deps + pull both splits from Hugging Face
#    (lands at dataset/main/ and dataset/easy/)
./setup.sh

# 3. Run one model through the main split (default; pass SPLIT=easy for the easy set)
BENCHMARK_MODELS="gpt-5.4" ./eval/run_benchmark_codex_cli.sh

# 4. Score the outputs with the default Grok judge (also defaults to main)
JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

To try just one slice first, point `TASKS_BASE_DIR` at a subset (see [Scoping a run](#scoping-a-run)).

## Running benchmarks

All runner scripts are invoked from the repo root. They copy each task into `/tmp`, let the agent work, then write only the final deliverables back to the dataset tree. Runs are resumable — tasks that already have output for a given model are skipped.

### Claude Code

```bash
BENCHMARK_MODELS="claude-sonnet-4-6_cc" ./eval/run_benchmark_claude_code_cli.sh
```

The `_cc` suffix is a runner label; it is stripped before calling the `claude` CLI, so the underlying model is `claude-sonnet-4-6`.

### Codex CLI

```bash
BENCHMARK_MODELS="gpt-5.4 gpt-5.3-codex" ./eval/run_benchmark_codex_cli.sh
```

Defaults to ChatGPT subscription login. For any OpenAI-compatible endpoint:

```bash
export OPENAI_BASE_URL="https://your-endpoint.example/v1"
export CODEX_API_KEY="sk-your-key"
./eval/run_benchmark_codex_cli.sh
```

### OpenCode

Pass models as `model_id|short_name` pairs, space-separated. `model_id` is anything OpenCode accepts; `short_name` labels the output directory. Drop `|short_name` to default to a sanitized form of the ID.

```bash
BENCHMARK_MODELS="anthropic/claude-sonnet-4-6|sonnet-4-6 openai/gpt-5.4|gpt-5-4" \
  ./eval/run_benchmark_opencode.sh
```

### Common environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SPLIT` | `main` | Which split to run: `main` or `easy` |
| `TASKS_BASE_DIR` | `<repo_root>/dataset/<SPLIT>` | Dataset root; override to point at a custom subset |
| `BENCHMARK_MODELS` | built-in defaults (CC/Codex); required for OpenCode | Space-separated model list, same variable across all three runners |
| `RUN_LABEL` | empty | Suffix appended to output directory names |
| `MAX_CONCURRENT_PER_MODEL` | runner default | Parallel tasks per model |
| `TIMEOUT_PER_TASK` | runner default | Wall-clock cap per task |

To run the easy split:

```bash
SPLIT=easy BENCHMARK_MODELS="gpt-5.4" ./eval/run_benchmark_codex_cli.sh
SPLIT=easy JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

### Scoping a run

To benchmark only a subset, copy the tasks you care about into another directory (keep the `profession/taskN/` layout) and point `TASKS_BASE_DIR` at it:

```bash
TASKS_BASE_DIR=/path/to/my_subset \
BENCHMARK_MODELS="claude-sonnet-4-6_cc" \
./eval/run_benchmark_claude_code_cli.sh
```

## Running the judge

The judge reads `model_output/` directories and scores them against each task's `RUBRICS.json`.

Running a full agent-as-judge loop (an agent calling tools to inspect outputs) would cost hundreds of dollars per evaluation pass. JobBench avoids that: it extracts text from each deliverable (xlsx/docx/pdf/ipynb/db/…) upfront and sends one chat completion per rubric — most rubrics judge purely on that text, which is what keeps cost low. A minority of rubrics that reference plots/figures auto-attach images on top (see below). Each file is capped at 200K chars; the cap only kicks in when a model mistakenly dumps a multi-MB raw input file into its output, and does not affect rubric correctness.

Default setup (xAI / Grok):

```bash
JUDGE_API_KEY="your_xai_key" uv run ./eval/run_judge.sh
```

Score only one model's outputs:

```bash
JUDGE_API_KEY="your_xai_key" \
EVAL_MODEL="claude-sonnet-4-6_cc" \
uv run ./eval/run_judge.sh
```

Use a different OpenAI-compatible endpoint:

```bash
JUDGE_API_BASE="https://api.openai.com/v1" \
JUDGE_API_KEY="sk-..." \
JUDGE_MODEL="your-judge-model" \
uv run ./eval/run_judge.sh
```

> Grok `grok-4-1-fast` is the validated judge for JobBench. Measured score deviation vs. `claude-opus-4.5` is within 1%, while costing ~$2 per full judge pass vs. ~$60 — a ~30× cost saving. Other models have not been validated against these rubrics — use them only for exploration.

Vision-capable rubrics (those mentioning `plot`, `figure`, `visualization`, `Q-Q`, etc.) automatically attach images from the evaluated model's output directory to the judge prompt. This requires the judge model to accept multimodal input. Rubrics without visual keywords run text-only so the extra image tokens aren't spent.

Commonly tuned variables (see `eval/run_judge.sh` for the full list):

| Variable | Default | Purpose |
|---|---|---|
| `JUDGE_MODEL` | `grok-4-1-fast` | Judge model id |
| `JUDGE_API_BASE` | `https://api.x.ai/v1` | OpenAI-compatible endpoint |
| `JUDGE_API_KEY` | (required) | Key for the endpoint above |
| `EVAL_MODEL` | all | Only score this model's outputs |
| `SPLIT` | `main` | Which split to judge: `main` or `easy` |
| `TARGET_DIR` | `<repo_root>/dataset/<SPLIT>` | Dataset root; override to judge a custom subset |
| `MAX_CONCURRENT` | 10 | Rubric-level parallelism |

## Output layout

After a run, each task directory is populated like this:

```
dataset/<split>/<profession>/taskN/    # <split> is "main" or "easy"
├── RUBRICS.json
├── task_card.md                       # human-readable task brief
├── task_folder/                       # source task materials
├── files_required_to_search/          # main split only — search-discoverable refs
├── model_output/<model>/              # agent's final deliverables
├── model_traj/<model>/                # structured trace from the runner
└── eval_result/eval_<model>/
    └── <judge_model>_judge.json       # detailed judge report + score
```

The judge JSON contains per-rubric pass/fail, evidence snippets, and aggregate scores. The top-level fields you usually want:

```json
{
  "evaluated_model": "claude-sonnet-4-6_cc",
  "judge_model": "grok-4-1-fast",
  "total_score": 18,
  "max_score": 25,
  "pass_rate": "68%",
  "passed_count": 6,
  "total_count": 9,
  "rubrics": [ ... ]
}
```

`pass_rate` is the percentage of rubrics fully passed. `total_score / max_score` is the weighted score, since each rubric has a `weight`.

## Notes

- Some tasks require live web search. Agents without browsing will score lower on those.
- The dataset is fetched from Hugging Face — `./setup.sh` skips the download if `dataset/` is already populated. Set `FORCE=1` to wipe and re-download.
