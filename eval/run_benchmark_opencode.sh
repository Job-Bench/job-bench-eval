#!/bin/bash

# JobBench Benchmark Runner
# Runs jobbench_dataset tests in OpenCode headless mode
# For each model, a full independent task folder is copied to /tmp; the model works there
# When the task finishes, output is moved to model_output/{model_name}/ and the temp dir is deleted

set -e

# ============================================================
# ===== Configuration (edit here) =====
# ============================================================

# Which split of the dataset to run against (main | easy). Defaults to main.
SPLIT="${SPLIT:-main}"
# Target task root (the directory containing all profession subdirectories)
TASKS_BASE_DIR="${TASKS_BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/dataset/${SPLIT}}"
# Model list -- supplied via BENCHMARK_MODELS env var (space-separated entries)
# Each entry has the form "model_id|short_name", e.g.
#   BENCHMARK_MODELS="anthropic/claude-sonnet-4-5|sonnet-4-5 openai/gpt-5.2|gpt-5-2"
# model_id is whatever OpenCode expects (depends on the providers configured in your OpenCode auth).
# short_name is used in output/trajectory directory names; if the "|short_name" is omitted,
# model_id itself is used (with "/" and "." sanitized).
# For backwards compatibility, MODEL_SPECS is still accepted.
if [[ -n "${BENCHMARK_MODELS:-}" ]]; then
    read -r -a MODELS <<< "$BENCHMARK_MODELS"
elif [[ -n "${MODEL_SPECS:-}" ]]; then
    read -r -a MODELS <<< "$MODEL_SPECS"
else
    MODELS=()
fi

# Optional run label appended to output directory names to avoid clobbering past runs
RUN_LABEL="${RUN_LABEL:-}"

find_task_dirs() {
    find "$TASKS_BASE_DIR" -mindepth 3 -maxdepth 3 -type d -name "task_folder" -path "*/task[0-9]*/task_folder" 2>/dev/null | sort
}

# Concurrency control: max concurrent tasks per model
MAX_CONCURRENT_PER_MODEL="${MAX_CONCURRENT_PER_MODEL:-6}"

# Per-task timeout in seconds; the task is killed and retried on timeout
TIMEOUT_PER_TASK="${TIMEOUT_PER_TASK:-7200}"  # default 120 minutes
# Retry timeout in seconds (more headroom on retries)
RETRY_TIMEOUT_PER_TASK="${RETRY_TIMEOUT_PER_TASK:-7200}"  # default 120 minutes
# Max retries on timeout
MAX_RETRIES="${MAX_RETRIES:-2}"

# Provider request timeout policy for benchmark runs. Long reasoning requests may
# stream for more than five minutes, so the outer per-task timeout is the total
# limit. Abort only when an SSE stream produces no data for five minutes.
OPENCODE_PROVIDER_TIMEOUT="${OPENCODE_PROVIDER_TIMEOUT:-false}"
OPENCODE_PROVIDER_CHUNK_TIMEOUT="${OPENCODE_PROVIDER_CHUNK_TIMEOUT:-300000}"

# OpenCode otherwise caps model output (including reasoning tokens) at 32K.
# Raise the default to 128K while preserving explicit caller overrides.
export OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX="${OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX:-131072}"

# Structured trajectory output directory name (relative to each task directory)
TRAJ_DIR_NAME="${TRAJ_DIR_NAME:-model_traj}"



# ============================================================
# ===== Paths =====
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OPENCODE_DIR="${OPENCODE_DIR:-$(dirname "$SCRIPT_DIR")/opencode}"
LOG_DIR="${SCRIPT_DIR}/logs"

mkdir -p "$LOG_DIR"

# ============================================================
# ===== Temp workspace =====
# ============================================================
TEMP_WORKSPACE_BASE="/tmp/opencode_benchmark_workspace"
mkdir -p "$TEMP_WORKSPACE_BASE"

# Extract model_id and short_name from a MODELS entry.
# If the entry has no "|", the model_id is used (with "/" and "." sanitized) as the short_name.
get_model_id() { echo "${1%%|*}"; }
get_model_name() {
    local entry="$1"
    if [[ "$entry" == *"|"* ]]; then
        echo "${entry##*|}"
    else
        echo "$entry" | tr '/' '_' | tr '.' '-'
    fi
}

configure_opencode_provider_timeouts() {
    local provider_ids=()
    local entry model_id provider_id

    for entry in "${MODELS[@]}"; do
        model_id=$(get_model_id "$entry")
        provider_id="${model_id%%/*}"
        provider_ids+=("$provider_id")
    done

    local base_config="${OPENCODE_CONFIG_CONTENT:-}"
    OPENCODE_CONFIG_CONTENT=$(
        OPENCODE_CONFIG_CONTENT="$base_config" python3 - \
            "$OPENCODE_PROVIDER_TIMEOUT" \
            "$OPENCODE_PROVIDER_CHUNK_TIMEOUT" \
            "${provider_ids[@]}" <<'PY'
import json
import os
import sys


def parse_timeout(value: str):
    if value.lower() == "false":
        return False
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("OPENCODE_PROVIDER_TIMEOUT must be false or a positive integer")
    return parsed


def parse_positive_int(name: str, value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


raw = os.environ.get("OPENCODE_CONFIG_CONTENT", "").strip()
config = json.loads(raw) if raw else {}
providers = config.setdefault("provider", {})
timeout = parse_timeout(sys.argv[1])
chunk_timeout = parse_positive_int("OPENCODE_PROVIDER_CHUNK_TIMEOUT", sys.argv[2])

for provider_id in dict.fromkeys(sys.argv[3:]):
    provider = providers.setdefault(provider_id, {})
    options = provider.setdefault("options", {})
    options["timeout"] = timeout
    options["chunkTimeout"] = chunk_timeout

print(json.dumps(config, separators=(",", ":")))
PY
    )
    export OPENCODE_CONFIG_CONTENT
}

# Sanitize a run label for use as a directory suffix
get_safe_run_label() {
    local label="$1"
    echo "$label" | tr '/ ' '__' | tr '.' '-'
}

# Final output/trajectory directory model name (optionally including the run label)
get_output_dir_model_name() {
    local model_name="$1"
    if [[ -n "${RUN_LABEL:-}" ]]; then
        echo "${model_name}-$(get_safe_run_label "$RUN_LABEL")"
    else
        echo "$model_name"
    fi
}

# Extract the first sessionID from a JSONL trajectory
extract_session_id_from_traj() {
    local traj_file="$1"
    if [[ ! -s "$traj_file" ]]; then
        return 0
    fi
    grep -m1 -o '"sessionID":"[^"]*"' "$traj_file" 2>/dev/null | cut -d'"' -f4 || true
}

# Append a structured attempt-index row for later batch error analysis
append_attempt_index() {
    local index_file="$1"
    local task_name="$2"
    local model_id="$3"
    local model_name="$4"
    local attempt="$5"
    local status="$6"
    local exit_code="$7"
    local timeout_seconds="$8"
    local session_id="$9"
    local started_at="${10}"
    local finished_at="${11}"
    local traj_file="${12}"

    if [[ ! -f "$index_file" ]]; then
        printf 'task_name\tmodel_id\tmodel_name\tattempt\tstatus\texit_code\ttimeout_seconds\tsession_id\tstarted_at\tfinished_at\ttraj_file\n' > "$index_file"
    fi

    printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$task_name" \
        "$model_id" \
        "$model_name" \
        "$attempt" \
        "$status" \
        "$exit_code" \
        "$timeout_seconds" \
        "$session_id" \
        "$started_at" \
        "$finished_at" \
        "$traj_file" >> "$index_file"
}

extract_attempt_error_message() {
    local traj_file="$1"
    python3 - "$traj_file" <<'PY'
import json
import sys

path = sys.argv[1]

try:
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                obj = json.loads(line)
            except Exception:
                continue
            err = obj.get("error")
            if not isinstance(err, dict):
                continue
            data = err.get("data")
            if isinstance(data, dict) and data.get("message"):
                print(str(data["message"]))
                raise SystemExit(0)
            if err.get("message"):
                print(str(err["message"]))
                raise SystemExit(0)
except FileNotFoundError:
    pass
PY
}

# Check whether a task has already completed (output dir exists and is non-empty)
is_task_completed() {
    local output_dir="$1"
    if [[ -d "$output_dir" ]] && [[ -n "$(ls -A "$output_dir" 2>/dev/null)" ]]; then
        return 0  # completed
    fi
    return 1  # not completed
}

# Run a single task with the given model
run_task() {
    local task_dir="$1"
    local model="$2"
    local model_name="$3"
    local task_parent_dir="$(dirname "$task_dir")"
    local profession=$(basename "$(dirname "$task_parent_dir")")
    local task_name="${profession}_$(basename "$task_parent_dir")"
    local output_model_name
    output_model_name=$(get_output_dir_model_name "$model_name")

    # Final output directory
    local final_output_dir="${task_parent_dir}/model_output/${output_model_name}"
    local traj_output_dir="${task_parent_dir}/${TRAJ_DIR_NAME}/${output_model_name}"
    local traj_index_file="${traj_output_dir}/attempt_index.tsv"
    local log_file="${LOG_DIR}/jb_${task_name}_${model_name}_$(date +%Y%m%d_%H%M%S).log"

    # Resumable: skip tasks that already have output
    if is_task_completed "$final_output_dir"; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] SKIP (already completed): $task_name ($model_name)"
        return 0
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting: $task_name ($model_name)"

    # Temp workspace: copy the whole task_folder into /tmp
    # Use BASHPID for the subshell PID ($$ would be the parent and is shared by background jobs)
    local temp_workspace="${TEMP_WORKSPACE_BASE}/${task_name}_${model_name}_${BASHPID}"
    local temp_task_folder="${temp_workspace}/task_folder"
    local temp_output_dir="${temp_workspace}/output"

    # Create the temp workspace and copy the task_folder
    mkdir -p "$temp_workspace"
    cp -r "$task_dir" "$temp_task_folder"
    mkdir -p "$temp_output_dir"
    mkdir -p "$traj_output_dir"

    # Make sure TASK_INSTRUCTIONS.txt exists
    local instructions_file="${temp_task_folder}/TASK_INSTRUCTIONS.txt"
    if [[ ! -f "$instructions_file" ]]; then
        echo "[ERROR] TASK_INSTRUCTIONS.txt not found in $temp_task_folder" | tee -a "$log_file"
        rm -rf "$temp_workspace"
        return 1
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Running OpenCode for: $task_name ($model_name)" | tee -a "$log_file"
    echo "Original task folder: $task_dir" | tee -a "$log_file"
    echo "Temp workspace: $temp_workspace" | tee -a "$log_file"
    echo "Temp task folder: $temp_task_folder" | tee -a "$log_file"
    echo "Temp output directory: $temp_output_dir" | tee -a "$log_file"
    echo "Model: $model" | tee -a "$log_file"
    echo "Final output directory: $final_output_dir" | tee -a "$log_file"
    echo "Trajectory directory: $traj_output_dir" | tee -a "$log_file"

    # Permission setup: allow read/write access to the temp workspace
    # We must allow both:
    #   1. ${TEMP_WORKSPACE_BASE}/* -- when a tool accesses the workspace dir itself, assertExternalDirectory uses the parent and generates this pattern
    #   2. ${temp_workspace}/**     -- covers everything inside the workspace (task_folder/*, output/*, ...)
    # Allow all paths under /tmp (models often write temp files there), deny other external dirs (avoid interactive prompts that hang)
    local permission_json="{\"external_directory\": {\"*\": \"deny\", \"/tmp\": \"allow\", \"/tmp/*\": \"allow\", \"/tmp/**\": \"allow\", \"${TEMP_WORKSPACE_BASE}/*\": \"allow\", \"${temp_workspace}/**\": \"allow\"}}"

    # Build the prompt message
    # Tell the model to read from temp_task_folder and write to temp_output_dir
    local prompt_msg="=== TASK FOLDER ===
${temp_task_folder}

=== INSTRUCTIONS ===
1. Read the TASK_INSTRUCTIONS.txt file in the task folder above
2. Based on the Reference Files section in TASK_INSTRUCTIONS.txt, read the corresponding files from the same task folder using appropriate tools.
3. Complete the task as specified in TASK_INSTRUCTIONS.txt
4. Only save the final deliverables to the output directory specified below. Do not save any intermediate or temporary files.

=== OUTPUT DIRECTORY ===
${temp_output_dir}

IMPORTANT:
- All reference files are in the task folder: ${temp_task_folder}
- Only save the final deliverables to the output directory ${temp_output_dir}. Do not save any intermediate or temporary files.
- You MUST only access files within ${temp_workspace} or search online for new reference files if you find needed. Do NOT access any files or directories in this system outside of this path.
- If you encounter ambiguous or conflicting information, analyze the conflict, explain your reasoning, and justify the approach you choose.
- If a file cannot be read directly (e.g., .xlsx, .docx, .db, .pptx), use appropriate tools, MCP servers, or code to extract and process its contents."

    # Run OpenCode (with timeout and retry)
    local attempt=0
    local exit_code=1

    while [[ $attempt -lt $MAX_RETRIES ]]; do
        attempt=$((attempt + 1))

        # Use a longer timeout on retries
        local current_timeout="${TIMEOUT_PER_TASK}"
        if [[ $attempt -gt 1 ]]; then
            current_timeout="${RETRY_TIMEOUT_PER_TASK}"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] RETRY $attempt/$MAX_RETRIES: $task_name ($model_name) (previous attempt failed, retry timeout: ${current_timeout}s)" | tee -a "$log_file"
            # Before retrying: rescue any partial output from the timed-out attempt so rm doesn't delete it
            if [[ -d "$temp_output_dir" ]] && [[ -n "$(ls -A "$temp_output_dir" 2>/dev/null)" ]]; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] Rescuing partial output from timed-out attempt $((attempt-1)) to: $final_output_dir" | tee -a "$log_file"
                mkdir -p "$final_output_dir"
                cp -r "$temp_output_dir"/. "$final_output_dir"/
            fi
            # Clean the temp dirs and re-copy task_folder to start the retry from a clean state
            rm -rf "$temp_task_folder" "$temp_output_dir"
            cp -r "$task_dir" "$temp_task_folder"
            mkdir -p "$temp_output_dir"
        fi

        local attempt_started_at
        attempt_started_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        local attempt_stamp
        attempt_stamp="$(date +%Y%m%d_%H%M%S)"
        local attempt_label="attempt${attempt}_${attempt_stamp}"
        local attempt_traj_file="${traj_output_dir}/${task_name}_${model_name}_${attempt_label}.jsonl"
        local attempt_stdout_file
        attempt_stdout_file="$(mktemp "${traj_output_dir}/${task_name}_${model_name}_${attempt_label}.stdout.XXXXXX")"
        local session_title="${task_name} | ${model_name} | attempt ${attempt}"

        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt trajectory file: $attempt_traj_file" | tee -a "$log_file"

        set +e
        OPENCODE_PERMISSION="$permission_json" timeout --kill-after=30s "${current_timeout}" \
            bun run --cwd "${OPENCODE_DIR}/packages/opencode" --conditions=browser src/index.ts run "$prompt_msg" \
            --model "$model" \
            --title "$session_title" \
            --format json \
            > "$attempt_stdout_file" \
            2> >(tee -a "$log_file" >&2)
        exit_code=$?
        set -e

        sed -n '/^{"type":/p' "$attempt_stdout_file" > "$attempt_traj_file"
        rm -f "$attempt_stdout_file"

        local session_id
        session_id="$(extract_session_id_from_traj "$attempt_traj_file")"
        local attempt_error_message=""
        attempt_error_message="$(extract_attempt_error_message "$attempt_traj_file")"
        local attempt_finished_at
        attempt_finished_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        local attempt_status="failed"
        local attempt_has_output=0

        if [[ -d "$temp_output_dir" ]] && [[ -n "$(ls -A "$temp_output_dir" 2>/dev/null)" ]]; then
            attempt_has_output=1
        fi

        if [[ $exit_code -eq 124 || $exit_code -eq 137 ]]; then
            attempt_status="timeout"
        elif [[ $exit_code -eq 0 ]]; then
            if [[ $attempt_has_output -eq 1 ]]; then
                attempt_status="success"
            else
                # A zero exit code without deliverables still means the task failed.
                exit_code=1
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] EMPTY OUTPUT: Task exited successfully but produced no files: $task_name ($model_name)" | tee -a "$log_file"
            fi
        fi

        if [[ -n "$attempt_error_message" ]]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt API error: $attempt_error_message" | tee -a "$log_file"
        fi

        append_attempt_index \
            "$traj_index_file" \
            "$task_name" \
            "$model" \
            "$model_name" \
            "$attempt" \
            "$attempt_status" \
            "$exit_code" \
            "$current_timeout" \
            "${session_id:-}" \
            "$attempt_started_at" \
            "$attempt_finished_at" \
            "$attempt_traj_file"

        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt session: ${session_id:-unknown}" | tee -a "$log_file"
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt structured trajectory saved to: $attempt_traj_file" | tee -a "$log_file"

        if [[ $exit_code -eq 124 || $exit_code -eq 137 ]]; then
            # 124 = timeout (SIGTERM), 137 = killed (SIGKILL via --kill-after)
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] TIMEOUT: Task exceeded ${current_timeout}s: $task_name ($model_name)" | tee -a "$log_file"
            if [[ $attempt -lt $MAX_RETRIES ]]; then
                continue
            else
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] GIVE UP: Task still timing out after $MAX_RETRIES attempts: $task_name ($model_name)" | tee -a "$log_file"
            fi
        elif [[ $exit_code -eq 0 ]]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Task completed successfully: $task_name ($model_name)" | tee -a "$log_file"
            break
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Task failed with exit code $exit_code: $task_name ($model_name)" | tee -a "$log_file"
            break
        fi
    done

    # Task done: move the output directory to its final location
    if [[ -d "$temp_output_dir" ]] && [[ -n "$(ls -A "$temp_output_dir" 2>/dev/null)" ]]; then
        mkdir -p "$final_output_dir"
        mv "$temp_output_dir"/* "$final_output_dir"/
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Output moved to: $final_output_dir" | tee -a "$log_file"
        echo "Output files:" | tee -a "$log_file"
        ls -la "$final_output_dir" | tee -a "$log_file"
    elif [[ -d "$final_output_dir" ]] && [[ -n "$(ls -A "$final_output_dir" 2>/dev/null)" ]]; then
        # Files were already rescued before the retry; use them
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Output rescued from prior timed-out attempt: $final_output_dir" | tee -a "$log_file"
        echo "Output files:" | tee -a "$log_file"
        ls -la "$final_output_dir" | tee -a "$log_file"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] No output files generated in $temp_output_dir" | tee -a "$log_file"
    fi

    # Clean up the temp workspace
    rm -rf "$temp_workspace"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Cleaned up temp workspace: $temp_workspace" | tee -a "$log_file"

    return $exit_code
}

# Main entry point
main() {
    local num_models=${#MODELS[@]}

    configure_opencode_provider_timeouts

    echo "=========================================="
    echo "JobBench Benchmark Runner (${num_models} Models)"
    echo "Models:"
    for entry in "${MODELS[@]}"; do
        echo "  - $(get_model_name "$entry"): $(get_model_id "$entry")"
    done
    echo "Tasks Base Directory: $TASKS_BASE_DIR"
    echo "Run Label: ${RUN_LABEL:-<none>}"
    echo "Temp Workspace: $TEMP_WORKSPACE_BASE"
    echo "Max concurrent per model: $MAX_CONCURRENT_PER_MODEL"
    echo "Timeout per task: ${TIMEOUT_PER_TASK}s ($(( TIMEOUT_PER_TASK / 60 )) min)"
    echo "Retry timeout per task: ${RETRY_TIMEOUT_PER_TASK}s ($(( RETRY_TIMEOUT_PER_TASK / 60 )) min)"
    echo "Max retries on timeout: $MAX_RETRIES"
    echo "OpenCode provider total timeout: $OPENCODE_PROVIDER_TIMEOUT"
    echo "OpenCode provider SSE chunk timeout: ${OPENCODE_PROVIDER_CHUNK_TIMEOUT}ms"
    echo "OpenCode output token max: $OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"
    echo "Structured trajectory directory name: $TRAJ_DIR_NAME"
    echo "OpenCode directory: $OPENCODE_DIR"
    echo "=========================================="

    if [[ ${num_models} -eq 0 ]]; then
        echo "[ERROR] No models specified. Set BENCHMARK_MODELS, e.g.:" >&2
        echo "  BENCHMARK_MODELS=\"anthropic/claude-sonnet-4-5|sonnet-4-5\" $0" >&2
        exit 1
    fi

    if [[ ! -d "$OPENCODE_DIR/packages/opencode" ]]; then
        echo "[ERROR] OpenCode is not installed at: $OPENCODE_DIR" >&2
        echo "Run ./setup_opencode.sh from the repo root first (or set OPENCODE_DIR)." >&2
        exit 1
    fi

    # cd into the opencode directory
    cd "$OPENCODE_DIR"

    local task_dirs=($(find_task_dirs))
    local total_tasks=${#task_dirs[@]}

    if [[ $total_tasks -eq 0 ]]; then
        echo "No task_folder directories found in $TASKS_BASE_DIR"
        exit 1
    fi

    echo "Found $total_tasks task_folder directories"
    echo "Will run ${total_tasks} x ${num_models} = $((total_tasks * num_models)) total tasks"
    echo ""

    local failed=0
    local completed=0
    local total=$((total_tasks * num_models))

    # Use temp files to store per-model PID lists (bash has no dynamically named arrays)
    local pids_dir
    pids_dir=$(mktemp -d)
    for entry in "${MODELS[@]}"; do
        touch "${pids_dir}/$(get_model_name "$entry")"
    done

    # Reap finished processes; update the PID file
    cleanup_finished_pids_file() {
        local pids_file="$1"
        local new_pids=()
        local pids
        read -ra pids < "$pids_file" 2>/dev/null || pids=()
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                new_pids+=("$pid")
            else
                if ! wait "$pid"; then
                    ((failed++)) || true
                fi
                ((completed++)) || true
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] Progress: $completed/$total completed, $failed failed"
            fi
        done
        echo "${new_pids[*]}" > "$pids_file"
    }

    count_pids() {
        local pids
        read -ra pids < "$1" 2>/dev/null || pids=()
        echo "${#pids[@]}"
    }

    add_pid() {
        local pids_file="$1"
        local new_pid="$2"
        local pids
        read -ra pids < "$pids_file" 2>/dev/null || pids=()
        pids+=("$new_pid")
        echo "${pids[*]}" > "$pids_file"
    }

    for task_dir in "${task_dirs[@]}"; do
        local task_name=$(dirname "$task_dir" | xargs basename)

        for entry in "${MODELS[@]}"; do
            local model_id=$(get_model_id "$entry")
            local model_name=$(get_model_name "$entry")
            local pids_file="${pids_dir}/${model_name}"

            # Wait until this model has a free slot
            while true; do
                cleanup_finished_pids_file "$pids_file"
                local running=$(count_pids "$pids_file")
                if [[ $running -lt $MAX_CONCURRENT_PER_MODEL ]]; then
                    break
                fi
                sleep 2
            done

            run_task "$task_dir" "$model_id" "$model_name" &
            add_pid "$pids_file" "$!"
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Launched: ${task_name} (${model_name}) - PID: $! (${model_name} running: $(count_pids "$pids_file")/$MAX_CONCURRENT_PER_MODEL)"
        done
    done

    echo ""
    echo "All tasks launched, waiting for remaining tasks to complete..."
    echo ""

    # Wait for the remaining tasks of every model to finish
    for entry in "${MODELS[@]}"; do
        local model_name=$(get_model_name "$entry")
        local pids_file="${pids_dir}/${model_name}"
        local pids
        read -ra pids < "$pids_file" 2>/dev/null || pids=()
        for pid in "${pids[@]}"; do
            if kill -0 "$pid" 2>/dev/null; then
                if ! wait "$pid"; then
                    ((failed++)) || true
                fi
                ((completed++)) || true
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] Progress: $completed/$total completed, $failed failed"
            fi
        done
    done

    # Clean up temp PID dir
    rm -rf "$pids_dir"

    echo ""
    echo "=========================================="
    echo "Benchmark Complete"
    echo "Total tasks: $((total_tasks * num_models)) (${total_tasks} tasks x ${num_models} models)"
    echo "Failed tasks: $failed"
    echo "Logs saved to: $LOG_DIR"
    echo "Output directories:"
    for entry in "${MODELS[@]}"; do
        local out_name
        out_name=$(get_output_dir_model_name "$(get_model_name "$entry")")
        echo "  - ${out_name}: model_output/${out_name}/"
    done
    echo "Trajectory directories:"
    for entry in "${MODELS[@]}"; do
        local out_name
        out_name=$(get_output_dir_model_name "$(get_model_name "$entry")")
        echo "  - ${out_name}: ${TRAJ_DIR_NAME}/${out_name}/"
    done
    echo "=========================================="
}

# Run main when this script is invoked directly
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
