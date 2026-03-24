import os
import json
import argparse
import re
import sys
import traceback
import tempfile

from concurrent.futures import ProcessPoolExecutor, as_completed
from tqdm import tqdm
from collections import defaultdict, OrderedDict
from openai import OpenAI
from utils.logger_util import get_logger, setup_logging, color_message
from pipeline.refine.refine import save_results
from utils.refine_util import extract_code
from model_config import MODELS_CONFIG, call_model

from check.misra_check import run_cppcheck_misra
import subprocess
from pipeline.generate.harness import (
    run_compiler, 
    run_tester, 
    build_io_tests_from_sample,
    run_harness_misra_check
)
from pipeline.generate.statistic import get_core_id
from pipeline.generate.harness import is_misra_pass


@get_logger
def pre_misra_autofix(logger, rid, code, violations):
    """
    Apply deterministic fixes for high-frequency, safe rules based on MISRA C:2012 analysis.
    Target rules: 15.6, 14.4, 17.7.
    """
    lines = code.split('\n')
    fixed_rule_ids = set()
    
    # Build a mapping from line number to violated rule core_ids
    line_to_rules_map = defaultdict(set)
    for v in violations:
        rule_id = v.get("rule_id", "")
        line_num = v.get("line")
        match = re.search(r'(\d+(\.\d+)+)', rule_id)
        if match and line_num is not None:
            core_id = match.group(1)
            line_to_rules_map[line_num].add(core_id)

    # --- Rule 15.6 (add braces) ---
    for i in range(len(lines)):
        if "15.6" in line_to_rules_map.get(i + 1, set()):
            line = lines[i]
            match = re.match(r'^(\s*)(if|while|for)(\s*\(.*\))\s*([^;{}]+\s*;\s*)$', line)
            if match:
                indent, keyword, condition, statement = match.groups()
                lines[i] = f"{indent}{keyword}{condition} {{\n{indent}    {statement.strip()}\n{indent}}}"
                fixed_rule_ids.add("15.6")

    # --- Rule 14.4 (boolean condition) ---
    for i in range(len(lines)):
        if "14.4" in line_to_rules_map.get(i + 1, set()):
            line = lines[i]
            match = re.match(r'^(\s*)(if|while|for)\s*\(\s*([a-zA-Z_]\w*)\s*\)(.*)$', line)
            if match:
                indent, keyword, var, rest = match.groups()
                lines[i] = f"{indent}{keyword} ({var} != 0){rest}"
                fixed_rule_ids.add("14.4")

    # --- Rule 17.7 (unused return value) ---
    for i in range(len(lines)):
        if "17.7" in line_to_rules_map.get(i + 1, set()):
            line = lines[i]
            match = re.match(r'^(\s*)([a-zA-Z_]\w*\s*\(.*\);)\s*$', line)
            if match:
                indent, statement = match.groups()
                lines[i] = f"{indent}(void){statement}"
                fixed_rule_ids.add("17.7")

    if fixed_rule_ids:
        logger.info(f"[{rid}] L1 Autofix applied for rules: {sorted(list(fixed_rule_ids))}")
        
    return "\n".join(lines)


@get_logger
def make_prompt(logger, problem_type, description, current_code, issue_detail, lang, rule_id=None):
    """Create the appropriate prompt based on problem type."""
    lang_name = "C" if lang == "c" else "C++"
    standard = "C11" if lang == "c" else "C++17"
    code_block_lang = "c" if lang == "c" else "cpp"
    
    base_user_instruction = f'''Please revise the {lang_name} code below to fix the listed issues.
Return the complete, corrected code in a single Markdown block:
```{code_block_lang}
// revised code here
```'''
    
    user = f'''
[Description]
{description}

[Current Code]
```cpp
{current_code}
```
'''

    if problem_type == "compile":
        msg = f"You are a helpful {lang_name} assistant expert at fixing compilation errors."
        user += f"\n[Compilation Error]\n{issue_detail}\n"
    elif problem_type == "test":
        msg = f"You are a helpful {lang_name} assistant expert at fixing logical errors in code."
        user += f"\n[Failed Test Cases]\n{issue_detail}\n"
    elif problem_type == "misra":
        is_multiple = rule_id and "," in rule_id
        rules_str = "Rules" if is_multiple else "Rule"
        focus_str = "these rules" if is_multiple else "this rule"
        specific_str = "these specific rules" if is_multiple else "this specific rule"

        msg = (
            f"You are a precise {lang_name} MISRA compliance expert.\n"
            f"Your current task is to fix the violations related to MISRA {rules_str} `{rule_id}`.\n"
            f"Focus exclusively on {focus_str}. Do not modify any other part of the code.\n"
            f"Make the absolute minimum changes required for compliance with {specific_str}.\n"
            "Strictly preserve all other logic and program structure."
        )
        user += f"\n[MISRA Violations to Fix for {rules_str} `{rule_id}`]\n{issue_detail}\n"
    else:
        msg = f"You are a helpful {lang_name} assistant."
        user += f"\n[Identified Issues]\n{issue_detail}\n"

    final_user_prompt = base_user_instruction + user
    return msg, final_user_prompt


def define_knowledge_strategy():
    """Define the knowledge injection strategy for different MISRA rules."""
    strategy = {
        # Enhanced - comprehensive info including rationale, examples, amplification
        "10.8": ["rationale", "example"],
        "11.5": ["rationale", "example"],
        "14.4": ["rationale", "example", "amplification"],
        "15.1": ["rationale", "example"],
        "15.5": ["rationale", "example", "amplification"],
        "21.3": ["rationale", "example", "amplification"],
        "21.6": ["rationale", "example", "amplification"],

        # Stubborn rules
        "8.2": ["example", "amplification"],
        "8.4": ["rationale", "example", "amplification"],
        "12.1": ["rationale", "example", "amplification"],
        "12.3": ["rationale", "example", "amplification"],
        "17.7": ["rationale", "example", "amplification"],
        "17.8": ["rationale", "example", "amplification"],

        # Pointers, memory, and type conversions
        "10.1": ["rationale", "example"],
        "10.3": ["rationale", "example"],
        "10.4": ["rationale", "example"],
        "11.1": ["rationale", "example"],
        "11.3": ["rationale", "example"],
        "11.4": ["rationale", "example"],
        "11.8": ["rationale", "example"],
        "18.1": ["rationale", "example"],
        
        # Expressions and side effects
        "13.2": ["rationale", "example"],
        "13.5": ["rationale", "example"],
        
        # Code structure and control flow
        "15.6": ["rationale", "example", "amplification"],
        "15.7": ["rationale", "example"],
        "16.1": ["rationale", "example"],
        "16.4": ["rationale", "example"],

        # Preprocessor
        "20.7": ["rationale", "example"],
        
        # Critical safety and undefined behavior
        "1.3": ["rationale", "amplification"],
        "9.1": ["rationale", "example"],
        "17.2": ["rationale"],

        # Default strategy
        "default": ["rationale"]
    }
    return strategy

def get_misra_explanation(item, field_list):
    """
    Extract specified fields from a MISRA rule item and format as a Markdown blockquote.

    :param item: Single MISRA rule dict (from misra-c-2012.json).
    :param field_list: List of knowledge fields to include, e.g., ["rationale", "example"].
    :return: Formatted Markdown string.
    """
    if not item or not field_list:
        return ""

    content_parts = []
    lang_for_example = "c"

    for field in field_list:
        val = item.get(field)
        if val and val.strip():
            header = f"**[{field.capitalize()}]**"
            if field == "example":
                body = f"```{lang_for_example}\n{val.strip()}\n```"
            else:
                body = val.strip()
            content_parts.append(f"{header}\n{body}")

    if not content_parts:
        return ""

    full_content = "\n\n".join(content_parts)
    formatted_lines = [f"> {line}".rstrip() for line in full_content.split('\n')]
    return "\n".join(formatted_lines)


@get_logger
def select_rule_to_fix(logger, violations, misra_explanations_map, excluded_rules=None):
    """
    Select the next batch of rules to fix from the violation list.
    Strategy:
    1. Separate "active" and "cooled-down" violations.
    2. Build a base fix batch from active violations via group scoring (cap at threshold).
    3. Fill spare capacity from the cooldown queue with highest-severity violations.
    """
    if not violations:
        return []
    if excluded_rules is None:
        excluded_rules = set()

    SCORE_MAP = {"Mandatory": 3, "Required": 2, "Advisory": 1}
    VIOLATION_THRESHOLD = 20

    # Step 1: Split violations into active and cooled-down
    active_violations = []
    cooled_violations = []
    for v in violations:
        core_id = get_core_id(v.get("rule_id"))
        if core_id in excluded_rules:
            cooled_violations.append(v)
        else:
            active_violations.append(v)

    # Step 2: Build the base fix batch from active violations
    final_rules_set = set()
    batched_groups_info = []
    total_violations_in_batch = 0

    if active_violations:
        group_stats = defaultdict(lambda: {"score": 0, "count": 0, "rules": set()})
        for v in active_violations:
            core_id = get_core_id(v.get("rule_id"))
            major_group_id = core_id.split('.')[0]
            category = misra_explanations_map.get(core_id, {}).get("category", "Unknown")
            score = SCORE_MAP.get(category, 0)
            group_stats[major_group_id]["score"] += score
            group_stats[major_group_id]["count"] += 1
            group_stats[major_group_id]["rules"].add(core_id)
        
        if group_stats:
            sorted_groups = sorted(group_stats.items(), key=lambda item: item[1]['score'], reverse=True)
            
            top_group_id, top_group_stats = sorted_groups[0]
            if top_group_stats['count'] > VIOLATION_THRESHOLD:
                final_rules_set.update(top_group_stats['rules'])
                batched_groups_info.append(f"`{top_group_id}.x` (Violations: {top_group_stats['count']})")
                total_violations_in_batch = top_group_stats['count']
            else:
                for group_id, stats in sorted_groups:
                    if total_violations_in_batch + stats['count'] > VIOLATION_THRESHOLD:
                        continue
                    final_rules_set.update(stats['rules'])
                    batched_groups_info.append(f"`{group_id}.x` (Violations: {stats['count']})")
                    total_violations_in_batch += stats['count']
            
            if not batched_groups_info and sorted_groups:
                top_group_id, top_group_stats = sorted_groups[0]
                final_rules_set.update(top_group_stats['rules'])
                batched_groups_info.append(f"`{top_group_id}.x` (Violations: {top_group_stats['count']})")
                total_violations_in_batch = top_group_stats['count']

    # Step 3: Fill spare capacity from cooldown queue
    spare_capacity = VIOLATION_THRESHOLD - total_violations_in_batch
    if spare_capacity > 0 and cooled_violations:
        cooled_violations.sort(
            key=lambda v: SCORE_MAP.get(misra_explanations_map.get(get_core_id(v.get("rule_id")), {}).get("category"), 0),
            reverse=True
        )
        violations_added_from_cooldown = 0
        added_from_cooldown_set = set()
        for v in cooled_violations:
            if violations_added_from_cooldown >= spare_capacity:
                break
            core_id = get_core_id(v.get("rule_id"))
            final_rules_set.add(core_id)
            added_from_cooldown_set.add(core_id)
            violations_added_from_cooldown += 1
        if violations_added_from_cooldown > 0:
            total_violations_in_batch += violations_added_from_cooldown

    if not final_rules_set:
        return []

    chosen_rules = sorted(list(final_rules_set))
    logger.info(f"==> Targeted rules: {chosen_rules}")
    return chosen_rules


@get_logger
def run_real_misra_checker(logger, rid, code_to_check, lang, args):
    """
    Run a real MISRA check on the given code string.
    Returns list of non-compliant violations, or None if the check failed.
    """
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            cpp_path = os.path.join(tmpdir, f"main.{lang}")
            with open(cpp_path, "w", encoding="utf-8") as f:
                f.write(code_to_check)
            
            all_violations = run_cppcheck_misra(
                cpp_path, args.misra_script, args.misra_rule
            )
            non_compliant = [
                v for v in all_violations 
                if v.get("rule_id") not in ("Compliant", "CheckFailed")
            ]
            return non_compliant

    except Exception as e:
        logger.error(f"[{rid}] Real-time MISRA check failed: {e}")
        return None


@get_logger 
def handle_compile_errors(logger, obj, args, client, code, stderr):
    """Iteratively fix compilation errors using LLM."""
    history = []
    rid = obj.get("ID")
    desc = obj.get("problem-description") or obj.get("description", "")
    model = args.model_name
    lang = args.lang
    for i in range(args.max_retries):
        sys_msg, user_msg = make_prompt("compile", desc, code, stderr, lang)
        raw_res = call_model(client, [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}], model, 0.2, 1200) or ""
        new_code = extract_code(raw_res)
        history.append({"stage": "compile", "attempt": i+1, "prompt": user_msg, "response": raw_res})

        if not new_code: 
            continue

        is_success, new_stderr = run_compiler(rid, new_code, lang)
        if is_success:
            logger.info(f"[{rid}] Compile-Fix succeeded after {i+1} attempts.")
            return True, new_code, "", history
        
        code, stderr = new_code, new_stderr

    logger.error(f"[{rid}] Compile-Fix failed after {args.max_retries} attempts.")
    return False, code, stderr, history


@get_logger
def handle_test_failures(logger, rid, client, model, desc, code, io_tests, lang, args, initial_summary=None):
    """
    Iteratively fix test-failing code using a greedy strategy.
    Only accepts fixes that improve the test pass rate.
    """
    history = []
    
    # Ablation switches
    is_full_mode = not args.ablation or "full" in args.ablation
    guard_compile = is_full_mode or "no_compile_guard" not in args.ablation
    guard_test_soft = is_full_mode or "no_test_guard" not in args.ablation

    # Initialize best code and pass rate
    best_code_so_far = code
    _, initial_summary, initial_cases = run_tester(rid, best_code_so_far, io_tests, args)
    initial_passed_count = sum(1 for case in initial_cases if case.get('ok'))
    best_pass_rate = initial_passed_count / len(io_tests) if io_tests else 0.0
    current_failed_summary = initial_summary
    logger.info(f"[{rid}] Test-Fix initialized. Pass rate: {best_pass_rate:.2%} ({initial_passed_count}/{len(io_tests)})")

    for i in range(args.max_retries):
        # Call LLM for fix
        sys_msg, user_msg = make_prompt("test", desc, best_code_so_far, current_failed_summary, lang)
        raw_res = call_model(client, [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}], model, 0.4, 1200) or ""
        new_code = extract_code(raw_res)
        history.append({"stage": "test", "attempt": i+1, "prompt": user_msg, "response": raw_res})

        if not new_code:
            continue
        
        # Compilation guard
        if guard_compile:
            compile_ok, compile_err = run_compiler(rid, new_code, lang)
            if not compile_ok:
                continue 
            
        # Evaluate test pass rate
        is_success, new_summary, new_cases = run_tester(rid, new_code, io_tests, args)
        
        if is_success:
            logger.info(f"[{rid}] Test-Fix fully succeeded after attempt #{i+1}!")
            return True, new_code, history
        
        new_passed_count = sum(1 for case in new_cases if case.get('ok'))
        new_pass_rate = new_passed_count / len(io_tests) if io_tests else 0.0
        
        # Decision: accept or reject new code
        if not guard_test_soft:
            best_code_so_far = new_code
            best_pass_rate = new_pass_rate
            current_failed_summary = new_summary
        else:
            if new_pass_rate > best_pass_rate:
                logger.info(f"[{rid}] Improvement: pass rate {best_pass_rate:.2%} -> {new_pass_rate:.2%}")
                best_code_so_far = new_code
                best_pass_rate = new_pass_rate
                current_failed_summary = new_summary

    # Loop ended
    is_success, _, final_cases = run_tester(rid, best_code_so_far, io_tests, args)
    return is_success, best_code_so_far, history


def short_msg(v: dict, limit: int = 220) -> str:
    """Extract and truncate the raw message from a violation dict."""
    msg = v.get("raw_message") or v.get("rule_description") or ""
    msg = re.sub(r'^\[[^\]]+\]\s*', "", msg)
    msg = re.sub(r'\s*\[[^\]]+\]\s*$', "", msg)
    msg = msg.strip()
    if len(msg) > limit:
        msg = msg[:limit - 1] + "…"
    return msg


@get_logger
def handle_misra_violations(logger, client, code, misra_expl, args, record):
    """
    Layered MISRA violation repair pipeline.
    L1: Deterministic autofix. L2: LLM iterative fix with guards.
    """
    history = []
    current_code = code
    lang = args.lang
    rid = record.get("ID")
    obj = dict(record)
    model = args.model_name
    desc = obj.get("problem-description") or obj.get("description", "")

    # Ablation switches
    is_full_mode = not args.ablation or "full" in args.ablation
    use_priority = is_full_mode or ("no_kepr" not in args.ablation and "no_priority" not in args.ablation)
    use_knowledge = is_full_mode or ("no_kepr" not in args.ablation and "no_knowledge" not in args.ablation)
    use_deterministic = is_full_mode or ("no_kepr" not in args.ablation and "no_deterministic" not in args.ablation)
    guard_compile = is_full_mode or "no_compile_guard" not in args.ablation
    guard_test_soft = is_full_mode or "no_test_guard" not in args.ablation
    guard_misra_soft = is_full_mode or "no_misra_guard" not in args.ablation
    use_cooling = is_full_mode or "no_cooling" not in args.ablation
    
    knowledge_strategy = define_knowledge_strategy()
    if not use_knowledge:
        knowledge_strategy = {"default": []}

    # --- Phase 1: L1 Deterministic autofix ---
    remaining_violations = run_harness_misra_check(obj, current_code, args)
    if use_deterministic:
        fixed_code_l1 = pre_misra_autofix(rid, current_code, remaining_violations)
        if not guard_compile:
            current_code = fixed_code_l1
        else:
            is_compiled, _ = run_compiler(rid, fixed_code_l1, lang)
            if is_compiled:
                current_code = fixed_code_l1
            else:
                logger.warning(f"[{rid}] L1 Autofix rejected (broke compilation).")

            remaining_violations = run_harness_misra_check(obj, current_code, args)
            if not remaining_violations:
                return True, current_code, history
        
    # --- Phase 2: L2 LLM iterative fix ---
    iteration_count = 0
    cooldown_rules = set()
    max_retries = min(max(args.max_retries, len(remaining_violations), 10), 20)
    consecutive_no_improvement = 0
    
    while remaining_violations and iteration_count < max_retries:
        iteration_count += 1
        violations_before_fix = len(remaining_violations)

        # Triage: prioritize CheckFailed
        check_failed_violation = next((v for v in remaining_violations if v.get("rule_id") == "CheckFailed"), None)
        rules_to_fix = []
        is_checkfailed_fix = False

        if check_failed_violation:
            is_checkfailed_fix = True
            rules_to_fix = ["CheckFailed"]
        else:
            if use_priority:
                rules_to_fix = select_rule_to_fix(remaining_violations, misra_expl, cooldown_rules)
            else:
                # Naive strategy: take first N distinct core_ids in order
                seen = OrderedDict()
                for v in remaining_violations:
                    cid = get_core_id(v.get("rule_id", ""))
                    if cid and cid not in seen and cid not in cooldown_rules:
                        seen[cid] = True
                    if len(seen) >= 20:
                        break
                rules_to_fix = list(seen.keys())

        if not rules_to_fix:
            break
       
        # Self-correction loop: attempt to find a valid fix for selected rules
        MAX_CORRECTION_ATTEMPTS = max(args.max_retries, 3)
        is_fix_accepted = False
        accepted_code = ""

        for attempt in range(MAX_CORRECTION_ATTEMPTS):
            if is_checkfailed_fix:
                error_message = check_failed_violation.get('raw_message', 'Unknown parsing error.')
                issue_detail = (
                    f"The static analysis tool failed to parse the code (CheckFailed).\n"
                    f"This is likely due to a syntax error.\n"
                    f"Please fix the code based on the following error message:\n\n"
                    f"```\n{error_message}\n```"
                )
                rules_str = "Cpp check dump failed!"
            else:   
                rules_str = ", ".join(rules_to_fix)
                # Build hybrid prompt
                pairs = [(v, get_core_id(v.get("rule_id", ""))) for v in remaining_violations]
                other_violations = [v for v, rid in pairs if rid not in rules_to_fix]
                
                # Primary issue
                primary_issue_parts = [f"**Rules to Fix: `{rules_str}`**"]
                violations_by_rule = defaultdict(list)
                for v, rid in pairs:
                    if rid in rules_to_fix:
                        violations_by_rule[rid].append(v)
                
                for rule_id in rules_to_fix:
                    violations_for_this_rule = violations_by_rule[rule_id]
                    if not violations_for_this_rule: 
                        continue
                    primary_issue_parts.append(f"\n---\n### Fixing Rule: `{rule_id}`")
                    primary_issue_parts.append(f"**Description**: {violations_for_this_rule[0].get('rule_description', '')}")
                    primary_issue_parts.extend([f"* Location: line {v.get('line', '?')} | Code: `{v.get('code', '')}`" for v in violations_for_this_rule])
                    
                    # Knowledge injection
                    fields_to_use = knowledge_strategy.get(rule_id, knowledge_strategy["default"])
                    if rule_id in misra_expl and fields_to_use:
                        explanation = get_misra_explanation(misra_expl[rule_id], fields_to_use)
                        if explanation:
                            primary_issue_parts.append(f"\n**Rule Explanation (Fields: {', '.join(fields_to_use)}):**\n{explanation}")

                    # Context: other existing violations
                    context_parts = [
                        "\n---\n**For Context: Other Existing Violations**\n",
                        "> Your primary goal is to fix the rules listed above. Avoid changes that worsen these other violations."
                    ]
                    grouped = defaultdict(list)
                    for v in other_violations:
                        rule_id = v.get("rule_id", "")
                        grouped[(get_core_id(rule_id), rule_id)].append(v)

                    for (cid, full_rid), items in grouped.items():
                        seen = set(); lines = []
                        for v in items:
                            s = short_msg(v)
                            if s in seen: continue
                            seen.add(s)
                            where = f"line {v.get('line')}" if v.get("line") is not None else ""
                            lines.append(f">• {s} {where}".rstrip())
                        header = f">* Rule `{full_rid or cid}`"
                        context_parts.append(header)
                        context_parts.extend(lines)

                    issue_detail = "\n".join(primary_issue_parts) + "\n" + "\n".join(context_parts)

            # Call LLM
            sys_msg, user_msg = make_prompt("misra", desc, current_code, issue_detail, lang, rule_id=rules_str)
            raw_res = call_model(client, [{"role": "system", "content": sys_msg}, {"role": "user", "content": user_msg}], model, 0.2, 1200) or ""
            new_code = extract_code(raw_res)
            history.append({"stage": "misra", "iteration": iteration_count, "attempt": attempt + 1, "rule_fixed": rules_to_fix, "prompt": user_msg, "response": raw_res})

            if not new_code:
                break 

            # Guard checks
            compile_ok, compile_err = run_compiler(rid, new_code, lang)
            if not guard_compile:
                compile_ok = True
            if not compile_ok:
                continue
            
            io_tests = build_io_tests_from_sample(obj.get("sample-test") or {})
            test_ok, failed_summary, _ = run_tester(rid, new_code, io_tests, args)
            if not guard_test_soft:
                 test_ok = True
            if not test_ok:
                continue
            
            is_fix_accepted = True
            accepted_code = new_code
            break 

        # Update state based on fix result
        if is_fix_accepted and accepted_code:
            new_violations = run_harness_misra_check(obj, accepted_code, args)
            
            improvement_found = False
            if not guard_misra_soft:
                improvement_found = True
            else:
                if new_violations is None:
                    pass
                elif is_checkfailed_fix:
                    if not any(v.get("rule_id") == "CheckFailed" for v in new_violations):
                        improvement_found = True
                else:
                    has_new_checkfailed = any(v.get("rule_id") == "CheckFailed" for v in new_violations)
                    if has_new_checkfailed:
                        logger.error(f"[{rid}] REGRESSION: fix introduced CheckFailed. Rejecting.")
                    elif len(new_violations) < violations_before_fix:
                        improvement_found = True
                        logger.info(f"[{rid}] Violations reduced: {violations_before_fix} -> {len(new_violations)}")

            if improvement_found:
                current_code = accepted_code
                remaining_violations = new_violations
                consecutive_no_improvement = 0
            else:
                if use_cooling:
                    cooldown_rules.update(rules_to_fix)
                consecutive_no_improvement += 1
        else:
            if use_cooling:
                cooldown_rules.update(rules_to_fix)
            consecutive_no_improvement += 1

        # Check for stagnation
        improvement_tries = getattr(args, 'stagnation_threshold', 5)
        if consecutive_no_improvement >= improvement_tries:
            logger.error(f"[{rid}] Stagnation detected ({improvement_tries} iterations). Aborting MISRA-Fix.")
            break

    final_violations_count = len(remaining_violations) if remaining_violations is not None else -1
    is_success = final_violations_count == 0
    return is_success, current_code, history


def unified_iterative_refine(conf, record, misra_explanations, args):
    """Main per-record refinement: compile fix -> test fix -> MISRA fix."""
    import logging
    logger = logging.getLogger("refine-single")
    obj = dict(record)
    model_name = args.model_name
    lang = args.lang
    rid = obj.get("ID", "UNKNOWN")

    current_code = obj.get("LLM_Code", "")
    description = obj.get("problem-description") or obj.get("description", "")
    client = OpenAI(base_url=conf["base_url"], api_key=conf["api_key"])
    history = []

    # Prepare test cases
    io_tests = build_io_tests_from_sample(obj.get("sample-test") or {})

    # --- Stage 1: Compile fix ---
    try:
        if not obj.get("compile_success"):
            compile_stderr = obj.get("compile_stderr", "")
            success, current_code, stderr, stage_history = handle_compile_errors(
                obj, args, client, current_code, compile_stderr
            )
            history.extend(stage_history)
            obj['Refined_Code'] = current_code
            obj['iteration_history'] = history 
            obj['compile_stderr'] = stderr
            obj['compile_success'] = success
            if not success:
                obj['refine_status'] = "compile_failed"
                return obj
    except Exception as e:
        logger.error(f"[{rid}] Exception during Compile-Fix: {e}")
        obj.update({"refine_status": "compile_exception", "iteration_history": history})
        return obj
    
    # --- Stage 2: Test fix ---
    try:
        test_ok, failed_summary, detailed_cases = run_tester(rid, current_code, io_tests, args)
        obj['cases'] = detailed_cases
        if io_tests:
            passed_count = sum(1 for case in detailed_cases if case.get('ok'))
            total_cases = len(detailed_cases)
            obj['test_passed'] = (passed_count == total_cases)
            obj['pass_ratio'] = passed_count / total_cases if total_cases > 0 else 1.0
        else:
            obj['test_passed'] = True
            obj['pass_ratio'] = 1.0

        if not obj['test_passed']:
            success, current_code, stage_history = handle_test_failures(
                rid, client, model_name, description, current_code, io_tests, lang, args, initial_summary=failed_summary
            )
            history.extend(stage_history)
            if not success:
                obj.update({"Refined_Code": current_code, "refine_status": "test_failed", "iteration_history": history})
                return obj
    except Exception as e:
        logger.error(f"[{rid}] Exception during Test-Fix: {e}")
        obj.update({"refine_status": "test_exception", "iteration_history": history})
        return obj

    # --- Stage 3: MISRA compliance fix ---
    try:
        initial_misra_violations = run_harness_misra_check(obj, current_code, args)
        if initial_misra_violations is None:
            logger.error(f"[{rid}] MISRA check failed. Cannot proceed.")
        elif not initial_misra_violations:
            pass  # Already compliant
        else:
            success, current_code, stage_history = handle_misra_violations(
                client, current_code, misra_explanations, args, record
            )
            new_misra_violations = run_harness_misra_check(obj, current_code, args)
            history.extend(stage_history)

        obj.update({"Refined_Code": current_code, "iteration_history": history})
        obj['refine_status'] = "misra_incomplete" if not is_misra_pass(obj=None, viol=new_misra_violations) else "all_stages_passed"
    except Exception as e:
        logger.error(f"[{rid}] Exception during MISRA-Fix: {e}", exc_info=True)
        obj.update({"refine_status": "misra_exception", "iteration_history": history})
        return obj
    return obj


def filter_by_ids(records, target_ids):
    """Filter records by a list of target IDs."""
    if isinstance(target_ids, str):
        target_ids = [target_ids]
    target_id_set = set(target_ids)
    return [record for record in records if record.get("ID") in target_id_set]


@get_logger
def main(logger, args):
    """Main function orchestrating the refinement pipeline."""
    logger.info(f"Starting Code Refinement for {args.lang.upper()}")
    if args.ablation and "full" not in args.ablation:
        logger.info(f"Ablation modes: {', '.join(args.ablation)}")

    conf = MODELS_CONFIG[args.model_family]
    in_path = args.in_jsonl
    out_path = args.out_jsonl

    # Step 1: Load MISRA explanations
    misra_explanations_map = {}
    try:
        with open(args.misra_json, "r", encoding="utf-8") as f:
            explanations_data = json.load(f)
        for item in explanations_data:
            rule_id_str = item.get("id")
            if not rule_id_str: continue
            core_id_match = re.search(r'(\d+(\.\d+)*)', rule_id_str)
            if not core_id_match: continue
            core_id = core_id_match.group(1)
            misra_explanations_map[core_id] = item
        logger.info(f"Loaded {len(misra_explanations_map)} MISRA rule explanations.")
    except Exception as e:
        logger.error(f"Failed to load MISRA explanation file '{args.misra_json}': {e}", exc_info=True)
        return

    # Step 2: Read input data
    try:
        with open(in_path, "r", encoding="utf-8") as f:
            records = [json.loads(line) for line in f if line.strip()]
        logger.info(f"Loaded {len(records)} records from '{in_path}'.")
    except Exception as e:
        logger.error(f"Failed to read input file '{in_path}': {e}")
        return
    
    # Filter by test IDs or apply index slicing
    if args.test_ids:
        records = filter_by_ids(records, args.test_ids)
        if not records:
            logger.error("No matching records found for specified test IDs.")
    elif args.start_index != -1 or args.end_index != -1:
        sorted_records = sorted(records, key=lambda record: record.get("ID", ""))
        start = args.start_index if args.start_index != -1 else 0
        end = args.end_index if args.end_index != -1 else None
        records = sorted_records[start:end]

    # Step 3: Reuse existing results
    refined_map = {}
    if args.reuse and os.path.exists(out_path):
        with open(out_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    obj = json.loads(line)
                    if obj.get("ID") is not None:
                        refined_map[obj.get("ID")] = obj
        logger.info(f"Found {len(refined_map)} existing results to reuse.")
        
    results = [None] * len(records)
    id_to_idx = {rec.get("ID"): i for i, rec in enumerate(records)}
    if args.reuse:
        for rid, reused_obj in refined_map.items():
            if rid in id_to_idx:
                results[id_to_idx[rid]] = reused_obj

    max_workers = args.workers if args.workers > 0 else os.cpu_count()

    # Step 4: Concurrent refinement
    tasks_to_submit = [(idx, rec) for idx, rec in enumerate(records) if rec.get("ID") not in refined_map]
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                unified_iterative_refine, conf, record, misra_explanations_map, args
            ): idx for idx, record in tasks_to_submit
        }
        logger.info(f"Submitted {len(futures)} tasks for refinement.")

        if futures:
            processed_count = 0
            save_interval = 1
            for future in tqdm(as_completed(futures), total=len(futures), desc="Refining tasks"):
                try:
                    original_index = futures[future]
                    record_id_for_log = records[original_index].get("ID", f"UNKNOWN_INDEX_{original_index}") 
                    result = future.result()
                    results[original_index] = result
                    processed_count += 1
                    if processed_count % save_interval == 0 and processed_count < len(futures):
                        save_results(results, out_path, color_message)
                except Exception as e:
                    logger.error(f"Error processing {record_id_for_log}: {e}", exc_info=True)

    # Step 5: Save final output
    save_results(results, out_path, color_message)
    
    # Step 6: Final statistics
    all_pass_count = 0
    final_results = [r for r in results if r is not None]
    total_processed = len(final_results)

    if total_processed > 0:
        for result in final_results:
            final_compile_ok = result.get("compile_success", False)
            final_test_ok = result.get("test_passed", False)
            final_misra_ok = is_misra_pass(obj=result)
            if final_compile_ok and final_test_ok and final_misra_ok:
                all_pass_count += 1
        
        pass_rate_str = f"{(all_pass_count / total_processed):.2%}"
        logger.info(f"Final: {total_processed} tasks processed. All-pass (compile/test/MISRA): {all_pass_count}/{total_processed} ({pass_rate_str})")
    else:
        logger.warning("No records processed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Refine C code for compile, test, and MISRA compliance.")
    parser.add_argument("--model_family", default="openai", help="Model family (from model_config)")
    parser.add_argument("--model_name", default="gpt-5-mini", help="Model name")
    parser.add_argument("--lang", default="c", choices=['c', 'cpp'], help="Language (c or cpp)")
    parser.add_argument("--in_jsonl", default="data/codeflowbench/c/gpt-5-mini/gpt-5-mini_c_stat_detail.jsonl", help="Input stat_detail.jsonl path")
    parser.add_argument("--out_jsonl", default="data/codeflowbench/c/gpt-5-mini/refined/gpt-5-mini_refined_explain.jsonl", help="Output refined JSONL path")
    parser.add_argument("--compare", type=str, default="loose", choices=['strict', 'loose'], help="Test output comparison mode")
    parser.add_argument("--misra_script", type=str, default="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/misra.py", help="MISRA check script path")
    parser.add_argument("--misra_rule", type=str, default="data/misra/misra-c-2012.txt", help="MISRA rule file path")
    parser.add_argument("--misra_json", default="data/misra/misra-c-2012.json", help="MISRA rules explanation JSON path")
    parser.add_argument("--workers", type=int, default=10, help="Number of concurrent processes")
    parser.add_argument("--reuse", action="store_true", help="Reuse existing results from output file")
    parser.add_argument("--start_index", type=int, default=0, help="Start sample index (inclusive), -1 for no limit")
    parser.add_argument("--end_index", type=int, default=10, help="End sample index (exclusive), -1 for no limit")
    parser.add_argument("--timeout", type=int, default=10.0, help="Timeout in seconds")
    parser.add_argument("--max_retries", type=int, default=3, help="Max retries per record")
    parser.add_argument("--test_ids", required=False, nargs='+', help="Filter to specific IDs (space-separated)")
    parser.add_argument(
        "--ablation", nargs="*", default=["full"],
        choices=["full", "no_kepr", "no_deterministic", "no_priority", "no_knowledge",
                 "no_compile_guard", "no_test_guard", "no_misra_guard", "no_cooling", "test"],
        help="Ablation modes (combinable). Empty or 'full' = full mode.",
    )
    parser.add_argument("--stagnation_threshold", type=int, default=5,
                        help="Consecutive no-improvement iterations before aborting MISRA fix (default: 5)")

    args = parser.parse_args()
    args.max_retries = 3
    setup_logging(log_dir="log/pipeline", log_file="refine_explain.log", level="info")  
    main(args)