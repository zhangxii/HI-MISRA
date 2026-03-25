#!/bin/bash

# Unified environment
export LANG=C.UTF-8
export LC_ALL=C.UTF-8
export PYTHONIOENCODING=utf-8

# Suppress output for non-interactive terminals
if [ ! -t 1 ]; then
  export NO_COLOR=1
  export TQDM_DISABLE=1
  export TERM=dumb
fi

set -e
set -o pipefail

# ============== Model Configuration ==============
MODEL_FAMILYS=("openai" "openai" "gemini" "claude" "deepseek")
MODEL_NAMES=("gpt-5-mini" "gpt-5" "gemini-2.5-flash" "claude-3-7-sonnet-20250219" "deepseek-chat")

# ============== Parameters and defaults ==============
if [ "$#" -eq 0 ]; then
    echo "Error: Please provide at least one model index as argument"
    echo "Usage: $0 <index1> [index2] ..."
    echo "Example: $0 0 2"
    exit 1
fi

MODEL_INDEX_LIST=("$@")

LANGUAGE="c"
START_INDEX=0
END_INDEX=100
WORKERS=32
INPUT_JSON="data/codeflowbench/codeflowbench_sample.json"

# ============== Main pipeline ==============
for MODEL_INDEX in "${MODEL_INDEX_LIST[@]}"; do
  MODEL_FAMILY=${MODEL_FAMILYS[$MODEL_INDEX]}
  MODEL_NAME=${MODEL_NAMES[$MODEL_INDEX]}

  # Top-level directory
  DATA_DIR="data/codeflowbench/${LANGUAGE}/${MODEL_NAME}"

  # Generation directories
  DIR_INFER="$DATA_DIR/base_gen"
  DIR_TMP="$DIR_INFER/tmp"
  DIR_COMBINE="$DIR_INFER/combine"

  # Evaluation and statistics directories
  DIR_HARNESS="$DIR_INFER/harness"
  DIR_STAT="$DIR_INFER/statistic"

  mkdir -p "$DIR_TMP" "$DIR_COMBINE" "$DIR_HARNESS" "$DIR_STAT"

  # Normalized file names
  COMBINED_JSONL="$DIR_COMBINE/${MODEL_NAME}_${LANGUAGE}.jsonl"
  EVAL_JSONL="$DIR_HARNESS/${MODEL_NAME}_${LANGUAGE}_evaluated.jsonl"
  STAT_JSON="$DIR_STAT/${MODEL_NAME}_${LANGUAGE}_stat.jsonl"
  MISRA_JSON="data/misra/misra-c-2012.json"

  echo
  echo "=========================================================="
  echo "Processing Model: [$MODEL_NAME]  Language: [$LANGUAGE]"
  echo "Workdirs:"
  echo "  infer/tmp:     $DIR_TMP"
  echo "  infer/combine: $DIR_COMBINE"
  echo "  harness:       $DIR_HARNESS"
  echo "  statistic:     $DIR_STAT"
  echo "=========================================================="
  echo

  # -------- Step 1: infer.py — Generate candidate code --------
  CMD1=$(cat <<EOF
python3 src/pipeline/generate/infer.py \
  --model_family "$MODEL_FAMILY" \
  --model_name "$MODEL_NAME" \
  --input_path "$INPUT_JSON" \
  --save_dir "$DIR_TMP" \
  --lang "$LANGUAGE" \
  --left "$START_INDEX" \
  --right "$END_INDEX" \
  --n_proc "$WORKERS" \
  --reuse
EOF
)
  echo "==== Step 1: [$MODEL_NAME] Generate [$LANGUAGE] code ===="
  echo "$CMD1"
  eval "$CMD1"

  # -------- Step 2: combine.py — Merge temporary results to jsonl --------
  CMD2=$(cat <<EOF
python3 src/pipeline/generate/combine.py \
  --temp_dir "$DIR_TMP" \
  --model_name "$MODEL_NAME" \
  --output_file "$COMBINED_JSONL" \
  --left "$START_INDEX" \
  --right "$END_INDEX"
EOF
)
  echo
  echo "==== Step 2: [$MODEL_NAME]-[$LANGUAGE] Merge temporary files ===="
  echo "$CMD2"
  eval "$CMD2"

  # -------- Step 3: harness.py — Compile and evaluate --------
  CMD3=$(cat <<EOF
python3 src/pipeline/generate/harness.py \
  --in_json "$COMBINED_JSONL" \
  --out_jsonl "$EVAL_JSONL" \
  --timeout 10 \
  --lang "$LANGUAGE" \
  --workers "$WORKERS" \
  --reuse 
EOF
)
  echo
  echo "==== Step 3: [$MODEL_NAME] Compile and evaluate ===="
  echo "$CMD3"
  eval "$CMD3"

  # -------- Step 4: statistic.py — Statistical summary --------
  CMD4=$(cat <<EOF
python3 src/pipeline/generate/statistic.py \
  --evaluated_jsonl "$EVAL_JSONL" \
  --out_json "$STAT_JSON" \
  --misra_json "$MISRA_JSON"
EOF
)
  echo
  echo "==== Step 4: [$MODEL_NAME] Statistical results ===="
  echo "$CMD4"
  eval "$CMD4"

  echo
  echo "==== [$MODEL_NAME] Pipeline done ===="
  echo "Outputs:"
  echo "  Combined: $COMBINED_JSONL"
  echo "  Evaluated: $EVAL_JSONL"
  echo "  Statistic: $STAT_JSON"
  echo
done

echo "==== All model pipelines completed ===="
