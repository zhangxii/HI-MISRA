#!/bin/bash

# =================================================================
# Run the complete refine and comparison pipeline for code repair.
#
# Usage:
#   srcipt/pipeline/refine_pipeline.sh [--base] [--intervenor] [--iter] [--explain] [--indict] [--reuse | --no-reuse] [--max-iters <count>] <model_index>
#
# Example:
#   1. srcipt/pipeline/refine_pipeline.sh --explain 0
#   2. srcipt/pipeline/refine_pipeline.sh --iter --indict --max-iters 5 --no-reuse 0
#   3. srcipt/pipeline/refine_pipeline.sh --base --iter --intervenor --explain --indict 0
# =================================================================

export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONIOENCODING=utf-8

if [ ! -t 1 ]; then
    export NO_COLOR=1
    export TQDM_DISABLE=1
    export TERM=dumb
fi

set -e
set -o pipefail

# Parse modes and arguments
MODES_TO_RUN=()
REUSE_FLAG="--reuse"
MAX_ITERS_VALUE=3
ITER_RETRY_VALUES=(3 10 20)

while [[ "$1" == --* ]]; do
    case "$1" in
        --base)       MODES_TO_RUN+=("base"); shift ;;
        --iter)       MODES_TO_RUN+=("iter"); shift ;;
        --intervenor) MODES_TO_RUN+=("intervenor"); shift ;;
        --indict)     MODES_TO_RUN+=("indict"); shift ;;
        --explain)    MODES_TO_RUN+=("explain"); shift ;;
        --reuse)      REUSE_FLAG="--reuse"; shift ;;
        --no-reuse)   REUSE_FLAG=""; shift ;;
        --max-iters)
            if [[ -n "$2" && "$2" != --* ]]; then
                MAX_ITERS_VALUE="$2"
                shift 2
            else
                echo "Error: --max-iters requires a numeric argument"
                exit 1
            fi
            ;;
        *) echo "Error: Unknown option $1"; exit 1 ;;
    esac
done

# Experiment count (set to 1 to reuse previous unlabeled results; 2, 3, ... for new experiments)
TIME=1

# ============== Model Configuration ==============
MODEL_FAMILYS=("openai" "openai" "deepseek" "gemini" "claude"  "deepseek" "deepseek" )
MODEL_NAMES=("gpt-5-mini" "gpt-5" "deepseek-reasoner"  "gemini-2.5-flash" "claude-3-7-sonnet-20250219" "deepseek-chat" "deepseek-v3.1-250821")

if [ "$#" -eq 0 ]; then
    echo "Error: Please provide at least one model index as argument"
    exit 1
fi
MODEL_INDEX_LIST=("$@")

LANGUAGE="c"
WORKERS=32
START_INDEX=0
END_INDEX=250
MISRA_SCRIPT="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/misra.py"

# Select MISRA rules and JSON file based on language
if [[ "$LANGUAGE" == "c" ]]; then
    MISRA_RULE="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/test/misra/rule/misra.txt"
    MISRA_JSON="data/misra/misra-c-2012-enhanced.json"
else
    MISRA_RULE="data/misra/misra-cpp-2008.txt"
    MISRA_JSON="data/misra/misra-cpp-2008.json"
fi

# Print configuration overview
MODELS_TO_PROCESS=()
for index in "${MODEL_INDEX_LIST[@]}"; do
    if [[ -n "${MODEL_NAMES[$index]}" ]]; then
        MODELS_TO_PROCESS+=("${MODEL_NAMES[$index]}")
    else
        MODELS_TO_PROCESS+=("unknown_index:$index")
    fi
done
MODES_STR=$(IFS=, ; echo "${MODES_TO_RUN[*]}")
MODELS_STR=$(IFS=, ; echo "${MODELS_TO_PROCESS[*]}")
ITER_VALUES_STR=$(IFS=, ; echo "${ITER_RETRY_VALUES[*]}")
REUSE_STATUS="Enabled"
[[ -z "$REUSE_FLAG" ]] && REUSE_STATUS="Disabled"

echo "=========================================================="
echo "           Codeflow Refine & Compare Pipeline"
echo "----------------------------------------------------------"
echo " Models       : [${MODELS_STR}]"
echo " Modes        : [${MODES_STR}]"
echo " Language     : [${LANGUAGE}]"
echo " Time         : [t${TIME}]"
echo " Max Iters    : [${MAX_ITERS_VALUE}]"
echo " Iter Retries : [${ITER_VALUES_STR}]"
echo " Reuse        : [${REUSE_STATUS}]"
echo " Workers      : [${WORKERS}]"
echo " Range        : [${START_INDEX}, ${END_INDEX}]"
echo "=========================================================="

# --- Main loop ---
for MODEL_INDEX in "${MODEL_INDEX_LIST[@]}"; do
    MODEL_FAMILY=${MODEL_FAMILYS[$MODEL_INDEX]}
    MODEL_NAME=${MODEL_NAMES[$MODEL_INDEX]}

    WORKDIR="data/codeflowbench/${LANGUAGE}/${MODEL_NAME}"
    DIR_REFINED="$WORKDIR/refined"
    DIR_REHARNESSED="$WORKDIR/reharnessed"
    DIR_REPORTS="$WORKDIR/reports"
    mkdir -p "$DIR_REFINED" "$DIR_REHARNESSED" "$DIR_REPORTS"

    DETAIL_IN="${WORKDIR}/statistic/${MODEL_NAME}_${LANGUAGE}_stat_detail.jsonl"

    echo
    echo "=========================================================="
    echo "Model: [$MODEL_NAME]"
    echo "=========================================================="

    # --- Step 1: Refine and reharness ---
    for MODE in "${MODES_TO_RUN[@]}"; do
        if [[ "$MODE" == "iter" ]]; then
            for RETRY_VAL in "${ITER_RETRY_VALUES[@]}"; do
                echo
                echo "-- [${MODE}] retries=${RETRY_VAL} --"

                OUTPUT_SUFFIX="_${MODE}_r${RETRY_VAL}"
                REFINE_SCRIPT="refine_iter.py"
                DETAIL_REFINE="$DIR_REFINED/${MODEL_NAME}_refined${OUTPUT_SUFFIX}_t${TIME}.jsonl"
                DETAIL_REFINE_EVAL="$DIR_REHARNESSED/${MODEL_NAME}_reharnessed${OUTPUT_SUFFIX}_t${TIME}.jsonl"
                MAX_ITERS_ARG="--max_retries $RETRY_VAL"

                echo "  1.1 Refining..."
                eval "python3 src/pipeline/refine/${REFINE_SCRIPT} \
                    --model_family \"$MODEL_FAMILY\" --model_name \"$MODEL_NAME\" \
                    --in_jsonl \"$DETAIL_IN\" --out_jsonl \"$DETAIL_REFINE\" \
                    --workers $WORKERS --start_index $START_INDEX --end_index $END_INDEX \
                    --lang $LANGUAGE $REUSE_FLAG $MAX_ITERS_ARG"

                echo "  1.2 Reharnessing..."
                eval "python3 src/pipeline/refine/reharness.py \
                    --in_jsonl \"$DETAIL_REFINE\" --out_jsonl \"$DETAIL_REFINE_EVAL\" \
                    --misra_script \"$MISRA_SCRIPT\" --misra_rule \"$MISRA_RULE\" \
                    --lang \"$LANGUAGE\" $REUSE_FLAG"
            done
        else
            case "$MODE" in
                base)       REFINE_SCRIPT="refine.py" ;;
                intervenor) REFINE_SCRIPT="refine_intervenor.py" ;;
                explain)    REFINE_SCRIPT="refine_explain.py" ;;
                indict)     REFINE_SCRIPT="refine_indict.py" ;;
                *) echo "Error: Unknown mode $MODE"; exit 1 ;;
            esac

            echo
            echo "-- [$MODE] --"

            OUTPUT_SUFFIX="_${MODE}"
            DETAIL_REFINE="$DIR_REFINED/${MODEL_NAME}_refined${OUTPUT_SUFFIX}_t${TIME}.jsonl"
            DETAIL_REFINE_EVAL="$DIR_REHARNESSED/${MODEL_NAME}_reharnessed${OUTPUT_SUFFIX}_t${TIME}.jsonl"

            MAX_ITERS_ARG=""
            if [[ "$MODE" == "explain" || "$MODE" == "indict" ]]; then
                MAX_ITERS_ARG="--max_retries $MAX_ITERS_VALUE"
            fi
            if [[ "$MODE" == "explain" ]]; then
                MAX_ITERS_ARG="$MAX_ITERS_ARG --misra_json $MISRA_JSON"
            fi

            echo "  1.1 Refining..."
            eval "python3 src/pipeline/refine/${REFINE_SCRIPT} \
                --model_family \"$MODEL_FAMILY\" --model_name \"$MODEL_NAME\" \
                --in_jsonl \"$DETAIL_IN\" --out_jsonl \"$DETAIL_REFINE\" \
                --workers $WORKERS --start_index $START_INDEX --end_index $END_INDEX \
                --lang $LANGUAGE $REUSE_FLAG $MAX_ITERS_ARG"

            echo "  1.2 Reharnessing..."
            eval "python3 src/pipeline/refine/reharness.py \
                --in_jsonl \"$DETAIL_REFINE\" --out_jsonl \"$DETAIL_REFINE_EVAL\" \
                --misra_script \"$MISRA_SCRIPT\" --misra_rule \"$MISRA_RULE\" \
                --lang \"$LANGUAGE\" $REUSE_FLAG"
        fi
    done

    # --- Step 2: Statistical comparison ---
    echo
    echo "-- Step 2: Comparison --"

    files_to_compare=()
    labels_to_compare=()

    for label in "${MODES_TO_RUN[@]}"; do
        if [[ "$label" == "iter" ]]; then
            for RETRY_VAL in "${ITER_RETRY_VALUES[@]}"; do
                local_label="iter_r${RETRY_VAL}"
                file_path="$DIR_REHARNESSED/${MODEL_NAME}_reharnessed_iter_r${RETRY_VAL}_t${TIME}.jsonl"
                if [ -f "$file_path" ]; then
                    files_to_compare+=("\"$file_path\"")
                    labels_to_compare+=("$local_label")
                else
                    echo "Warning: Result file not found for [$local_label], skipping."
                fi
            done
        else
            file_path="$DIR_REHARNESSED/${MODEL_NAME}_reharnessed_${label}_t${TIME}.jsonl"
            if [ -f "$file_path" ]; then
                files_to_compare+=("\"$file_path\"")
                labels_to_compare+=("$label")
            else
                echo "Warning: Result file not found for [$label], skipping."
            fi
        fi
    done

    if [ ${#files_to_compare[@]} -gt 0 ]; then
        COMPARE_CSV="$DIR_REPORTS/${MODEL_NAME}_comparison_t${TIME}.csv"
        COMPARE_DETAIL_CSV="$DIR_REPORTS/${MODEL_NAME}_comparison_detail_t${TIME}.csv"
        COMPARE_MD="$DIR_REPORTS/${MODEL_NAME}_comparison_t${TIME}.md"
        COMPARE_REPORT_TXT="$DIR_REPORTS/${MODEL_NAME}_comparison_summary_t${TIME}.txt"

        files_str=$(IFS=' '; echo "${files_to_compare[*]}")
        labels_str=$(IFS=' '; echo "${labels_to_compare[*]}")

        eval "python3 src/pipeline/refine/statistic_refine_compare.py \
            --start_index $START_INDEX --end_index $END_INDEX \
            --origin_jsonl \"$DETAIL_IN\" \
            --refined_files $files_str \
            --refined_labels $labels_str \
            --detailed_out_csv \"$COMPARE_DETAIL_CSV\" \
            --misra_json \"$MISRA_JSON\" \
            --out_md \"$COMPARE_MD\" \
            --out_csv \"$COMPARE_CSV\"" | tee "$COMPARE_REPORT_TXT"

        echo "==== [$MODEL_NAME] done. Reports: $DIR_REPORTS ===="
    else
        echo "Error: No result files found for [$MODEL_NAME]."
    fi
done

echo "==== All model pipelines completed ===="