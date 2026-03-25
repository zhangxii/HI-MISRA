#!/bin/bash

# =================================================================
# Run the HI-MISRA refine and comparison pipeline for code repair.
#
# Usage:
#   srcipt/pipeline/refine_pipeline.sh [--reuse | --no-reuse] [--max-iters <count>] <model_index1> [model_index2] ...
#
# Example:
#   1. srcipt/pipeline/refine_pipeline.sh 0
#   2. srcipt/pipeline/refine_pipeline.sh --max-iters 5 --no-reuse 0
#   3. srcipt/pipeline/refine_pipeline.sh 0 1 4
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

# Parse options
REUSE_FLAG="--reuse"
MAX_ITERS_VALUE=3

while [[ "$1" == --* ]]; do
    case "$1" in
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
    echo "Usage: $0 [--reuse | --no-reuse] [--max-iters <count>] <model_index1> [model_index2] ..."
    echo "Example: $0 0 1 4"
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
MODELS_STR=$(IFS=, ; echo "${MODELS_TO_PROCESS[*]}")
REUSE_STATUS="Enabled"
[[ -z "$REUSE_FLAG" ]] && REUSE_STATUS="Disabled"

echo "=========================================================="
echo "         HI-MISRA Refine & Compare Pipeline"
echo "----------------------------------------------------------"
echo " Models       : [${MODELS_STR}]"
echo " Language     : [${LANGUAGE}]"
echo " Time         : [t${TIME}]"
echo " Max Iters    : [${MAX_ITERS_VALUE}]"
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

    OUTPUT_SUFFIX="_explain"
    DETAIL_REFINE="$DIR_REFINED/${MODEL_NAME}_refined${OUTPUT_SUFFIX}_t${TIME}.jsonl"
    DETAIL_REFINE_EVAL="$DIR_REHARNESSED/${MODEL_NAME}_reharnessed${OUTPUT_SUFFIX}_t${TIME}.jsonl"

    echo
    echo "=========================================================="
    echo "Model: [$MODEL_NAME]"
    echo "=========================================================="

    # --- Step 1: Refine with HI-MISRA (knowledge-guided) ---
    echo
    echo "-- Step 1.1: Refining with HI-MISRA --"
    eval "python3 src/pipeline/refine/refine_explain.py \
        --model_family \"$MODEL_FAMILY\" --model_name \"$MODEL_NAME\" \
        --in_jsonl \"$DETAIL_IN\" --out_jsonl \"$DETAIL_REFINE\" \
        --workers $WORKERS --start_index $START_INDEX --end_index $END_INDEX \
        --lang $LANGUAGE $REUSE_FLAG --max_retries $MAX_ITERS_VALUE --misra_json $MISRA_JSON"

    echo
    echo "-- Step 1.2: Reharnessing --"
    eval "python3 src/pipeline/refine/reharness.py \
        --in_jsonl \"$DETAIL_REFINE\" --out_jsonl \"$DETAIL_REFINE_EVAL\" \
        --misra_script \"$MISRA_SCRIPT\" --misra_rule \"$MISRA_RULE\" \
        --lang \"$LANGUAGE\" $REUSE_FLAG"

    # --- Step 2: Statistical comparison ---
    echo
    echo "-- Step 2: Comparison --"

    file_path="$DIR_REHARNESSED/${MODEL_NAME}_reharnessed_explain_t${TIME}.jsonl"
    if [ -f "$file_path" ]; then
        COMPARE_CSV="$DIR_REPORTS/${MODEL_NAME}_comparison_t${TIME}.csv"
        COMPARE_DETAIL_CSV="$DIR_REPORTS/${MODEL_NAME}_comparison_detail_t${TIME}.csv"
        COMPARE_MD="$DIR_REPORTS/${MODEL_NAME}_comparison_t${TIME}.md"
        COMPARE_REPORT_TXT="$DIR_REPORTS/${MODEL_NAME}_comparison_summary_t${TIME}.txt"

        eval "python3 src/pipeline/refine/statistic_refine_compare.py \
            --start_index $START_INDEX --end_index $END_INDEX \
            --origin_jsonl \"$DETAIL_IN\" \
            --refined_files \"$file_path\" \
            --refined_labels explain \
            --detailed_out_csv \"$COMPARE_DETAIL_CSV\" \
            --misra_json \"$MISRA_JSON\" \
            --out_md \"$COMPARE_MD\" \
            --out_csv \"$COMPARE_CSV\"" | tee "$COMPARE_REPORT_TXT"

        echo "==== [$MODEL_NAME] done. Reports: $DIR_REPORTS ===="
    else
        echo "Error: Result file not found for [$MODEL_NAME]: $file_path"
    fi
done

echo "==== All model pipelines completed ===="