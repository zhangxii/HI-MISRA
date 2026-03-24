"""
======================================================================
explain_misra.py — MISRA C 知识库批量Generate工具
======================================================================
Function: Read MISRA C rule text file, call LLM to generate structured explanations for each rule,
including detailed explanations, counter examples, and correct examples.

Usage：
    cd /root/autodl-tmp/workspace/hi-misra
    export PYTHONPATH=./src
    python src/knowledge_build/explain_misra.py \
        --model_family openai \
        --rule_file data/misra/misra.txt \
        --output_file data/misra/misra_explaination.json \
        --workers 8

Migrated from：/root/autodl-tmp/workspace/misra-extend/src/explain_misra.py
Adaptation: reuse hi-misra's model_config (MODELS_CONFIG + call_model) and utils.logger_util
"""

from openai import OpenAI
import argparse
import sys
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

from utils.logger_util import setup_logging, get_logger, color_message
from model_config import MODELS_CONFIG


@get_logger
def show_explanation(logger, explanation):
    """Display the generated result for a rule"""
    if not explanation or "error" in explanation or "parse_error" in explanation:
        logger.warning(f"Explanation has error: {explanation}")
        return
    logger.info(color_message(
        f"detailed_explanation: \n{explanation.get('detailed_explanation', 'N/A')}", "blue"))
    ce = explanation.get('counter_example', {})
    logger.info(color_message(
        f"counter_example: \n{ce.get('code', 'N/A')}\n"
        f"  violate_reason: \n{ce.get('violate_reason', 'N/A')}", "yellow"))
    co = explanation.get('correct_example', {})
    logger.info(color_message(
        f"correct_example: \n{co.get('code', 'N/A')}\n"
        f"  fixed_reason: \n{co.get('fixed_reason', 'N/A')}", "green"))


@get_logger
def generate_rule_details(logger, rule_obj, model_conf) -> dict:
    """
    Call LLM with a structured rule and return a structured explanation (JSON format).

    :param rule_obj: {"id": ..., "type": ..., "content": ...}
    :param model_conf: Model Configuration字典，包含 api_key, base_url, model_name
    :return: Explanation dictionary
    """
    # Construct prompt
    system_content = (
        "You are an expert in the MISRA C coding standard. "
        "Given the following rule (with id, type, and content), "
        "please perform these tasks and output your answer strictly in JSON:"
    )
    user_content = f"""
Rule:
ID: {rule_obj.get('id')}
Type: {rule_obj.get('type')}
Content: {rule_obj.get('content')}

Output format (strictly JSON, without markdown):

{{
  "detailed_explanation": "A clear and thorough explanation of the rule's intent, rationale, and how it applies to C programming.",
  "counter_example": {{
    "code": "A minimal C code snippet that violates the rule. Use proper formatting and comments.",
    "violate_reason": "A brief explanation of why the above code violates the rule."
  }},
  "correct_example": {{
    "code": "A corrected C code snippet that complies with the rule. Use proper formatting and comments.",
    "fixed_reason": "A brief explanation of how this code fixes the violation."
  }}
}}

Strict requirements:
- Only output a valid JSON object, without any markdown or extra text.
- Each field must be filled out as described above.
"""
    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": user_content}
    ]

    logger.debug(f"Prompt for LLM:\n{user_content}")
    explanation = {}
    try:
        client = OpenAI(
            api_key=model_conf["api_key"],
            base_url=model_conf["base_url"]
        )
        # Call LLM
        response = client.chat.completions.create(
            model=model_conf["model_name"],
            messages=messages,
            temperature=1.0,
            timeout=1200,
        )
        response_text = response.choices[0].message.content or ""
        logger.info(f"Raw response from LLM (len={len(response_text)}):\n{response_text}")

        # Try to extract JSON from response (compatible with/without markdown wrapping)
        json_str = response_text.strip()
        if json_str.startswith("```"):
            json_str = json_str.lstrip("`")
            # Remove ```json header and trailing ```
            if "json" in json_str[:10].lower():
                json_str = json_str[4:].strip()
            if "```" in json_str:
                json_str = json_str.split("```")[0].strip()
        try:
            explanation = json.loads(json_str)
        except Exception as e:
            logger.error(f"Failed to parse explanation JSON: {e}\nResponse was:\n{response_text}")
            explanation = {"parse_error": str(e), "raw_response": response_text}
        show_explanation(explanation)
    except Exception as e:
        logger.error(f"[Error] Failed to process Rule {rule_obj.get('id')}: {e}")
        explanation = {"error": str(e)}
    return explanation


@get_logger
def read_rules_from_file(logger, filepath):
    """
    Read MISRA rule file into a structured list.
    Assumes 2 lines per rule: 1. Rule 1.1 Required  2. Description. Separated by blank lines.

    :param filepath: 规则files路径 (如 data/misra/misra.txt)
    :return: Rule list [{"id": ..., "type": ..., "content": ...}, ...]
    """
    rules = []
    with open(filepath, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    idx = 0
    # Regex directly captures the ID part
    pattern = re.compile(r"^Rule\s+(\d+\.\d+)\s+(Required|Advisory|Mandatory)$", re.I)
    while idx < len(lines):
        m = pattern.match(lines[idx])
        if m:
            rule_id = m.group(1)      # Only the numeric ID
            rule_type = m.group(2)
            if idx + 1 < len(lines):
                rule_content = lines[idx + 1]
                rules.append({
                    "id": rule_id,
                    "type": rule_type,
                    "content": rule_content
                })
                idx += 2
            else:
                logger.warning(f"Rule {rule_id} has no description!")
                idx += 1
        else:
            idx += 1

    logger.info(f"Loaded {len(rules)} rules from {filepath}")
    return rules


@get_logger
def batch_explanation(logger, args):
    """
    Batch generate LLM explanations for all MISRA rules.

    Process:
    1. Parse model configuration
    2. Read rule file
    3. 并发Call LLM Generate解释
    4. Save every 20 entries to prevent data loss on interruption
    """
    # 1. Parse model configuration
    model_family = args.model_family
    if model_family not in MODELS_CONFIG:
        logger.error(f"Model family '{model_family}' not found in MODELS_CONFIG.")
        sys.exit(1)
    model_conf = MODELS_CONFIG[model_family]

    # 2. Read rules
    if not args.rule_file:
        logger.error("You must provide --rule_file.")
        sys.exit(1)
    rules = read_rules_from_file(args.rule_file)
    explanations = [{}] * len(rules)  # Pre-allocate to ensure order

    # 3. Process and save results (concurrent + progress bar)
    max_workers = min(args.workers, len(rules))
    futures = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor, \
         tqdm(total=len(rules), desc="Generating explanations") as pbar:
        for idx, rule in enumerate(rules):
            futures.append(executor.submit(generate_rule_details, rule, model_conf))
        for idx, future in enumerate(as_completed(futures)):
            i = futures.index(future)  # Retrieve original order index
            try:
                explanation = future.result()
                flat_result = {**rules[i], **{"explanation": explanation}}
                explanations[i] = flat_result
            except Exception as e:
                logger.error(f"[Error] Failed to process Rule {i+1}: {e}")
                explanations[i] = {**rules[i], "explanation": {}, "error": str(e)}
            pbar.update(1)
            # Save every 20 entries to prevent data loss
            if (idx + 1) % 20 == 0 or (idx + 1) == len(rules):
                with open(args.output_file, "w", encoding="utf-8") as f:
                    json.dump(explanations, f, ensure_ascii=False, indent=2)
                logger.info(f"Progress: {idx+1}/{len(rules)} explanations saved.")

    logger.info(f"All explanations saved to {args.output_file}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch MISRA rule explanation with LLM.")
    parser.add_argument("--model_family", type=str, default="openai",
                        help="model family key in MODELS_CONFIG (default: openai).")
    parser.add_argument("--rule_file", type=str, default="data/misra/misra.txt",
                        help="MISRA rule text file path.")
    parser.add_argument("--output_file", type=str, default="data/misra/misra_explaination.json",
                        help="Output JSON file path for generated explanations.")
    parser.add_argument("--workers", type=int, default=8,
                        help="Number of concurrent workers (default: 8).")
    args = parser.parse_args()
    setup_logging("log/knowledge_build", "explain_misra.log")
    batch_explanation(args)
