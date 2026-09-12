# JobBench with Harbor

Harbor runs JobBench agents in isolated environments and scores their
deliverables with the shared JobBench judge. It manages its own tasks, Python
environment, and results under `harbor/`. For the other route, see
[ordinary evaluation](../eval/README.md). Run all examples from the repository root.

## Prepare tasks

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and have
Docker available for evaluation. From the repository root, run:

```bash
./harbor/setup.sh
```

Setup creates a separate `harbor/.venv` with the locked Harbor **0.22.0** and
downloads source files from `JobBench/job-bench` on Hugging Face. It materializes
all tasks beneath `harbor/tasks/`: currently **65 main + 63 easy** tasks. No model
or judge credentials are needed to prepare the public dataset. Python >=3.12 is
required; uv provisions the project environment.

Rerun the same command to check the latest Hugging Face `main` revision. Every
invocation resolves the branch to a commit and checks the generated files;
unchanged downloads use the cache and unchanged sources reuse the existing
generation. Added, edited, and deleted tasks become active together only after
both splits are generated and validated. Existing jobs and generations remain,
and refreshing tasks does not rerun evaluation.

To reproduce a source version:

```bash
./harbor/setup.sh --revision "<hf-commit>"
```

Use `--repo-id organization/dataset` for a compatible HF repository. Both splits
must use the raw `dataset/<occupation>/taskN/` and
`dataset_easy/<occupation>/taskN/` layouts.

The importer reads raw instructions, rubrics, task cards, and starter files.
Parquet tables are derived exports; task generation uses the raw source files.

## Evaluate

Before evaluation (including `--dry-run`), the wrapper compares the selected
generation's recorded source revision with HF `main`. An available update
produces a warning with `./harbor/setup.sh`; a failed check is reported as
unable to verify. Both are advisory and evaluation continues. Set
`JOBBENCH_SKIP_HF_CHECK=1` for an offline or deliberately pinned run.

Choose a native or custom agent with `-a` and a model with `-m`. Provide their
credentials and the separate JobBench judge key. For example, using Terminus-2:

```bash
export OPENAI_API_KEY="your_model_provider_key"
export JUDGE_API_KEY="your_xai_key"
./harbor/eval.sh --split main \
  -a terminus-2 -m openai/gpt-5.4
```

The judge defaults to `grok-4.3` at `https://api.x.ai/v1`. Configure an alternative
OpenAI-compatible judge with `JUDGE_MODEL` and `JUDGE_API_BASE`. Alternative judges
may yield different scores. Agent authentication uses the selected Harbor
agent's normal configuration; the wrapper does not contain agent-specific runners.

```bash
# Inspect the generation and invocation without running an agent or judge.
./harbor/eval.sh --split easy --dry-run \
  -a terminus-2 -m openai/gpt-5.4

# Use native Harbor options, such as concurrency and agent kwargs.
./harbor/eval.sh --split main -a terminus-2 -m your-provider/your-model \
  -n 2 --ak max_turns=100

# Load a native Harbor YAML configuration.
./harbor/eval.sh --split main --config harbor/configs/example.yaml

# Select a task or occupation before launching a larger run.
./harbor/eval.sh --split main -i 'main--biostatisticians--*' \
  -a terminus-2 -m openai/gpt-5.4
```

The default split is `main`, the leaderboard split. Use `--split easy` for
simplified tasks or `--split all` for both. Splits have separate task IDs;
same-numbered slots can contain different tasks. Use `./harbor/eval.sh --help`
and `harbor/.venv/bin/harbor run --help` for the pinned version's options.

Use `--print-config` to ask the pinned Harbor CLI to resolve the actual native
configuration and exit before running a task. Unlike the concise `--dry-run`,
this prints config values, so avoid literal secrets in configuration. A model
override (`-m`) preserves the kwargs of a single configured agent; with several
configured agents, select the intended agent explicitly using `-a`.

Harbor may request confirmation before a task receives host environment
variables. Its native `-y` option is available for intentionally unattended runs.
Only pass credentials to the roles that need them.

## Agent settings and custom agents

Agent/model settings belong in the native job configuration or `--ak` options,
not in the generated tasks. `configs/example.yaml` shows a Terminus-2 output
limit and turn limit. Parameter names are agent/provider-specific: a CLI agent
may use a native config file, and an agent may not expose every model parameter.
Task timeouts are separate; use Harbor's timeout multiplier options to adjust
the generated default execution budget.

For OpenCode, [`configs/opencode.yaml`](configs/opencode.yaml) pins `v1.14.18`,
matching [`setup_opencode.sh`](../setup_opencode.sh):

```bash
./harbor/eval.sh --split main --config harbor/configs/opencode.yaml
```

To select the same agent and version on the CLI, use `-a opencode --ak version=1.14.18`.

Custom agents implement Harbor's `BaseAgent` or `BaseInstalledAgent` interface.
Point the same wrapper at an importable class:

```bash
PYTHONPATH=/absolute/path/to/agent-package \
  ./harbor/eval.sh --split main -a my_package.agent:MyAgent
```

If the custom adapter has host-side dependencies, install it into the isolated
environment:

```bash
uv pip install --python harbor/.venv/bin/python -e /absolute/path/to/agent-package
```

Setup uses `uv sync --locked --inexact` so refreshing tasks retains these optional
packages while reconciling this integration's declared dependencies. The custom
adapter is responsible for compatibility with the pinned Harbor version and for
installing any tools its agent needs inside the container. No JobBench task
changes are required to add an agent.

## Inputs, search, and outputs

The agent starts in `/workspace`, with the original files under
`/workspace/task_folder` and final deliverables under `/workspace/output`.
Instructions preserve the original task text and the existing runner's input,
output, and online-search contract.

Both splits allow public network access. Of the current main tasks, 57 have
search-reference folders; easy tasks may also require external information.
Those search files are recorded as withheld metadata and are not copied into the
agent image. Rubrics and task cards also stay outside the agent environment.
Task cards often disclose answers. Historical model outputs are never imported.

The agent image includes scientific Python, Office/PDF processing, OCR, SQLite,
STL, and geospatial tools. Actual search/browser tools and vision capability
still depend on the chosen agent. Public-network access makes source retrieval
possible; it does not ensure every website, Kaggle dataset, or external API is
available without additional authentication.

Some tasks require modifying an input database or workbook, including the court
clerk, emergency dispatch, and sociology gradebook tasks. Submit the updated
database/workbook in `/workspace/output`, alongside other requested deliverables.
Only final output is collected and scored; input copies and scratch files are
not automatically added to the submission.

## Scoring and records

Each task has a separate verifier image containing a byte-identical copy of the
repository's `eval/judge.py`, the shared `eval/jobbench_eval` extraction helpers,
its rubric, and a small wrapper. The image includes the same pinned LibreOffice
runtime as ordinary evaluation; the calculation subprocess disables networking
and receives no judge credentials. Judge credentials are passed to the verifier
only. Both routes use the same extraction logic, and the weighted scoring formula
remains:

```text
reward = round(sum(passed rubric weights) / sum(all rubric weights), 4)
```

The result is a continuous weighted score, distinct from the unweighted rubric
pass rate. Reward is written to `/logs/verifier/reward.json`; detailed judgment
and diagnostic logs are retained with the Harbor trial. An empty submission
receives zero without calling the judge. Configuration, API, parsing, and unsafe
artifact errors are reported as failed verification without a valid reward,
so they can be distinguished from a legitimately unsuccessful submission.
`judge-details.extraction.json` retains extracted text, input hashes, formula
evidence and calculation/truncation diagnostics. See
[judge evidence](../eval/README.md#judge-evidence) for supported formats and limits.

Jobs and collected deliverables default to `harbor/jobs/`. Each evaluation pins
one immutable generated task directory and records its source revision, judge
hash and selected tasks in `jobbench_provenance.json`. Harbor records its own resolved run configuration and
agent metadata. Keep credentials in environment variables, not literal YAML or
CLI values that upstream tools may record.

The wrapper preserves Harbor's exit status. Harbor 0.22.0 can exit successfully
even when individual trials fail: inspect the job/trial `result.json` exception
records and verifier diagnostics when deciding whether a run completed correctly.

Setup publishes `harbor/current` atomically; `harbor/tasks` and
`harbor/manifest.json` point through it. Old snapshots remain in
`harbor/.generated/` so updating HF does not change tasks used by an evaluation
already in progress. They are not automatically pruned. Preserve referenced
generations while jobs are running or when replaying a saved job configuration.
Generated files are managed artifacts: edit HF sources or the adapter/templates,
then rerun setup, rather than editing `harbor/tasks/` in place.
Helper and calculator-runtime changes also produce a new generation even when
HF task sources have not changed. Run `./harbor/setup.sh` after updating this code;
the generated verifier builds its own calculation environment, so the ordinary
route's calculator setup is not required for Harbor.

Source and judge pins make the task package traceable. Live websites, model
providers and different agents' execution settings can still change outcomes.
The conversion does not include gold solutions, so the Oracle agent is not an
available correctness baseline.
