#!/usr/bin/env bash
# run_judge.sh - Evaluate outputs against rubrics using a text-only judge runner
# Text-only judge: calls an OpenAI-compatible API directly; no OpenCode agent framework needed
# Rubric-level evaluation: judge ALL criteria of a single rubric in one shot
# Judge model returns pass/fail per criterion; rubric overall is binary pass/fail
# Supports multiple model subdirectories under model_output/, evaluated separately

# set -e  # disabled: some commands return non-zero without affecting overall flow

# ============================================================
# ===== Configuration (edit here) =====
# ============================================================

SPLIT="${SPLIT:-main}"
TARGET_DIR="${TARGET_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/dataset/${SPLIT}}"
rubrics_file_name="RUBRICS"
MAX_CONCURRENT="${MAX_CONCURRENT:-10}"              # max concurrency at the rubric level
MAX_JUDGE_WORKERS="${MAX_JUDGE_WORKERS:-$MAX_CONCURRENT}"
MAX_RETRIES="${MAX_RETRIES:-1}"
DEFAULT_JUDGE_MODELS="${DEFAULT_JUDGE_MODELS:-grok-4-1-fast}"

[[ "$TARGET_DIR" != /* ]] && TARGET_DIR="$(cd "$TARGET_DIR" 2>/dev/null && pwd)"

JUDGE_API_BASE="${JUDGE_API_BASE:-https://api.x.ai/v1}"
JUDGE_API_KEY="${JUDGE_API_KEY:-}"
JUDGE_ALT_API_BASE="${JUDGE_ALT_API_BASE:-$JUDGE_API_BASE}"
JUDGE_ALT_API_KEY="${JUDGE_ALT_API_KEY:-$JUDGE_API_KEY}"
JUDGE_ALT_MODELS="${JUDGE_ALT_MODELS:-}"

if [[ -n "${JUDGE_MODELS:-}" ]]; then
    JUDGE_MODELS_ARRAY=($JUDGE_MODELS)
elif [[ -n "${JUDGE_MODEL:-}" ]]; then
    JUDGE_MODELS_ARRAY=($JUDGE_MODEL)
else
    JUDGE_MODELS_ARRAY=($DEFAULT_JUDGE_MODELS)
fi

EVAL_MODEL_DEFAULT=""
EVAL_MODEL="${EVAL_MODEL:-$EVAL_MODEL_DEFAULT}"

TIMEOUT_PER_RUBRIC="${TIMEOUT_PER_RUBRIC:-300}"

OUTPUT_DIR_NAME="model_output"
JUDGE_RESULTS_DIR="eval_result"

# ============================================================
# ===== Paths (rarely need editing) =====
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
JOBBENCH_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SCRIPT_PATH="${SCRIPT_DIR}/$(basename "${BASH_SOURCE[0]}")"
LOG_DIR="${SCRIPT_DIR}/logs"
TEMP_DIR="${SCRIPT_DIR}/.judge_tmp"
DETAIL_LOG_DIR="${LOG_DIR}/detail"

mkdir -p "$LOG_DIR" "$TEMP_DIR" "$DETAIL_LOG_DIR"

# ===== Helper functions =====

find_task_dirs() {
    find "$TARGET_DIR" -mindepth 2 -maxdepth 2 -type d -regex '.*/task[0-9]+' 2>/dev/null | sort
}

find_model_dirs() {
    local output_dir="$1"
    find "$output_dir" -mindepth 1 -maxdepth 1 -type d 2>/dev/null | sort
}

collect_output_files() {
    local output_dir="$1"
    find "$output_dir" -type f 2>/dev/null
}

get_rubrics_path() {
    local rubrics_file="$1"
    if jq -e '.rubrics' "$rubrics_file" >/dev/null 2>&1; then
        echo ".rubrics"
    elif jq -e '.evaluation_rubrics' "$rubrics_file" >/dev/null 2>&1; then
        echo ".evaluation_rubrics"
    else
        echo ""
    fi
}

model_uses_alt_route() {
    local model="$1"
    local routed_model

    for routed_model in $JUDGE_ALT_MODELS; do
        [[ -z "$routed_model" ]] && continue
        if [[ "$model" == "$routed_model" ]]; then
            return 0
        fi
    done

    return 1
}

get_api_base_for_model() {
    local model="$1"
    if model_uses_alt_route "$model"; then
        echo "$JUDGE_ALT_API_BASE"
        return 0
    fi
    echo "$JUDGE_API_BASE"
}

get_api_key_for_model() {
    local model="$1"
    if model_uses_alt_route "$model"; then
        echo "$JUDGE_ALT_API_KEY"
        return 0
    fi
    echo "$JUDGE_API_KEY"
}

pick_judge_python() {
    local requested="${JOBBENCH_JUDGE_PYTHON:-}"
    local candidate=""
    local project_python="${JOBBENCH_ROOT}/.venv/bin/python"

    if [[ -n "$requested" ]]; then
        if [[ -x "$requested" ]]; then
            echo "$requested"
            return 0
        fi
        if command -v "$requested" >/dev/null 2>&1; then
            command -v "$requested"
            return 0
        fi
        echo "[WARN] JOBBENCH_JUDGE_PYTHON not found: $requested" >&2
    fi

    if [[ -x "$project_python" ]] && "$project_python" -c "import openai" >/dev/null 2>&1; then
        echo "$project_python"
        return 0
    fi

    for candidate in \
        "$HOME/miniconda3/bin/python3" \
        "$HOME/anaconda3/bin/python3"; do
        if [[ -x "$candidate" ]] && "$candidate" -c "import openai" >/dev/null 2>&1; then
            echo "$candidate"
            return 0
        fi
    done

    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            candidate="$(command -v "$candidate")"
            if "$candidate" -c "import openai" >/dev/null 2>&1; then
                echo "$candidate"
                return 0
            fi
        fi
    done

    command -v python3 2>/dev/null || command -v python 2>/dev/null || echo "python3"
}

JUDGE_PYTHON="${JUDGE_PYTHON:-$(pick_judge_python)}"

export TEMP_DIR JUDGE_MODEL TARGET_DIR OUTPUT_DIR_NAME JUDGE_RESULTS_DIR TIMEOUT_PER_RUBRIC DETAIL_LOG_DIR EVAL_MODEL JUDGE_API_BASE JUDGE_API_KEY JUDGE_ALT_API_BASE JUDGE_ALT_API_KEY JUDGE_ALT_MODELS MAX_RETRIES JUDGE_PYTHON

get_safe_judge_model_name() {
    echo "$JUDGE_MODEL" | sed 's|.*/||' | tr '.' '-'
}

aggregate_results() {
    local unique_key="$1"
    local evaluated_model="$2"
    local result_dir="$3"
    local rubric_count="$4"
    local safe_judge_name
    safe_judge_name=$(get_safe_judge_model_name)
    local result_file="${result_dir}/${safe_judge_name}_judge.json"

    if [[ -f "$result_file" ]]; then
        local total_score
        total_score=$(jq -r '.total_score' "$result_file" 2>/dev/null || echo "0")
        local max_score
        max_score=$(jq -r '.max_score' "$result_file" 2>/dev/null || echo "0")
        local passed_count
        passed_count=$(jq -r '.passed_count' "$result_file" 2>/dev/null || echo "0")
        local judged_count
        judged_count=$(jq '.rubrics | length' "$result_file" 2>/dev/null || echo "0")

        echo "      Score: $total_score / $max_score, Passed: $passed_count / $judged_count"
        echo "      Results saved to: $result_file"
    else
        echo "      ERROR: No results generated"
    fi
}

is_model_judged() {
    local result_file="$1"
    local rubric_count="$2"

    if [[ ! -f "$result_file" ]]; then
        return 1
    fi

    local judged_count
    judged_count=$(jq '.rubrics | length' "$result_file" 2>/dev/null || echo "0")
    if [[ "$judged_count" -eq "$rubric_count" ]]; then
        return 0
    fi
    return 1
}

judge_model_output() {
    local task_dir="$1"
    local task_name="$2"
    local model_output_dir="$3"
    local model_name="$4"
    local rubrics_path="$5"
    local rubric_count="$6"
    local log_file="$7"

    local result_dir="${task_dir}/${JUDGE_RESULTS_DIR}/eval_${model_name}"
    mkdir -p "$result_dir"

    local safe_model_name
    safe_model_name=$(echo "$model_name" | tr '/' '_' | tr '.' '-')
    local safe_judge_name
    safe_judge_name=$(get_safe_judge_model_name)
    local result_file="${result_dir}/${safe_judge_name}_judge.json"
    local lock_file="${result_dir}/.${safe_judge_name}_judge.lock"
    local unique_key="${task_name}_${safe_model_name}_${safe_judge_name}"

    echo "    [Model: $model_name]" | tee -a "$log_file"
    echo "      Output dir: $model_output_dir" | tee -a "$log_file"
    echo "      Result dir: $result_dir" | tee -a "$log_file"

    if is_model_judged "$result_file" "$rubric_count"; then
        echo "      [SKIP] Already judged (found $rubric_count rubrics in result)" | tee -a "$log_file"
        return 0
    fi

    local file_count
    file_count=$(collect_output_files "$model_output_dir" | wc -l)
    if [[ $file_count -eq 0 ]]; then
        echo "      [SKIP] Output directory is empty, no files to evaluate" | tee -a "$log_file"
        return 0
    fi

    echo "      Output files:" | tee -a "$log_file"
    collect_output_files "$model_output_dir" | while read -r f; do
        echo "        - $(basename "$f")" | tee -a "$log_file"
    done

    rm -f "${TEMP_DIR}/${unique_key}_"* "$lock_file"

    local api_base
    api_base=$(get_api_base_for_model "$JUDGE_MODEL")
    local api_key
    api_key=$(get_api_key_for_model "$JUDGE_MODEL")

    "$JUDGE_PYTHON" "${SCRIPT_DIR}/judge.py" \
        --output-dir "$model_output_dir" \
        --rubrics-file "${task_dir}/${rubrics_file_name}.json" \
        --details-file "$result_file" \
        --judge-model "$JUDGE_MODEL" \
        --api-base "$api_base" \
        --api-key "$api_key" \
        --max-workers "$MAX_JUDGE_WORKERS" \
        --max-retries "$MAX_RETRIES" \
        --timeout-per-rubric "$TIMEOUT_PER_RUBRIC" \
        --evaluated-model "$model_name" \
        --lock-file "$lock_file" \
        --detail-log-dir "$DETAIL_LOG_DIR" \
        --detail-log-prefix "$unique_key" \
        >> "$log_file" 2>&1
    local judge_exit_code=$?

    if [[ $judge_exit_code -ne 0 ]]; then
        if [[ -z "${KEEP_TEMP_FILES:-}" ]]; then
            rm -f "${TEMP_DIR}/${unique_key}_"* "${TEMP_DIR}/${unique_key}.lock" "$lock_file"
        fi
        echo "      [ERROR] Judge process failed with exit code $judge_exit_code" | tee -a "$log_file"
        return $judge_exit_code
    fi

    aggregate_results "$unique_key" "$model_name" "$result_dir" "$rubric_count" | tee -a "$log_file"

    if [[ -z "${KEEP_TEMP_FILES:-}" ]]; then
        rm -f "${TEMP_DIR}/${unique_key}_"* "${TEMP_DIR}/${unique_key}.lock" "$lock_file"
    fi
}

judge_task() {
    local task_dir="$1"
    local profession
    profession=$(basename "$(dirname "$task_dir")")
    local task_name="${profession}_$(basename "$task_dir")"
    local rubrics_file="${task_dir}/${rubrics_file_name}.json"
    local output_base_dir="${task_dir}/${OUTPUT_DIR_NAME}"
    local log_file="${LOG_DIR}/jb_judge_${task_name}_$(date +%Y%m%d_%H%M%S).log"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting judge for: $task_name" | tee -a "$log_file"

    if [[ ! -f "$rubrics_file" ]]; then
        echo "[ERROR] rubrics not found in $task_dir" | tee -a "$log_file"
        return 1
    fi

    if [[ ! -d "$output_base_dir" ]]; then
        echo "[ERROR] $OUTPUT_DIR_NAME directory not found in $task_dir" | tee -a "$log_file"
        return 1
    fi

    local rubrics_path
    rubrics_path=$(get_rubrics_path "$rubrics_file")
    if [[ -z "$rubrics_path" ]]; then
        echo "[ERROR] Cannot find rubrics array in $rubrics_file" | tee -a "$log_file"
        return 1
    fi

    local rubric_count
    rubric_count=$(jq "${rubrics_path} | length" "$rubrics_file")
    echo "  Rubrics: $rubric_count (path: $rubrics_path)" | tee -a "$log_file"

    local model_dirs=($(find_model_dirs "$output_base_dir"))
    local model_count=${#model_dirs[@]}

    if [[ $model_count -eq 0 ]]; then
        echo "[ERROR] No model directories found in $output_base_dir" | tee -a "$log_file"
        return 1
    fi

    echo "  Found $model_count model(s) to evaluate:" | tee -a "$log_file"
    for model_dir in "${model_dirs[@]}"; do
        echo "    - $(basename "$model_dir")" | tee -a "$log_file"
    done

    if [[ -n "${EVAL_MODEL:-}" ]]; then
        echo "  [DEBUG MODE] Filtering for eval model: $EVAL_MODEL" | tee -a "$log_file"
    fi

    for model_dir in "${model_dirs[@]}"; do
        local model_name
        model_name=$(basename "$model_dir")

        if [[ -n "${EVAL_MODEL:-}" ]]; then
            if [[ "$model_name" != *"$EVAL_MODEL"* ]] && [[ "$EVAL_MODEL" != *"$model_name"* ]]; then
                echo "  [SKIP] $model_name (not matching EVAL_MODEL=$EVAL_MODEL)" | tee -a "$log_file"
                continue
            fi
        fi

        echo "" | tee -a "$log_file"
        judge_model_output "$task_dir" "$task_name" "$model_dir" "$model_name" "$rubrics_path" "$rubric_count" "$log_file"
    done

    echo "" | tee -a "$log_file"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Completed: $task_name" | tee -a "$log_file"
}

run_with_judge_model() {
    local judge_model="$1"
    export JUDGE_MODEL="$judge_model"

    local safe_judge_name
    safe_judge_name=$(echo "$judge_model" | sed 's|.*/||' | tr '.' '-')
    local log_file="${LOG_DIR}/jb_judge_${safe_judge_name}_$(date +%Y%m%d_%H%M%S).log"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting judge with model: $judge_model" | tee -a "$log_file"

    local task_dirs=($(find_task_dirs))
    local total_tasks=${#task_dirs[@]}

    local completed=0
    for task_dir in "${task_dirs[@]}"; do
        ((completed++))
        echo "" | tee -a "$log_file"
        local profession
        profession=$(basename "$(dirname "$task_dir")")
        local tname="${profession}_$(basename "$task_dir")"
        echo "[$judge_model] [$completed/$total_tasks] Processing: $tname" | tee -a "$log_file"
        judge_task "$task_dir" 2>&1 | tee -a "$log_file"
    done

    echo "" | tee -a "$log_file"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Completed judge with model: $judge_model" | tee -a "$log_file"
}

main() {
    echo "=========================================="
    echo "Text-Only Evaluation Judge (Routed OpenAI-Compatible APIs)"
    echo "Target Dir: $TARGET_DIR"
    echo "Judge Models: ${JUDGE_MODELS_ARRAY[*]}"
    echo "Primary Judge API: $JUDGE_API_BASE"
    echo "Alt Judge API: $JUDGE_ALT_API_BASE"
    echo "Alt Judge Models: ${JUDGE_ALT_MODELS:-<none>}"
    echo "Judge Python: $JUDGE_PYTHON"
    echo "Max Concurrent per model: $MAX_CONCURRENT"
    echo "Max Retries per rubric: $MAX_RETRIES"
    if [[ -n "${EVAL_MODEL:-}" ]]; then
        echo "Eval Model Filter: $EVAL_MODEL (DEBUG MODE)"
    else
        echo "Eval Model Filter: (all models)"
    fi
    echo "=========================================="
    echo ""

    local task_dirs=($(find_task_dirs))
    local total_tasks=${#task_dirs[@]}

    if [[ $total_tasks -eq 0 ]]; then
        echo "No task directories found in $TARGET_DIR"
        exit 1
    fi

    echo "Found $total_tasks tasks to judge:"
    for task_dir in "${task_dirs[@]}"; do
        local profession
        profession=$(basename "$(dirname "$task_dir")")
        echo "  - ${profession}_$(basename "$task_dir")"
    done
    echo ""

    local judge_pids=()
    for judge_model in "${JUDGE_MODELS_ARRAY[@]}"; do
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Launching judge with model: $judge_model"
        run_with_judge_model "$judge_model" &
        judge_pids+=($!)
    done

    echo ""
    echo "All ${#JUDGE_MODELS_ARRAY[@]} judge models launched in parallel, waiting for completion..."
    echo ""

    local failed=0
    for pid in "${judge_pids[@]}"; do
        if ! wait "$pid"; then
            ((failed++))
        fi
    done

    echo ""
    echo "=========================================="
    echo "Judge Complete"
    echo "Total judge models: ${#JUDGE_MODELS_ARRAY[@]}"
    echo "Failed: $failed"
    echo "=========================================="
    echo ""

    echo "=== Summary ==="
    echo ""
    printf "%-35s %-15s %-15s %8s %8s %10s\n" "Task" "Eval Model" "Judge Model" "Score" "Max" "Pass Rate"
    printf "%-35s %-15s %-15s %8s %8s %10s\n" "----" "----------" "-----------" "-----" "---" "---------"

    for task_dir in "${task_dirs[@]}"; do
        local profession
        profession=$(basename "$(dirname "$task_dir")")
        local task_name="${profession}_$(basename "$task_dir")"
        local judge_base="${task_dir}/${JUDGE_RESULTS_DIR}"
        if [[ -d "$judge_base" ]]; then
            for eval_model_dir in "$judge_base"/*/; do
                for judge_model in "${JUDGE_MODELS_ARRAY[@]}"; do
                    local safe_judge_name
                    safe_judge_name=$(echo "$judge_model" | sed 's|.*/||' | tr '.' '-')
                    local result_file="${eval_model_dir}${safe_judge_name}_judge.json"
                    if [[ -f "$result_file" ]]; then
                        local eval_model
                        eval_model=$(jq -r '.evaluated_model' "$result_file")
                        local judge
                        judge=$(jq -r '.judge_model' "$result_file" | sed 's|.*/||')
                        local score
                        score=$(jq -r '.total_score' "$result_file")
                        local max
                        max=$(jq -r '.max_score' "$result_file")
                        local rate
                        rate=$(jq -r '.pass_rate' "$result_file")
                        printf "%-35s %-15s %-15s %8s %8s %10s\n" "$task_name" "$eval_model" "$judge" "$score" "$max" "$rate"
                    fi
                done
            done
        fi
    done

    echo ""
    echo "Results saved to: ${JUDGE_RESULTS_DIR}/{eval_model}/{judge_model}_judge.json"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    main "$@"
fi
