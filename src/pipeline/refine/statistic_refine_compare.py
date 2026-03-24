import json
import argparse
import re
import csv
import os
from collections import Counter, defaultdict
from utils.logger_util import get_logger, setup_logging, color_message
from pipeline.generate.harness import is_misra_pass

def _pct(x, total):
    return f"{(x/total*100):.2f}%" if total else "0.00%"

def count_misra_violations(record):
    """Count total MISRA violations in a single record."""
    if not record:
        return 0
    violations_list = record.get("misra_violations_refined", record.get("misra_violations", []))
    if not isinstance(violations_list, list):
        return 0
    count = 0
    for v in violations_list:
        rule_id = v.get("rule_id")
        if rule_id and rule_id not in ("Compliant", "CheckFailed"):
            count += 1
    return count

def get_pass_ratio(record):
    """Get test case pass ratio from a record."""
    if not record:
        return 0.0
    pass_ratio = record.get("pass_ratio", 0.0)
    if isinstance(pass_ratio, (int, float)):
        return float(pass_ratio)
    cases = record.get("cases", [])
    if isinstance(cases, list) and cases:
        passed_count = sum(1 for c in cases if c.get("ok"))
        return passed_count / len(cases)
    return 0.0

def get_misra_violation_summary(record):
    """Get MISRA violation summary: violation count, or 'CheckFailed'."""
    if not record:
        return "N/A"
    violations_list = record.get("misra_violations_refined", record.get("misra_violations", []))
    if not isinstance(violations_list, list):
        return "Invalid Data"
    if any(v.get("rule_id") == "CheckFailed" for v in violations_list):
        return "CheckFailed"
    return count_misra_violations(record)

def get_core_id(rule_id: str) -> str:
    """Extract core numeric part from rule ID, e.g. 'misra-c2012-15.6' -> '15.6'."""
    if not rule_id:
        return ""
    m = re.search(r'(\d+\.\d+)', rule_id)
    return m.group(1) if m else rule_id

def collect_rule_violations(record, rule_counter, severity_counter, category_map):
    """Collect MISRA rule IDs and severities from a record, updating counters."""
    if not record:
        return
    violations_list = record.get("misra_violations_refined", record.get("misra_violations", []))
    if not isinstance(violations_list, list):
        return
    for v in violations_list:
        rule_id = v.get("rule_id")
        if rule_id and rule_id not in ("Compliant", "CheckFailed"):
            short_rule_id = (re.search(r'\d+\.\d+', rule_id) or re.search(r'.*', rule_id)).group(0)
            rule_counter[short_rule_id] += 1
            if category_map:
                core_id = get_core_id(rule_id)
                category = category_map.get(core_id, "Unknown")
                severity_counter[category] += 1

# ==================== Markdown table generation ====================
def make_markdown_summary_table(stats, labels, total):
    headers = ["Metric", "Before"] + [f"After ({lb})" for lb in labels]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--"] + [":--:" for _ in range(len(headers)-1)]) + " |"]
    def row(metric, key_base):
        lines.append("| " + " | ".join([metric, str(stats[f"{key_base}_pre"])] + [str(v) for v in stats[f"{key_base}_post"]]) + " |")
    row("Compile Pass", "compile")
    row("Test Pass", "test")
    row("MISRA Compliant", "misra")
    row("MISRA Violations Total", "misra_violations")
    row("All Three Pass", "all_pass")
    row("LLM Calls", "llm_calls")
    lines.append(f"\nSamples: {total}")
    return "\n".join(lines)

def make_markdown_ratio_table(stats, labels, total):
    headers = ["Metric", "Before"] + [f"After ({lb})" for lb in labels]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--"] + [":--:" for _ in range(len(headers)-1)]) + " |"]
    def row(metric, key_base, is_avg_ratio=False):
        pre_ratio = _pct(stats[f"{key_base}_pre"], total)
        post_ratios = [_pct(v, total) for v in stats[f"{key_base}_post"]]
        lines.append("| " + " | ".join([metric, pre_ratio] + post_ratios) + " |")
    row("Compile Pass Rate", "compile")
    row("Test Pass Rate", "test")
    row("Avg Test Case Pass Rate", "avg_test_pass_ratio", is_avg_ratio=True)
    row("MISRA Pass Rate", "misra")
    row("All Three Pass Rate", "all_pass")
    return "\n".join(lines)
    
def make_markdown_rule_dist_table(rule_stats, labels, top_n=20):
    pre_counter, post_counters = rule_stats['pre'], rule_stats['post']
    all_rule_ids = set(pre_counter.keys()).union(*(set(pc.keys()) for pc in post_counters))
    sort_counter, sort_label = pre_counter, "Before"
    try:
        sort_idx = labels.index('explain')
        sort_counter, sort_label = post_counters[sort_idx], "explain"
    except ValueError: pass
    sorted_rule_ids = sorted(list(all_rule_ids), key=lambda r: -sort_counter.get(r, 0))
    headers = ["Rule ID", "Before"] + [f"After ({lb})" for lb in labels]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--"] + [":--:"] * (len(headers) - 1)) + " |"]
    for rule_id in sorted_rule_ids[:top_n]:
        cells = [rule_id, str(pre_counter.get(rule_id, 0))] + [str(pc.get(rule_id, 0)) for pc in post_counters]
        lines.append("| " + " | ".join(cells) + " |")
    lines.append(f"\nNote: sorted by `After ({sort_label})` violations descending, showing Top {top_n}.")
    return "\n".join(lines)

def make_markdown_severity_dist_table(severity_stats, labels):
    pre_counter, post_counters = severity_stats['pre'], severity_stats['post']
    all_categories = set(pre_counter.keys()).union(*(set(pc.keys()) for pc in post_counters))
    
    def sort_key(category):
        if category == 'Required': return 0
        if category == 'Advisory': return 1
        if category == 'Mandatory': return 2
        if category == 'Unknown': return 99
        return 10
    
    sorted_categories = sorted(list(all_categories), key=sort_key)
    headers = ["Category", "Before"] + [f"After ({lb})" for lb in labels]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--"] + [":--:"] * (len(headers) - 1)) + " |"]
    for category in sorted_categories:
        if not category: continue
        cells = [category, str(pre_counter.get(category, 0))] + [str(pc.get(category, 0)) for pc in post_counters]
        lines.append("| " + " | ".join(cells) + " |")
    total_pre = sum(pre_counter.values())
    total_post = [sum(pc.values()) for pc in post_counters]
    total_cells = ["Total", f"{total_pre}"] + [f"{t}" for t in total_post]
    lines.append("| " + " | ".join(total_cells) + " |")
    return "\n".join(lines)

def make_markdown_violation_dist_table(distribution, ids, max_ids_to_show=5):
    headers = ["Remaining Violations", "Task Count", f"Example Task IDs (max {max_ids_to_show})"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--:"] * len(headers)) + " |"]
    total_non_compliant_tasks = sum(distribution.values())
    for viol_count, task_count in sorted(distribution.items()):
        example_ids = ids.get(viol_count, [])
        ids_str = ", ".join(example_ids[:max_ids_to_show])
        if len(example_ids) > max_ids_to_show: ids_str += ", ..."
        lines.append(f"| {viol_count} | {task_count} | `{ids_str}` |")
    lines.append(f"\nTotal non-compliant tasks: {total_non_compliant_tasks}")
    return "\n".join(lines)

def make_markdown_stubborn_rule_table(counter, stat_threshold):
    headers = ["Stubborn Rule ID", "Occurrences"]
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join([":--", ":--:"]) + " |"]
    total_violations = sum(counter.values())
    for rule_id, count in counter.most_common():
        lines.append(f"| {rule_id} | {count} |")
    lines.append(f"\nTotal stubborn violations (in tasks with < {stat_threshold} remaining): {total_violations}")
    return "\n".join(lines)

# ==================== CSV output ====================
def write_combined_csv(filepath, stats, labels, total_samples, rule_stats, 
                           severity_stats, misra_category_map,
                           violation_distribution, almost_compliant_ids, stubborn_rule_counter, 
                           stat_threshold, top_n=20, max_ids_to_show=5):
    """Write all analysis tables into a single CSV file."""
    try:
        with open(filepath, 'w', newline='', encoding='utf-8-sig') as f:
            writer = csv.writer(f)

            # 1. Summary statistics
            writer.writerow(["# Before/After Statistics"])
            headers_summary = ["Metric", "Before"] + [f"After ({lb})" for lb in labels]
            writer.writerow(headers_summary)
            summary_metrics = [
                ("Compile Pass", "compile"), ("Test Pass", "test"), ("MISRA Compliant", "misra"),
                ("MISRA Violations Total", "misra_violations"), ("All Three Pass", "all_pass"),
                ("LLM Calls", "llm_calls")
            ]
            for metric, key_base in summary_metrics:
                row = [metric, stats[f"{key_base}_pre"]] + stats[f"{key_base}_post"]
                writer.writerow(row)
            writer.writerow(["Total Samples", total_samples])
            writer.writerow([])

            # 2. Pass rate statistics
            writer.writerow(["# Pass Rate Improvement"])
            writer.writerow(headers_summary)
            ratio_metrics = [
                ("Compile Pass Rate", "compile"), ("Test Pass Rate", "test"),
                ("Avg Test Case Pass Rate", "avg_test_pass_ratio"),
                ("MISRA Pass Rate", "misra"), ("All Three Pass Rate", "all_pass")
            ]
            for metric, key_base in ratio_metrics:
                pre_val = stats[f"{key_base}_pre"]
                post_vals = stats[f"{key_base}_post"]
                row = [metric, _pct(pre_val, total_samples)] + [_pct(v, total_samples) for v in post_vals]
                writer.writerow(row)
            writer.writerow([])

            # 3. MISRA rule distribution
            sort_label = 'explain' if 'explain' in labels else 'Before'
            writer.writerow([f"# MISRA Rule Distribution (sorted by 'After ({sort_label})' Top {top_n})"])
            pre_counter, post_counters = rule_stats['pre'], rule_stats['post']
            all_rule_ids = set(pre_counter.keys()).union(*(set(pc.keys()) for pc in post_counters))
            sort_counter = pre_counter
            try:
                sort_idx = labels.index('explain')
                sort_counter = post_counters[sort_idx]
            except ValueError: pass
            sorted_rule_ids = sorted(list(all_rule_ids), key=lambda r: -sort_counter.get(r, 0))
            headers_rules = ["Rule ID", "Before"] + [f"After ({lb})" for lb in labels]
            writer.writerow(headers_rules)
            for rule_id in sorted_rule_ids[:top_n]:
                row = [rule_id, pre_counter.get(rule_id, 0)] + [pc.get(rule_id, 0) for pc in post_counters]
                writer.writerow(row)
            writer.writerow([])

            # 3.5. MISRA severity distribution
            if misra_category_map:
                writer.writerow(["# MISRA Violation Severity Distribution"])
                pre_counter, post_counters = severity_stats['pre'], severity_stats['post']
                all_categories = set(pre_counter.keys()).union(*(set(pc.keys()) for pc in post_counters))
                def sort_key(category):
                    if category == 'Required': return 0
                    if category == 'Advisory': return 1
                    if category == 'Mandatory': return 2
                    if category == 'Unknown': return 99
                    return 10
                sorted_categories = sorted(list(all_categories), key=sort_key)
                headers_severity = ["Category", "Before"] + [f"After ({lb})" for lb in labels]
                writer.writerow(headers_severity)
                for category in sorted_categories:
                    if not category: continue
                    row = [category, pre_counter.get(category, 0)] + [pc.get(category, 0) for pc in post_counters]
                    writer.writerow(row)
                total_pre = sum(pre_counter.values())
                total_post = [sum(pc.values()) for pc in post_counters]
                writer.writerow(["Total", total_pre] + total_post)
                writer.writerow([])

            # 4. Almost-compliant analysis
            if violation_distribution:
                writer.writerow([f"# Almost-Compliant Analysis: remaining violations after explain"])
                headers_viol = ["Remaining Violations", "Task Count", f"Example IDs (max {max_ids_to_show})"]
                writer.writerow(headers_viol)
                for viol_count, task_count in sorted(violation_distribution.items()):
                    example_ids = almost_compliant_ids.get(viol_count, [])
                    ids_str = ", ".join(example_ids[:max_ids_to_show])
                    if len(example_ids) > max_ids_to_show: ids_str += ", ..."
                    writer.writerow([viol_count, task_count, ids_str])
                writer.writerow([])
            
            # 5. Stubborn rule analysis
            if stubborn_rule_counter:
                writer.writerow([f"# Stubborn Rules: in tasks with < {stat_threshold} remaining violations"])
                headers_stubborn = ["Stubborn Rule ID", "Occurrences"]
                writer.writerow(headers_stubborn)
                for rule_id, count in stubborn_rule_counter.most_common():
                    writer.writerow([rule_id, count])
                writer.writerow([])
            return True
    except Exception:
        return False

@get_logger
def load_jsonl(logger, path):
    def get_record_id(record):
        return record.get("ID") or record.get("problem-id")
    if not path: return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = [json.loads(line) for line in f if line.strip()]
            return {get_record_id(obj): obj for obj in data if get_record_id(obj)}
    except FileNotFoundError:
        logger.error(f"File not found: {path}")
        return {}
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error in {path}: {e}")
        return {}

def get_error_summary(record):
    if not record: return "N/A"
    refine_status = record.get("refine_status")
    if refine_status == "skip_no_problem": return "Pass (Skipped)"
    if refine_status == "compile_failed": return "Compile Error"
    error_parts = []
    if not record.get("test_passed"):
        cases = record.get("cases", [])
        failed_cases = sum(1 for c in cases if not c.get("ok"))
        if cases: error_parts.append(f"Test Failed ({failed_cases}/{len(cases)})")
    violations_list = record.get("misra_violations_refined", record.get("misra_violations", []))
    if not is_misra_pass(obj=None, viol=violations_list):
        if any(v.get("rule_id") == "CheckFailed" for v in violations_list):
            error_parts.append("MISRA: Check Failed") 
        else:
            violations = [v['rule_id'] for v in violations_list if v.get("rule_id") not in ("Compliant")]
            if violations:
                short_counts = Counter((re.search(r'\d+\.\d+', k) or re.search(r'.*', k)).group(0) for k in violations)
                misra_str = ", ".join([f"{k}({v})" for k, v in sorted(short_counts.items())])
                error_parts.append(f"MISRA: {misra_str}")
    return " | ".join(error_parts) if error_parts else "Pass"

def generate_detailed_markdown(f, table_data):
    if not table_data: return
    headers = list(table_data[0].keys())
    f.write(f"| {' | '.join(headers)} |\n")
    f.write(f"| {' | '.join(['---'] * len(headers))} |\n")
    for row in table_data:
        values = [str(row.get(h, '')) for h in headers]
        f.write(f"| {' | '.join(values)} |\n")

@get_logger
def main(logger, args):
    if len(args.refined_files) != len(args.refined_labels):
        logger.error("--refined_files and --refined_labels must have the same count!")
        return

    # Load full pre_map, then apply index slicing
    pre_map_full = load_jsonl(args.origin_jsonl)
    pre_map = {}
    if args.start_index != -1 or args.end_index != -1:
        all_ids = list(pre_map_full.keys())
        total_full = len(all_ids)
        start_idx = args.start_index if args.start_index != -1 else 0
        end_idx = args.end_index if args.end_index != -1 else total_full
        py_start = max(0, start_idx)
        py_end = min(total_full, end_idx)
        sliced_ids = all_ids[py_start:py_end] if py_start < py_end else []
        pre_map = {pid: pre_map_full[pid] for pid in sliced_ids}
        logger.info(f"Index slicing applied: {len(pre_map)}/{total_full} samples (range [{py_start}, {py_end}))")
    else:
        pre_map = pre_map_full

    post_maps = [load_jsonl(p) for p in args.refined_files]
    labels = args.refined_labels

    # Load MISRA rule category mapping
    misra_category_map = {}
    if args.misra_json and os.path.exists(args.misra_json):
        try:
            with open(args.misra_json, 'r', encoding='utf-8') as f:
                rules_data = json.load(f)
                for rule in rules_data:
                    core_id = get_core_id(rule.get('id', ''))
                    if core_id:
                        misra_category_map[core_id] = rule.get('category', 'Unknown')
        except Exception as e:
            logger.error(f"Failed to load MISRA JSON {args.misra_json}: {e}")
            misra_category_map = {}

    # Find common IDs across all datasets
    common_ids = set(pre_map.keys())
    for post_map in post_maps:
        if post_map: common_ids.intersection_update(post_map.keys())
    
    logger.info(f"Comparing {len(common_ids)} common IDs across origin ({len(pre_map)}) and {len(labels)} refined sets")

    num_methods = len(labels)
    stats = defaultdict(lambda: [0.0] * num_methods) 
    stats.update({
        'compile_pre': 0, 'test_pre': 0, 'misra_pre': 0, 'all_pass_pre': 0, 
        'misra_violations_pre': 0, 'avg_test_pass_ratio_pre': 0.0
    })
    rule_stats = {'pre': Counter(), 'post': [Counter() for _ in range(num_methods)]}
    severity_stats = {'pre': Counter(), 'post': [Counter() for _ in range(num_methods)]}
    table_data = []
    dynamic_call_totals = defaultdict(float)

    for pid in sorted(list(common_ids)):
        pre_record = pre_map.get(pid)
        post_records = [pm.get(pid) for pm in post_maps]

        pre_compile, pre_test = bool(pre_record.get("compile_success")), bool(pre_record.get("test_passed"))
        pre_misra = is_misra_pass(obj=pre_record)
        pre_violations = count_misra_violations(pre_record)
        pre_pass_ratio = get_pass_ratio(pre_record)
        
        stats['compile_pre'] += pre_compile
        stats['test_pre'] += pre_test
        stats['misra_pre'] += pre_misra
        stats['all_pass_pre'] += (pre_compile and pre_test and pre_misra)
        stats['misra_violations_pre'] += pre_violations
        stats['avg_test_pass_ratio_pre'] += pre_pass_ratio
        collect_rule_violations(pre_record, rule_stats['pre'], severity_stats['pre'], misra_category_map)
        
        row_data = {"ID": pid, "Before": get_error_summary(pre_record), "Before_Violations": get_misra_violation_summary(pre_record)}

        for i, post_record in enumerate(post_records):
            if not post_record: continue

            post_compile, post_test = bool(post_record.get("compile_success")), bool(post_record.get("test_passed"))
            post_misra = is_misra_pass(obj=post_record)
            post_violations = count_misra_violations(post_record)
            post_pass_ratio = get_pass_ratio(post_record)

            stats['compile_post'][i] += post_compile
            stats['test_post'][i] += post_test
            stats['misra_post'][i] += post_misra
            stats['all_pass_post'][i] += (post_compile and post_test and post_misra)
            stats['misra_violations_post'][i] += post_violations
            stats['avg_test_pass_ratio_post'][i] += post_pass_ratio
            collect_rule_violations(post_record, rule_stats['post'][i], severity_stats['post'][i], misra_category_map)
            
            label = labels[i]
            row_data[f"After ({label})"] = get_error_summary(post_record)
            row_data[f"After ({label})_Violations"] = get_misra_violation_summary(post_record)

            # Accumulate LLM call counts from iteration history
            history = []
            if label == 'explain':
                history = post_record.get("iteration_history", [])
            elif label.startswith('iter'):
                history = post_record.get("history", [])
            if isinstance(history, list):
                dynamic_call_totals[label] += len(history)
        
        table_data.append(row_data)

    total_samples = len(common_ids)
    if total_samples == 0:
        logger.error("No common sample IDs found for comparison.")
        return

    # Calculate LLM call statistics
    fixed_calls_map = {'base': 1, 'intervenor': 2}
    llm_calls_post = []
    for label in labels:
        if label in fixed_calls_map:
            llm_calls_post.append(fixed_calls_map[label])
        elif label in dynamic_call_totals:
            avg_calls = (dynamic_call_totals[label] / total_samples)
            llm_calls_post.append(f"{avg_calls:.2f}")
        else:
            llm_calls_post.append("N/A")
    stats['llm_calls_pre'] = "N/A"
    stats['llm_calls_post'] = llm_calls_post

    # Almost-compliant and stubborn rule analysis (for 'explain' mode)
    stat_threshold = 5
    violation_dist_md, stubborn_rule_md = "", ""
    violation_distribution, stubborn_rule_counter = Counter(), Counter()
    almost_compliant_ids = defaultdict(list)
    try:
        explain_idx = labels.index('explain')
        explain_map = post_maps[explain_idx]
        for pid in sorted(list(common_ids)):
            record = explain_map.get(pid)
            if not record: continue
            n_viol = count_misra_violations(record)
            if n_viol > 0:
                violation_distribution[n_viol] += 1
                if n_viol <= stat_threshold:
                    almost_compliant_ids[n_viol].append(pid)
                if 1 <= n_viol < stat_threshold:
                    violations_list = record.get("misra_violations_refined", record.get("misra_violations", []))
                    for v in violations_list:
                        rule_id = v.get("rule_id")
                        if rule_id and rule_id not in ("Compliant", "CheckFailed"):
                            short_rule_id = (re.search(r'\d+\.\d+', rule_id) or re.search(r'.*', rule_id)).group(0)
                            stubborn_rule_counter[short_rule_id] += 1
        if violation_distribution:
            violation_dist_md = make_markdown_violation_dist_table(violation_distribution, almost_compliant_ids)
        if stubborn_rule_counter:
            stubborn_rule_md = make_markdown_stubborn_rule_table(stubborn_rule_counter, stat_threshold)
    except (ValueError, IndexError):
        pass

    # --- Print results ---
    sort_label = 'explain' if 'explain' in labels else 'Before'
    print("\n# Before/After Statistics\n")
    print(make_markdown_summary_table(stats, labels, total_samples))
    print("\n# Pass Rate Improvement\n")
    print(make_markdown_ratio_table(stats, labels, total_samples))
    print(f"\n# MISRA Rule Distribution (sorted by 'After ({sort_label})' Top 20)\n")
    print(make_markdown_rule_dist_table(rule_stats, labels))
    
    if misra_category_map:
        print(f"\n# MISRA Violation Severity Distribution\n")
        print(make_markdown_severity_dist_table(severity_stats, labels))

    if violation_dist_md:
        print(f"\n# Almost-Compliant Analysis: remaining violations after explain\n")
        print(violation_dist_md)
    if stubborn_rule_md:
        print(f"\n# Stubborn Rules: in tasks with < {stat_threshold} remaining violations\n")
        print(stubborn_rule_md)

    # --- Save Markdown report ---
    if args.out_md and table_data:
        try:
            with open(args.out_md, 'w', encoding='utf-8') as f:
                f.write("# Before/After Statistics\n\n")
                f.write(make_markdown_summary_table(stats, labels, total_samples) + "\n\n")
                f.write("# Pass Rate Improvement\n\n")
                f.write(make_markdown_ratio_table(stats, labels, total_samples) + "\n\n")
                f.write(f"# MISRA Rule Distribution (sorted by 'After ({sort_label})' Top 20)\n\n")
                f.write(make_markdown_rule_dist_table(rule_stats, labels) + "\n\n")
                if misra_category_map:
                    f.write(f"# MISRA Violation Severity Distribution\n\n")
                    f.write(make_markdown_severity_dist_table(severity_stats, labels) + "\n\n")
                if violation_dist_md:
                    f.write(f"# Almost-Compliant Analysis\n\n")
                    f.write(violation_dist_md + "\n\n")
                if stubborn_rule_md:
                    f.write(f"# Stubborn Rules Analysis\n\n")
                    f.write(stubborn_rule_md + "\n\n")
                f.write("# Detailed Comparison\n\n")
                generate_detailed_markdown(f, table_data)
                logger.info(f"Markdown report saved to: {args.out_md}")
        except Exception as e:
            logger.error(f"Failed to save Markdown: {e}")

    # --- Save combined CSV report ---
    if args.out_csv:
        success = write_combined_csv(args.out_csv, stats, labels, total_samples, rule_stats,
                                     severity_stats, misra_category_map,
                                     violation_distribution, almost_compliant_ids, 
                                     stubborn_rule_counter, stat_threshold)
        if success:
            logger.info(f"CSV report saved to: {args.out_csv}")
        else:
            logger.error(f"Failed to save CSV: {args.out_csv}")

    # --- Save detailed per-item CSV ---
    if args.detailed_out_csv and table_data:
        try:
            fieldnames = ["ID", "Before", "Before_Violations"]
            for label in labels:
                fieldnames.extend([f"After ({label})", f"After ({label})_Violations"])
            with open(args.detailed_out_csv, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(table_data)
            logger.info(f"Detailed CSV saved to: {args.detailed_out_csv}")
        except Exception as e:
            logger.error(f"Failed to save detailed CSV: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Compare before/after code repair results with N-way comparison")
    parser.add_argument("--origin_jsonl", required=True, help="Pre-repair stat_detail.jsonl file")
    parser.add_argument("--refined_files", required=True, nargs='+', help="One or more post-repair result files (space-separated)")
    parser.add_argument("--refined_labels", required=True, nargs='+', help="Labels for each refined file (e.g. base explain iter_r3)")
    parser.add_argument("--misra_json", default="data/misra/misra-c-2012.json", help="(Optional) MISRA rule JSON for severity statistics")
    parser.add_argument("--detailed_out_csv", help="(Optional) Save per-item detailed comparison to CSV")
    parser.add_argument("--out_md", help="(Optional) Save full analysis report to Markdown")
    parser.add_argument("--out_csv", help="(Optional) Save all analysis tables to a single CSV")
    parser.add_argument("--start_index", type=int, default=-1, help="Start sample index (0-based, inclusive), -1 for no limit")
    parser.add_argument("--end_index", type=int, default=-1, help="End sample index (0-based, exclusive), -1 for no limit")
    args = parser.parse_args()
    setup_logging("log/pipeline", "statistic_refine_compare.log")
    main(args)