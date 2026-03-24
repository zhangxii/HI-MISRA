import os
import json
import argparse
import traceback
from tqdm import tqdm

from pipeline.generate.harness import (
    build_io_tests_from_sample,
    run_compiler,
    run_tester,
    run_harness_misra_check,
)
from utils.logger_util import get_logger, setup_logging, color_message
from pipeline.refine.statistic_refine_compare import is_misra_pass


@get_logger
def reharness_single(logger, obj, args):
    """Re-evaluate a single record's Refined_Code (compile, test, MISRA check)."""
    rid = obj.get("ID") or obj.get("problem-id") or "unknown"
    code = obj.get("Refined_Code", "")
    timeout = args.timeout
    compare = args.compare
    lang = args.lang

    rec = {
        "ID": rid,
        "compile_success": False,
        "compile_stderr": "",
        "test_passed": False,
        "pass_ratio": 0.0,
        "cases": [],
        "misra_violations": [],
        "misra_compliant": False,
        "is_refined": True
    }

    if not code or not code.strip():
        rec["error"] = "empty_refined_code"
        logger.error(f"[{rid}] Empty Refined_Code, skipping.")
        return {**obj, **rec}
    
    io_tests = build_io_tests_from_sample(obj.get("sample-test") or {})
    if not io_tests:
        rec["error"] = "no_sample_test"
        logger.error(f"[{rid}] No valid sample test cases, skipping.")
        return {**obj, **rec}

    try:
        # Step 1: Compile
        compile_success, compile_stderr = run_compiler(rid, code, lang)
        rec["compile_success"] = compile_success
        rec["compile_stderr"] = compile_stderr
        if not compile_success:
            return {**obj, **rec}

        # Step 2: Functional test
        args_for_tester = argparse.Namespace(lang=lang, timeout=timeout, compare=compare)
        test_passed, failed_summary, cases = run_tester(rid, code, io_tests, args_for_tester)
        rec["test_passed"] = test_passed
        rec["cases"] = cases
        if cases:
            passed_count = sum(1 for case in cases if case.get('ok'))
            rec["pass_ratio"] = passed_count / len(cases)

        # Step 3: MISRA compliance check
        misra_violations = run_harness_misra_check(obj, code, args)
        rec["misra_violations"] = misra_violations
        rec["misra_compliant"] = is_misra_pass(viol=rec["misra_violations"])

        return {**obj, **rec}

    except Exception as e:
        logger.error(f"[{rid}] Reharness failed: {e}\n{traceback.format_exc()}")
        rec["error"] = f"reharness_failed: {e}"
        return {**obj, **rec}
    

@get_logger
def reharness_jsonl(logger, args):
    records = []
    with open(args.in_jsonl, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                records.append(json.loads(line))

    processed_ids = set()
    if args.reuse and os.path.exists(args.out_jsonl):
        try:
            with open(args.out_jsonl, "r", encoding="utf-8") as f_in:
                for line in f_in:
                    if line.strip():
                        try:
                            obj = json.loads(line)
                            rid = obj.get("ID") or obj.get("problem-id")
                            if rid:
                                processed_ids.add(rid)
                        except json.JSONDecodeError:
                            pass
            logger.info(f"Reuse: found {len(processed_ids)} already processed IDs.")
        except Exception as e:
            logger.error(f"Failed to read existing output for reuse: {e}")
            processed_ids.clear()

    if processed_ids:
        records_to_process = [r for r in records if (r.get("ID") or r.get("problem-id")) not in processed_ids]
    else:
        records_to_process = records
    
    logger.info(f"Records to evaluate: {len(records_to_process)}")
    if not records_to_process:
        logger.info("No new records to process.")
        return

    file_mode = 'a' if args.reuse and processed_ids else 'w'
    
    cnt_compile_fail, cnt_test_fail, cnt_misra_viol = 0, 0, 0
    with open(args.out_jsonl, file_mode, encoding="utf-8") as out:
        for obj in tqdm(records_to_process, desc="Reharness"):
            if obj.get("refine_status") == "skip_no_problem" or not obj.get("Refined_Code"):
                out.write(json.dumps(obj, ensure_ascii=False) + "\n")
                continue
            
            rec = reharness_single(obj, args)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")

            if not rec.get("compile_success"):
                cnt_compile_fail += 1
            if not rec.get("test_passed"):
                cnt_test_fail += 1
            if not is_misra_pass(obj=rec, viol=None):
                cnt_misra_viol += 1

    logger.info(f"Done. Compile fail: {cnt_compile_fail}, Test fail: {cnt_test_fail}, MISRA violations: {cnt_misra_viol}")
    logger.info(f"Results saved to {args.out_jsonl}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Re-evaluate refined code (compile, test, MISRA)")
    parser.add_argument("--in_jsonl", default="data/codeflowbench/c/gpt-5-mini/refined/gpt-5-mini_refined_explain.jsonl", help="Input refined JSONL path")
    parser.add_argument("--out_jsonl", default="data/codeflowbench/c/gpt-5-mini/reharnessed/gpt-5-mini_reharnessed_explain.jsonl", help="Output evaluated JSONL path")
    parser.add_argument("--misra_script", type=str, default="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/misra.py")
    parser.add_argument("--misra_rule", type=str, default="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/test/misra/rule/misra.txt")
    parser.add_argument("--timeout", type=float, default=3.0)
    parser.add_argument("--compare", choices=["strict", "loose"], default="strict")
    parser.add_argument("--lang", choices=["c", "cpp"], default="c", help="Code language (c or cpp)")
    parser.add_argument("--reuse", action="store_true", help="Reuse existing results and skip processed IDs")
    args = parser.parse_args()
    setup_logging("log/pipeline", "reharness.log")
    reharness_jsonl(args)