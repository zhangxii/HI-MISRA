#!/bin/bash
# ======================================================================
# Knowledge Base Build Script — Batch generate LLM explanations
# for MISRA C rules
# ======================================================================
# Usage:
#   bash srcipt/knowledge_build/explain.sh
#
# Prerequisites:
#   - conda activate cppcheck
#   - data/misra/misra.txt rule file must exist
# ======================================================================

# --- Environment Setup ---
conda activate cppcheck
cd /root/autodl-tmp/workspace/hi-misra/
export PYTHONPATH=./src

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

# --- Configuration ---
MODEL_FAMILY="openai"          # Options: openai, deepseek, gemini, claude
RULE_FILE="data/misra/misra.txt"
OUTPUT_FILE="data/misra/misra_explaination.json"
WORKERS=8

echo "=========================================="
echo " MISRA Knowledge Base Batch Generation"
echo " Model: ${MODEL_FAMILY}"
echo " Rule File: ${RULE_FILE}"
echo " Output: ${OUTPUT_FILE}"
echo " Workers: ${WORKERS}"
echo "=========================================="

python src/knowledge_build/explain_misra.py \
  --model_family "${MODEL_FAMILY}" \
  --rule_file "${RULE_FILE}" \
  --output_file "${OUTPUT_FILE}" \
  --workers "${WORKERS}"

echo "=========================================="
echo " Generation Done!"
echo "=========================================="
