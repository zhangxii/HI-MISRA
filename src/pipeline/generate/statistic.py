import json
import argparse
import os, csv
import re
from collections import Counter, defaultdict
from utils.logger_util import get_logger, setup_logging, color_message
from pipeline.generate.harness import is_misra_pass

def get_core_id(rule_id: str) -> str:
    """Extract the core numeric part from a full rule ID, e.g. 'misra-c2012-15.6' -> '15.6'"""
    if not rule_id:
        return ""
    m = re.search(r'(\d+\.\d+)', rule_id)
    return m.group(1) if m else rule_id

def make_markdown_violation_dist_table(distribution, ids, max_ids_to_show=5):
    """Generate remaining violation count distribution table."""
    headers = ["Remaining Violations", "Task Count", f"Example Task IDs (up to {max_ids_to_show})"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--:"] * len(headers)) + " |"]
    total_non_compliant_tasks = sum(distribution.values())
    for viol_count, task_count in sorted(distribution.items()):
        example_ids = ids.get(viol_count, [])
        ids_str = ", ".join(example_ids[:max_ids_to_show])
        if len(example_ids) > max_ids_to_show: ids_str += ", ..."
        lines.append(f"| {viol_count} | {task_count} | `{ids_str}` |")
    lines.append(f"\nAnalysis: Distribution of tasks with few remaining violations (near-compliant) shown above.")
    return "\n".join(lines)

def make_markdown_blocking_rule_table(counter, stat_threshold):
    """Generate table of blocking rules that prevent compliance."""
    headers = ["Blocking Rule ID", "Occurrence Count"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--", ":--:"]) + " |"]
    total_violations = sum(counter.values())
    for rule_id, count in counter.most_common():
        lines.append(f"| {rule_id} | {count} |")
    lines.append(f"\nAnalysis: {total_violations} total violations found in tasks with <= {stat_threshold} remaining violations. Above are the primary rules blocking full compliance.")
    return "\n".join(lines)

def save_misra_non_compliant_summary(non_compliant_ids, evaluated_jsonl, out_csv):
    """Save detailed violation info for each non-compliant task."""
    rows = []
    with open(evaluated_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("ID") in non_compliant_ids or obj.get("task_id") in non_compliant_ids:
                misra_violations = obj.get("misra_violations") or []
                for v in misra_violations:
                    if v.get("rule_id") not in ("Compliant", "CheckFailed"):
                        rows.append({
                            "ID": obj.get("ID") or obj.get("task_id"),
                            "rule_id": v.get("rule_id"),
                            "rule_description": v.get("rule_description"),
                            "line": v.get("line"),
                            "code": v.get("code"),
                            "message": v.get("message"),
                        })
    if not rows: return
    with open(out_csv, "w", newline='', encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

def save_misra_rule_count(misra_rule_counter, out_csv):
    """Save global statistics for all violated rules."""
    with open(out_csv, "w", newline='', encoding="utf-8") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(["rule_id", "count"])
        for rule_id, count in sorted(misra_rule_counter.items(), key=lambda x: -x[1]):
            writer.writerow([rule_id, count])

def save_task_complaint_summary(rows, out_csv):
    """Save per-task summary of three key metrics: compile, test, compliance."""
    if not rows: return
    headers = ["ID", "compile", "test", "compliance"]
    with open(out_csv, "w", newline='', encoding="utf-8") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)

@get_logger
def main(logger, args):
    # --- 1. Initialize all statistics variables ---
    total = 0
    compile_ok = 0
    pass_ok = 0
    ids_fail = []

    all_passed = 0
    misra_compliant = 0
    misra_non_compliant_real = 0
    misra_check_failed = 0
    misra_failed_ids = []
    misra_non_compliant_ids = []
    misra_rule_counter = Counter()
    
    misra_total_real_violations = 0
    misra_violation_density = Counter()
    misra_severity_counter = Counter()
    misra_total_line_density_sum = 0.0

    stat_threshold = 5  
    almost_compliant_dist = Counter()      
    almost_compliant_ids = defaultdict(list) 
    blocking_rule_counter = Counter()      

    task_status_rows = [] 

    # --- 2. Load MISRA rule severity mapping ---
    misra_category_map = {}
    if args.misra_json and os.path.exists(args.misra_json):
        logger.info(f"Loading MISRA category map from {args.misra_json}")
        with open(args.misra_json, 'r', encoding='utf-8') as f:
            rules_data = json.load(f)
            for rule in rules_data:
                core_id = get_core_id(rule.get('id', ''))
                if core_id:
                    misra_category_map[core_id] = rule.get('category')
    else:
        logger.warning("MISRA JSON not provided or not found. Severity statistics will be unavailable.")

    # --- 3. Iterate through evaluated.jsonl ---
    detail_jsonl_path = os.path.splitext(args.out_json)[0] + "_detail.jsonl"
    with open(detail_jsonl_path, "w", encoding="utf-8") as detail_out, \
         open(args.evaluated_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            if not line.strip(): continue
            obj = json.loads(line)
            total += 1
            task_id = obj.get("ID") or obj.get("task_id")

            # Basic metrics
            is_compile_success = obj.get("compile_success", False)
            is_test_passed = obj.get("test_passed", False)

            if is_compile_success: compile_ok += 1
            if is_test_passed: pass_ok += 1
            else: ids_fail.append(task_id)

            # --- MISRA statistics logic ---
            misra_violations = obj.get("misra_violations") or []
            is_check_failed = any(v.get("rule_id") == "CheckFailed" for v in misra_violations)

            is_compliant_bool = False 

            if is_check_failed:
                misra_check_failed += 1
                misra_failed_ids.append(task_id)
                misra_rule_counter["CheckFailed"] += 1
            elif not misra_violations or all(v.get("rule_id") == "Compliant" for v in misra_violations):
                misra_compliant += 1
                is_compliant_bool = True
            else:
                misra_non_compliant_real += 1
                misra_non_compliant_ids.append(task_id)
                
                n_viol = 0
                current_task_short_rules = []

                for v in misra_violations:
                    rule_id = v.get("rule_id")
                    if rule_id and rule_id != "Compliant":
                        n_viol += 1
                        misra_rule_counter[rule_id] += 1
                        
                        core_id = get_core_id(rule_id)
                        if core_id not in misra_category_map:
                            print("Unknown severity rule_id:", rule_id, "core_id:", core_id)
                        category = misra_category_map.get(core_id, "Unknown")
                        misra_severity_counter[category] += 1
                        
                        if core_id:
                            current_task_short_rules.append(core_id)
                
                misra_total_real_violations += n_viol
                if n_viol > 0:
                    misra_violation_density[n_viol] += 1
                
                llm_code = obj.get("LLM_Code", obj.get("gen", {}).get("LLM_Code", ""))
                if llm_code and n_viol > 0:
                    lines_of_code = len(llm_code.splitlines())
                    if lines_of_code > 0:
                        task_line_density = n_viol / lines_of_code
                        misra_total_line_density_sum += task_line_density
                
                if 0 < n_viol <= stat_threshold:
                    almost_compliant_dist[n_viol] += 1
                    almost_compliant_ids[n_viol].append(task_id)
                    for r in current_task_short_rules:
                        blocking_rule_counter[r] += 1
            
            # === Collect CSV row data for current task ===
            task_status_rows.append({
                "ID": task_id,
                "compile": is_compile_success,
                "test": is_test_passed,
                "compliance": is_compliant_bool
            })
            # Count tasks that pass all three checks
            if is_compile_success and is_test_passed and is_compliant_bool:
                all_passed += 1

            detail = {
                "ID": task_id,
                "compile_success": is_compile_success,
                "compile_stderr": obj.get("compile_stderr", ""),
                "test_passed": is_test_passed,
                "pass_ratio": obj.get("pass_ratio"),
                "cases": obj.get("cases", []),
                "misra_violations": misra_violations,
                "LLM_Code": obj.get("LLM_Code", obj.get("gen", {}).get("LLM_Code", "")),
                "problem-description": obj.get("problem-description", obj.get("description", "")),
                "description": obj.get("description", ""),
                "input": obj.get("input", ""),
                "output": obj.get("output", ""),
                "sample-test": obj.get("sample-test", {}),
            }
            detail_out.write(json.dumps(detail, ensure_ascii=False) + "\n")

    # --- For tasks with violation count 1..5, compute top 10 violated rules ---
    almost_compliant_top_rules_dict = {}
    for n in range(1, stat_threshold + 1):
        ids = almost_compliant_ids.get(n, [])
        rule_counter = Counter()
        if not ids:
            continue
        with open(args.evaluated_jsonl, "r", encoding="utf-8") as ftmp:
            for line in ftmp:
                obj = json.loads(line)
                task_id = obj.get("ID") or obj.get("task_id")
                if task_id not in ids:
                    continue
                misra_violations = obj.get("misra_violations") or []
                for v in misra_violations:
                    rule_id = v.get("rule_id")
                    if rule_id and rule_id not in ("Compliant", "CheckFailed"):
                        rule_counter[rule_id] += 1
        almost_compliant_top_rules_dict[str(n)] = dict(rule_counter.most_common(10))

    # --- 4. Build final summary ---
    avg_density = (misra_total_real_violations / misra_non_compliant_real) if misra_non_compliant_real > 0 else 0.0
    avg_line_density = (misra_total_line_density_sum / misra_non_compliant_real) if misra_non_compliant_real > 0 else 0.0

    violation_dist_md = ""
    blocking_rule_md = ""
    if almost_compliant_dist:
        violation_dist_md = make_markdown_violation_dist_table(almost_compliant_dist, almost_compliant_ids)
    if blocking_rule_counter:
        blocking_rule_md = make_markdown_blocking_rule_table(blocking_rule_counter, stat_threshold)

    summary = {
        "total": total,
        "compile_test_compliance_all_passed": all_passed,
        "compile_success_rate": compile_ok / total if total else 0.0,
        "pass_rate": pass_ok / total if total else 0.0,
        "failed_ids": ids_fail[:10],
        "misra_compliant": misra_compliant,
        "misra_non_compliant": misra_non_compliant_real,
        "misra_check_failed": misra_check_failed,
        "misra_compliant_rate": misra_compliant / total if total else 0.0,
        "misra_total_violations": misra_total_real_violations,
        "misra_average_violation_density": round(avg_density, 2),
        "misra_average_line_density": round(avg_line_density, 4), 
        "misra_violation_density": dict(sorted(misra_violation_density.items())),
        "misra_severity_counter": dict(sorted(misra_severity_counter.items(), key=lambda x: -x[1])),
        "misra_rule_counter": dict(sorted(misra_rule_counter.items(), key=lambda x: -x[1])),
        "misra_non_compliant_ids": misra_non_compliant_ids[:10],
        "misra_failed_ids": misra_failed_ids[:10],
        "analysis_almost_compliant_dist": dict(sorted(almost_compliant_dist.items())),
        "analysis_blocking_rules": dict(blocking_rule_counter.most_common(20)),
        "analysis_almost_compliant_top_rules": almost_compliant_top_rules_dict
    }

    with open(args.out_json, "w", encoding="utf-8") as out:
        json.dump(summary, out, ensure_ascii=False, indent=2)
    
    logger.info(json.dumps(summary, ensure_ascii=False, indent=2))
    logger.info(f"Statistics saved to: {args.out_json}")
    logger.info(f"Detailed results saved to: {detail_jsonl_path}")

    if violation_dist_md:
        print(f"\n# Near-Compliant Analysis: Distribution of tasks with <= {stat_threshold} remaining violations\n")
        print(violation_dist_md)
    
    if blocking_rule_md:
        print(f"\n# Blocking Rules Analysis: Rules appearing in near-compliant tasks (Top Blocking Rules)\n")
        print(blocking_rule_md)

    # Output top 10 violated rules for each violation count
    for n in range(1, stat_threshold + 1):
        group = almost_compliant_top_rules_dict.get(str(n), {})
        if group:
            print(f"\n## Top 10 Violated Rules for Remaining Violations = {n}\n")
            lines = ["| Violated Rule | Count |", "| :-- | :--: |"]
            for rule_id, cnt in group.items():
                lines.append(f"| {rule_id} | {cnt} |")
            lines.append(f"\nTop 10 violated rules for all tasks with remaining violations = {n}.")
            print('\n'.join(lines))

    # --- 5. Save CSV reports ---
    output_dir = os.path.dirname(args.out_json)
    non_compliant_csv = os.path.join(output_dir, "misra_non_compliant_summary.csv")
    rule_count_csv = os.path.join(output_dir, "misra_rule_count.csv")
    task_complaint_csv = os.path.join(output_dir, "task_complaint_summary.csv")

    save_misra_non_compliant_summary(misra_non_compliant_ids, args.evaluated_jsonl, non_compliant_csv)
    save_misra_rule_count(misra_rule_counter, rule_count_csv)
    save_task_complaint_summary(task_status_rows, task_complaint_csv)

    logger.info(f"Non-compliant details saved to: {non_compliant_csv}")
    logger.info(f"Rule count statistics saved to: {rule_count_csv}")
    logger.info(f"Task status summary saved to: {task_complaint_csv}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluated_jsonl", required=True)
    ap.add_argument("--out_json", required=True)
    ap.add_argument("--misra_json", help="Path to the MISRA rules explanation JSON file (e.g., misra-c-2012.json)")
    args = ap.parse_args()
    setup_logging("log", "statistic.log")
    main(args)
