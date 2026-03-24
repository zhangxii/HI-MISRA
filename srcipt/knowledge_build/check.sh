#!/bin/bash
# ======================================================================
# Knowledge Base Quality Check and Retry Script
# ======================================================================
# Usage:
#   # Check quality only
#   bash srcipt/knowledge_build/check.sh --check_only
#
#   # Auto-detect and retry all abnormal entries
#   bash srcipt/knowledge_build/check.sh
#
#   # Retry specific IDs
#   bash srcipt/knowledge_build/check.sh --retry_id "1.3,2.2,8.4"
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
MODEL_FAMILY="openai"
DATA_FILE="data/misra/misra_explaination.json"

echo "=========================================="
echo " MISRA Knowledge Base Quality Check/Retry"
echo " Model: ${MODEL_FAMILY}"
echo " Data File: ${DATA_FILE}"
echo "=========================================="

# Pass all extra arguments to the Python script
python src/knowledge_build/check_misra.py \
  --model_family "${MODEL_FAMILY}" \
  --data_file "${DATA_FILE}" \
  "$@"

echo "=========================================="
echo " Done!"
echo "=========================================="
