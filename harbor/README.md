# JobBench with Harbor

Harbor runs agents in isolated environments and scores their deliverables with
the shared JobBench judge. Tasks, dependencies, and results live under `harbor/`,
independently of [ordinary evaluation](../eval/README.md).
Run all examples from the repository root.

## Setup

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and have
Docker available, then run:

```bash
./harbor/setup.sh
```

Setup creates `harbor/.venv` with locked Harbor **0.22.0** and generates
**65 main + 63 easy** tasks from `JobBench/job-bench` on Hugging Face under
`harbor/tasks/`. Python >=3.12 is required; uv provisions the environment.
No model or judge credentials are needed for setup.

## Agent configuration

Use Harbor's native agent authentication and model configuration. Set maximum
output tokens and context limits through the selected agent/provider's supported
settings; parameter names vary. Agent kwargs can be passed with `--ak` or in a
YAML configuration. [`configs/example.yaml`](configs/example.yaml) shows
Terminus-2 turn and output limits. Task timeouts are separate; use Harbor's timeout
multiplier options to adjust them.

For OpenCode, the optional [`configs/opencode.yaml`](configs/opencode.yaml)
example pins **v1.14.18**, matching [`setup_opencode.sh`](../setup_opencode.sh).
Select it with `--config harbor/configs/opencode.yaml`, or use
`-a opencode --ak version=1.14.18`.

Custom agents implement Harbor's `BaseAgent` or `BaseInstalledAgent` interface.
Use an importable class without changing JobBench tasks or adding a runner:

```bash
PYTHONPATH=/absolute/path/to/agent-package \
  ./harbor/eval.sh --split main -a my_package.agent:MyAgent
```

Install any host-side adapter dependencies into the Harbor environment:

```bash
uv pip install --python harbor/.venv/bin/python -e /absolute/path/to/agent-package
```

Setup preserves these optional packages. The adapter must support Harbor 0.22.0
and install the tools its agent needs inside the container.

## Run evaluation

Choose an agent with `-a` and a model with `-m`. Supply its provider credentials
and a separate judge key. For example, using Terminus-2:

```bash
export OPENAI_API_KEY="your_model_provider_key"
export JUDGE_API_KEY="your_xai_key"
./harbor/eval.sh --split main -a terminus-2 -m openai/gpt-5.4
```

Each trial runs the agent, then the judge. The judge defaults to `grok-4.3` at
`https://api.x.ai/v1`; use `JUDGE_MODEL` and `JUDGE_API_BASE` for another
OpenAI-compatible judge, which may yield different scores. Keep credentials in
environment variables rather than literal YAML or CLI values.

| Option | Purpose |
| --- | --- |
| `--split main`, `easy`, or `all` | Choose tasks; `main` is the default leaderboard split. |
| `-n 4` | Run four concurrent trials. |
| `--ak max_turns=100` | Pass an agent-specific setting. |
| `-i 'main--biostatisticians--*'` | Filter task IDs by glob. |
| `--config harbor/configs/example.yaml` | Load a native Harbor YAML configuration. |
| `--dry-run` | Inspect the selected tasks and command without running evaluation. |
| `--print-config` | Print Harbor's resolved configuration and exit; values may include secrets. |

For example, inspect a subset before running it:

```bash
./harbor/eval.sh --split main -i 'main--biostatisticians--*' \
  -a terminus-2 -m openai/gpt-5.4 -n 4 --dry-run
```

Remove `--dry-run` to start. When overriding a model in a configuration with
multiple agents, also select the intended agent with `-a`. Harbor may ask before
passing host environment variables to tasks; use its native `-y` option for
unattended runs. See `./harbor/eval.sh --help` and
`harbor/.venv/bin/harbor run --help` for more options.

### Inputs and deliverables

Agents start in `/workspace`, read inputs from `/workspace/task_folder`, and
submit final files in `/workspace/output`. Only final output is collected and
scored; if a task requires editing an input workbook or database, save the updated
copy there too. Rubrics, task cards, and hidden search references are withheld.

Both splits allow public network access, and some tasks require online research.
The image includes scientific Python, Office/PDF processing, OCR, SQLite, STL,
and geospatial tools. Search/browser and vision capabilities depend on the agent;
external sites or APIs may require their own authentication.

## Scoring and results

Both evaluation routes use the same judge and artifact extraction logic. Harbor
runs verification in a separate image and gives judge credentials only to the
verifier. The weighted reward is:

```text
reward = round(sum(passed rubric weights) / sum(all rubric weights), 4)
```

This differs from the unweighted rubric pass rate. Jobs and collected deliverables
are saved under `harbor/jobs/`; each trial retains `reward.json`, detailed judge
results, and diagnostics in its verifier logs. See
[judge evidence](../eval/README.md#judge-evidence) for supported formats and limits.

Empty submissions receive zero without a judge call. Configuration, API, parsing,
and unsafe-artifact failures produce failed verification without a valid reward.
**Harbor 0.22.0 can exit successfully even when trials fail**: check the job/trial
`result.json` exception records and verifier logs before treating a run as complete.

Saved jobs include the resolved configuration and source provenance. Live websites,
model providers, and agent settings can still change outcomes.

## Refreshing and rerunning

Rerun `./harbor/setup.sh` after HF data or this integration changes. Setup checks
for the latest source, refreshes tasks, and preserves existing jobs. Evaluation
warns about newer HF data or an unavailable freshness check, then continues;
use `JOBBENCH_SKIP_HF_CHECK=1` for offline or deliberately pinned runs.

To pin a source version, run `./harbor/setup.sh --revision "<hf-commit>"`.
Use `--repo-id organization/dataset` for another compatible repository containing
both raw `dataset/<occupation>/taskN/` and `dataset_easy/<occupation>/taskN/` layouts.

Run evaluation again as a new Harbor job to score refreshed tasks. Edit HF sources
or the converter, then rerun setup; do not edit generated tasks directly. Keep
referenced directories in `harbor/.generated/` while jobs run or for saved-job replay.
