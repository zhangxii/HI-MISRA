"""
======================================================================
check_misra.py — MISRA Knowledge Base Quality Check and Retry Tool
======================================================================
Function: Check the quality of entries in the generated misra_explaination.json file,
and regenerate entries with parse failures, missing fields, or specified IDs via LLM.

Usage:
    cd /root/autodl-tmp/workspace/hi-misra
    export PYTHONPATH=./src

    # Auto-detect and retry all abnormal entries
    python src/knowledge_build/check_misra.py \
        --model_family openai \
        --data_file data/misra/misra_explaination.json

    # Specify ID list for regeneration
    python src/knowledge_build/check_misra.py \
        --model_family openai \
        --data_file data/misra/misra_explaination.json \
        --retry_id "1.3,2.2,8.4"

Migrated from: /root/autodl-tmp/workspace/misra-extend/src/check_misra.py
Adaptation: reuse hi-misra's model_config and utils.logger_util,
       import generate_rule_details / show_explanation from sibling module explain_misra
"""

import argparse
import sys
import json

from utils.logger_util import setup_logging, get_logger, color_message
from model_config import MODELS_CONFIG
from knowledge_build.explain_misra import generate_rule_details, show_explanation


def is_bad_explanation(explanation: dict) -> bool:
    """
    Determine if a rule's explanation is of insufficient quality.

    Failure criteria:
    - explanation is empty or None
    - Contains 'error' or 'parse_error' field (LLM call or JSON parse failed)
    - missing 'detailed_explanation', 'counter_example', 'correct_example' key fields
    """
    if not explanation or 'error' in explanation or 'parse_error' in explanation:
        return True
    for key in ["detailed_explanation", "counter_example", "correct_example"]:
        if key not in explanation:
            return True
    return False


@get_logger
def check_quality(logger, data_file):
    """
    Scan the explanation file and report quality statistics.

    :param data_file: misra_explaination.json file path
    :return: (good_count, bad_count, bad_ids)
    """
    with open(data_file, "r", encoding="utf-8") as f:
        explanations = json.load(f)

    good_count = 0
    bad_count = 0
    bad_ids = []

    for item in explanations:
        rule_id = item.get("id", "unknown")
        expl = item.get("explanation", {})
        if is_bad_explanation(expl):
            bad_count += 1
            bad_ids.append(rule_id)
            logger.warning(f"[BAD] Rule {rule_id}: {list(expl.keys()) if expl else 'empty'}")
        else:
            good_count += 1

    logger.info(f"Quality check complete: {good_count} good, {bad_count} bad out of {len(explanations)} total.")
    if bad_ids:
        logger.info(color_message(f"Bad rule IDs: {', '.join(bad_ids)}", "yellow"))

    return good_count, bad_count, bad_ids


@get_logger
def retry_bad_explanations(logger, args):
    """
    Regenerate explanations, only for abnormal or specified IDs.

    Process:
    1. Read existing explanations file
    2. Determine entries to retry (auto-detect + manual specification)
    3. Call LLM to regenerate
    4. Overwrite back to original file
    """
    # 1. Parse model configuration
    if args.model_family not in MODELS_CONFIG:
        logger.error(f"Model family '{args.model_family}' not found in MODELS_CONFIG.")
        sys.exit(1)
    model_conf = MODELS_CONFIG[args.model_family]

    # 2. Parse retry_id
    target_id_list = None
    if hasattr(args, "retry_id") and args.retry_id and args.retry_id.strip():
        target_id_list = [x.strip() for x in args.retry_id.split(",") if x.strip()]

    # 3. Read explanations file
    with open(args.data_file, "r", encoding="utf-8") as f:
        explanations = json.load(f)

    updated = 0
    for item in explanations:
        need_retry = False
        if target_id_list:
            # Manually specified ID list
            if item.get("id") in target_id_list:
                need_retry = True
        else:
            # Auto-detect abnormal entries
            if is_bad_explanation(item.get("explanation", {})):
                need_retry = True
        if need_retry:
            logger.info(color_message(f"Retrying rule: {item.get('id')}", "red"))
            rule_obj = {
                "id": item.get("id"),
                "type": item.get("type"),
                "content": item.get("content")
            }
            new_explanation = generate_rule_details(rule_obj, model_conf)
            show_explanation(new_explanation)
            item["explanation"] = new_explanation
            updated += 1

    with open(args.data_file, "w", encoding="utf-8") as f:
        json.dump(explanations, f, ensure_ascii=False, indent=2)
    logger.info(f"Retry complete, {updated} explanations updated in {args.data_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="MISRA knowledge base quality check and retry tool."
    )
    parser.add_argument("--model_family", type=str, default="openai",
                        help="model family key in MODELS_CONFIG (default: openai).")
    parser.add_argument("--data_file", type=str, default="data/misra/misra_explaination.json",
                        help="Path to the explanations JSON file.")
    parser.add_argument("--retry_id", type=str, default="",
                        help="Comma-separated rule IDs to retry, e.g. '1.3,2.2'. "
                             "If empty, auto-detect and retry all bad explanations.")
    parser.add_argument("--check_only", action="store_true",
                        help="Only run quality check without retrying.")
    args = parser.parse_args()

    setup_logging("log/knowledge_build", "check_misra.log")

    if args.check_only:
        check_quality(args.data_file)
    else:
        retry_bad_explanations(args)
