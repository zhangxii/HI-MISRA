# harness.py
import os
import json
import argparse
import tempfile
import subprocess
import re
import sys
import signal
import multiprocessing
from concurrent.futures import ProcessPoolExecutor, as_completed

from tqdm import tqdm

from utils.logger_util import get_logger, setup_logging, color_message
from check.misra_check import run_cppcheck_misra


def extract_core_driver(code: str):
    if code is None:
        return "", "", ""

    # 1) Strip Markdown code fences
    lines = code.split("\n")
    clean_lines = []
    for line in lines:
        if line.strip().startswith("```"):
            continue
        clean_lines.append(line)
    clean_text = "\n".join(clean_lines)

    # --- helpers ---
    default_includes = [
        "#include <stdint.h>",
        "#include <stdbool.h>",
        "#include <string.h>",
        "#include <stdlib.h>",
        "#include <stdio.h>",
        "#include <math.h>",
        "#include <limits.h>",
        "#include <ctype.h>",
        "#include <inttypes.h>",
    ]

    def _split_by_comment_markers(s: str):
        pat = re.compile(
            r"(?is)//\s*(?:-+\s*)?CORE\s*(?:-+)?\s*\r?\n(.*?)\r?\n//\s*(?:-+\s*)?DRIVER\s*(?:-+)?\s*\r?\n(.*)",
            re.DOTALL,
        )
        mm = pat.search(s)
        if not mm:
            return None
        return mm.group(1).strip(), mm.group(2).strip()

    def _split_by_bracket_markers(s: str):
        pat = re.compile(
            r"(?is)(?:^|\n)[ \t\*#]*\[CORE\][ \t\*]*\r?\n(.*?)\r?\n(?:[ \t\*#]*\[DRIVER\][ \t\*]*\r?\n)(.*)",
            re.DOTALL | re.IGNORECASE,
        )
        mm = pat.search(s)
        if not mm:
            return None
        return mm.group(1).strip(), mm.group(2).strip()

    def _extract_and_strip_includes(src: str):
        incs = []
        out_lines = []
        for ln in src.splitlines():
            if re.match(r"^\s*#\s*include\s+[<\"].*[>\"]\s*$", ln):
                incs.append(re.sub(r"^\s+", "", ln).rstrip())
            else:
                out_lines.append(ln)
        stripped = "\n".join(out_lines).strip()
        return incs, stripped

    def _merge_includes(*include_lists):
        seen = set()
        merged = []
        for lst in include_lists:
            for inc in lst:
                if inc not in seen:
                    merged.append(inc)
                    seen.add(inc)
        return merged

    # 2) Try new format (comment markers) first, then fall back to old format
    split = _split_by_comment_markers(clean_text)
    if split is None:
        split = _split_by_bracket_markers(clean_text)

    if split is None:
        # Cannot split, return original text as-is (driver is empty)
        return clean_text.strip(), "", clean_text.strip()

    core_src, driver_src = split

    # 3) Collect, deduplicate, and prepend all includes
    core_incs, core_body = _extract_and_strip_includes(core_src)
    drv_incs, drv_body = _extract_and_strip_includes(driver_src)

    includes = _merge_includes(default_includes, core_incs, drv_incs)
    headers = "\n".join(includes) + "\n\n"

    full_src = (
        f"{headers}"
        f"// --- Core Logic ---\n{core_body}\n\n"
        f"// --- Driver Logic ---\n{drv_body}"
    )

    return core_body, drv_body, full_src

@get_logger
def read_records(logger, path: str):
    with open(path, "r", encoding="utf-8") as f:
        txt = f.read().strip()

    if not txt:
        return []

    if "\n" in txt and txt.lstrip().startswith("{") and txt.count("\n{") > 0:
        recs = []
        for line in txt.splitlines():
            if line.strip():
                recs.append(json.loads(line))
        return recs

    data = json.loads(txt)
    return data if isinstance(data, list) else [data]


@get_logger
def normalize_out(logger, s: str) -> str:
    lines = s.splitlines()
    lines = [re.sub(r"[ \t]+$", "", L) for L in lines]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def tokenize_lines(s: str):
    return [re.findall(r"\S+", line) for line in s.strip("\n").splitlines()]


def normalize_tokens(tokens, case_mode: str = "none"):
    out = []
    for line in tokens:
        if case_mode == "upper":
            out.append([t.upper() for t in line])
        elif case_mode == "lower":
            out.append([t.lower() for t in line])
        else:
            out.append(line)
    return out


@get_logger
def build_io_tests_from_sample(logger, sample_test: dict):
    if not isinstance(sample_test, dict):
        return []

    ins = sample_test.get("input") or []
    outs = sample_test.get("output") or []
    pairs = []

    for i in range(min(len(ins), len(outs))):
        stdin = ins[i]
        stdout = outs[i]
        if not stdin.endswith("\n"):
            stdin += "\n"
        if not stdout.endswith("\n"):
            stdout += "\n"
        pairs.append({"stdin": stdin, "stdout": stdout})

    return pairs


@get_logger
def run_exe(logger, exe: str, stdin_text: str, timeout: float = 3.0):
    return subprocess.run(
        [exe],
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def rc_readable(rc: int) -> str:
    if rc < 0:
        try:
            return f"{rc} ({signal.Signals(-rc).name})"
        except Exception:
            return f"{rc} (SIG{-rc})"
    return str(rc)


@get_logger
def compile_cpp(logger, code: str, cxx: str = "g++"):
    tmpdir = tempfile.mkdtemp(prefix="cxx_run_")
    src = os.path.join(tmpdir, "main.cpp")
    exe = os.path.join(tmpdir, "a.out")
    with open(src, "w", encoding="utf-8") as f:
        f.write(code)

    cmd = [cxx, src, "-std=gnu++17", "-O2", "-pipe", "-s", "-o", exe]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return (False, None, p.stderr, tmpdir)
    return (True, exe, "", tmpdir)


@get_logger
def compile_c(logger, code: str, cc: str = "gcc"):
    tmpdir = tempfile.mkdtemp(prefix="c_run_")
    src = os.path.join(tmpdir, "main.c")
    exe = os.path.join(tmpdir, "a.out")
    with open(src, "w", encoding="utf-8") as f:
        f.write(code)

    cmd = [cc, src, "-std=c11", "-Wall", "-Wextra", "-pedantic", "-o", exe]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        return (False, None, p.stderr, tmpdir)
    return (True, exe, "", tmpdir)


@get_logger
def run_compiler(logger, rid: str, code_to_check: str, lang: str):
    logger.debug(f"[{rid}] COMPILER: Attempting to compile...")
    ok, exe, err, tmpdir = False, None, "Unsupported language", None
    try:
        if lang == "c":
            ok, exe, err, tmpdir = compile_c(code_to_check)
        elif lang == "cpp":
            ok, exe, err, tmpdir = compile_cpp(code_to_check)

        if ok:
            logger.info(color_message(f"[{rid}] --COMPILER: Compilation SUCCESSFUL.", "green"))
            return True, ""
        logger.info(f"[{rid}] --COMPILER: Compilation FAILED.")
        
        logger.debug(f"[{rid}] --COMPILER: Compilation code: \n{code_to_check}")
        logger.error(f"[{rid}] --COMPILER: Compilation stderr: {err}")
        return False, err
    except Exception as e:
        logger.error(f"[{rid}] --COMPILER: Compilation ERROR: {e}", exc_info=True)
        return False, str(e)
    finally:
        if tmpdir and os.path.exists(tmpdir):
            try:
                if exe and os.path.exists(exe):
                    os.remove(exe)
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                os.rmdir(tmpdir)
            except Exception as e:
                logger.warning(f"[{rid}] --COMPILER: Error cleaning up temp directory {tmpdir}: {e}")


@get_logger
def is_misra_pass(logger, obj=None, viol=None):
    if obj is None and viol is None:
        return False

    if viol is None:
        if not isinstance(obj, dict):
            return False
        if "misra_compliant" in obj:
            return obj["misra_compliant"]
        viol = obj.get("misra_violations")

    if not isinstance(viol, list):
        return False
    if not viol:
        return True
    return all(v.get("rule_id") == "Compliant" for v in viol)


@get_logger
def run_harness_misra_check(logger, obj, code: str, args):
    tmpdir = None
    lang = args.lang
    rid = obj.get("ID") or obj.get("problem-id") or "unknown"
    try:
        logger.info(f"ID: {rid} MISRA | language: {lang} | Starting MISRA compliance check...")
        if lang not in ("c", "cpp"):
            return None

        ok, exe, err, tmpdir = compile_c(code) if lang == "c" else compile_cpp(code)

        src_filename = f"main.{lang}"
        cpp_path = os.path.join(tmpdir, src_filename)

        if not os.path.exists(cpp_path):
            logger.error(f"ID: {rid} MISRA: Source file generation failed: {err}")
            return None

        misra_violations = run_cppcheck_misra(cpp_path, args.misra_script, args.misra_rule)

        if is_misra_pass(obj=None, viol=misra_violations):
            logger.debug(color_message(f"[{rid}] MISRA: CppCheck SUCCESSFUL.", "green"))
            return []
        non_compliant = [v for v in misra_violations if v.get("rule_id") != "Compliant"]
        n_viol = len(non_compliant)
        logger.debug(color_message(f"ID: {rid} --MISRA: Violations count {n_viol}", "cyan"))
        return non_compliant

    except Exception as e:
        logger.warning(f"ID: {rid} MISRA: MISRA check process failed: {e}")
        return None
    finally:
        if tmpdir and os.path.exists(tmpdir):
            try:
                exe_path_in_tmp = os.path.join(tmpdir, "a.out")
                if os.path.exists(exe_path_in_tmp):
                    os.remove(exe_path_in_tmp)
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                os.rmdir(tmpdir)
            except Exception as e:
                logger.warning(f"ID: {rid} MISRA: Error cleaning up temp directory {tmpdir}: {e}")


@get_logger
def run_tester(logger, rid: str, code_to_check: str, io_tests, args):
    logger.debug(f"[{rid}] TESTER: Starting test run...")
    lang = args.lang
    ok, exe, err, tmpdir = False, None, "Unsupported language", None
    try:
        if lang == "c":
            ok, exe, err, tmpdir = compile_c(code_to_check)
        elif lang == "cpp":
            ok, exe, err, tmpdir = compile_cpp(code_to_check)
        else:
            logger.error(f"[{rid}] ----TESTER: Unsupported language '{lang}'")
            return False, f"Unsupported language '{lang}'", []

        if not ok:
            logger.warning(f"[{rid}] ----TESTER: Compilation failed before testing. Cannot run tests.")
            return False, f"Compilation failed with error: {err}", []

        if not io_tests:
            logger.warning(f"{rid} No valid I/O test cases found, marking all as passed.")
            return True, "", []

        passed = 0
        failed_cases_details_summary = []
        detailed_cases_results = []

        for i, case in enumerate(io_tests, 1):
            case_result = {
                "idx": i,
                "ok": False,
                "type": "value_mismatch",
                "returncode": "N/A",
                "stdin": case["stdin"],
                "stdout": "",
                "expected": case["stdout"],
                "stderr": "",
            }
            try:
                timeout = args.timeout or 3.0
                p = run_exe(exe, case["stdin"], timeout=timeout)

                case_result["returncode"] = rc_readable(p.returncode)
                case_result["stdout"] = p.stdout
                case_result["stderr"] = p.stderr

                got = normalize_out(p.stdout)
                exp = normalize_out(case["stdout"])

                case_ok = False
                if p.returncode == 0:
                    case_result["type"] = "value_mismatch"
                    if args.compare == "strict":
                        if got == exp:
                            case_ok = True
                            case_result["type"] = "ok"
                    else:
                        got_tok = normalize_tokens(tokenize_lines(got))
                        exp_tok = normalize_tokens(tokenize_lines(exp))
                        if got_tok == exp_tok:
                            case_ok = True
                            case_result["type"] = "ok_loose"
                else:
                    case_result["type"] = "runtime_error"

                case_result["ok"] = case_ok
                if case_ok:
                    passed += 1
                else:
                    failed_cases_details_summary.append(
                        f"Case#{i} | stdin={repr(case['stdin'])} | got={repr(p.stdout)} | expected={repr(case['stdout'])}"
                    )
            except subprocess.TimeoutExpired:
                case_result["type"] = "timeout"
                case_result["returncode"] = "TIMEOUT"
                failed_cases_details_summary.append(f"Case#{i} | stdin={repr(case['stdin'])} | TIMEOUT")

            detailed_cases_results.append(case_result)

        all_passed = (passed == len(io_tests))
        summary = "\n".join(failed_cases_details_summary)

        if all_passed:
            logger.info(color_message(f"[{rid}] ----TESTER: All {len(io_tests)} tests PASSED.", "green"))
            return True, "", detailed_cases_results

        logger.warning(f"[{rid}] ----TESTER: {len(io_tests) - passed}/{len(io_tests)} tests FAILED.")
        return False, summary, detailed_cases_results

    except Exception as e:
        logger.error(f"[{rid}] ----TESTER: Error during testing: {e}", exc_info=True)
        return False, str(e), []
    finally:
        if tmpdir and os.path.exists(tmpdir):
            try:
                if exe and os.path.exists(exe):
                    os.remove(exe)
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                os.rmdir(tmpdir)
            except Exception as e:
                logger.warning(f"[{rid}] ----TESTER: Error cleaning up temp directory {tmpdir}: {e}")


@get_logger
def process_record(logger, record_tuple):
    obj, args = record_tuple
    rid = obj.get("ID") or obj.get("problem-id") or "unknown"
    gen = obj.get("gen") or {}
    raw_llm_code = gen.get("LLM_Code") or obj.get("LLM_Code") or ""
    io_tests = build_io_tests_from_sample(obj.get("sample-test") or {})

    logger.info(f"Start processing ID: {rid}")

    rec = {
        "ID": rid,
        "compile_success": False,
        "test_passed": False,
        "pass_ratio": 0.0,
        "cases": [],
        "misra_violations": [{"rule_id": "CheckFailed", "message": "Not checked"}],
        "format_compliant": False,
    }

    if not raw_llm_code.strip():
        rec["error"] = "empty_code"
        logger.warning(f"ID: {rid} code is empty, skipped")
        return {**obj, **rec}

    core_code, driver_code, runnable_code = extract_core_driver(raw_llm_code)

    logger.info(f"[{rid}] Code Split: Core={len(core_code)} chars, Driver={len(driver_code)} chars")

    if not runnable_code.strip():
        rec["error"] = "extract_failed"
        return {**obj, **rec}

    rec["format_compliant"] = (len(driver_code) > 0)

    is_success, new_stderr = run_compiler(rid, runnable_code, args.lang)
    if not is_success:
        rec["compile_stderr"] = new_stderr
        rec["compile_success"] = False
    else:
        rec["compile_success"] = True
        logger.debug(f"ID: {rid} compilation successful")

    misra_res = run_harness_misra_check(obj, core_code, args)
    misra_violations = misra_res if isinstance(misra_res, list) else []

    rec["misra_violations"] = misra_violations
    n_viol = len([v for v in misra_violations if v.get("rule_id") != "Compliant"])

    is_check_failed = any(v.get("rule_id") == "CheckFailed" for v in misra_violations)

    if is_check_failed:
        logger.warning(color_message(f"[{rid}] MISRA CHECK FAILED! Dumping Runnable Code for Debug:", "red"))
    elif n_viol < 3:
        msg_color = "green" if n_viol == 0 else "yellow"
        logger.info(color_message(f"[{rid}] Excellent/Good Result ({n_viol} violations). Dumping Runnable Code:", msg_color))

    logger.info(color_message(f"[{rid}] MISRA Check (Core Only): {n_viol} violations", "cyan"))

    if not io_tests:
        rec["error"] = "no_sample_test"
        return {**obj, **rec}

    if rec["compile_success"]:
        test_ok, failed_summary, detailed_cases = run_tester(rid, runnable_code, io_tests, args)
        rec["cases"] = detailed_cases

        if io_tests:
            passed_count = sum(1 for case in detailed_cases if case.get("ok"))
            total_cases = len(detailed_cases)
            rec["test_passed"] = (passed_count == total_cases)
            rec["pass_ratio"] = (passed_count / total_cases) if total_cases > 0 else 1.0
            logger.debug(color_message(f"ID: {rid} - Pass Rate: {passed_count} / {total_cases} | {rec['pass_ratio']:.2%}", "cyan"))
        else:
            rec["test_passed"] = True
            rec["pass_ratio"] = 1.0

        if not test_ok:
            rec["test_stderr"] = failed_summary
    else:
        rec["test_passed"] = False
        rec["pass_ratio"] = 0.0

    return {**obj, **rec}


@get_logger
def main(logger, args):
    logger.info(f"{args.lang} IO code evaluation script started")
    try:
        records = read_records(args.in_json)
        logger.info(f"Successfully read {len(records)} records from '{args.in_json}'")
    except Exception as e:
        logger.error(f"Error reading file '{args.in_json}': {e}")
        return

    processed_ids = set()
    if args.reuse and os.path.exists(args.out_jsonl):
        try:
            logger.info("Detected --reuse flag, reading previously processed records...")
            processed_records = read_records(args.out_jsonl)
            for rec in processed_records:
                if "problem-id" in rec:
                    processed_ids.add(rec["problem-id"])
                elif "ID" in rec:
                    processed_ids.add(rec["ID"])
            logger.info(f"Found {len(processed_ids)} previously processed records.")
        except Exception as e:
            logger.warning(f"Error reading existing output file: {e}. Will reprocess all records.")
            processed_ids.clear()

    if processed_ids:
        records = [
            r for r in records
            if r.get("problem-id") not in processed_ids and r.get("ID") not in processed_ids
        ]
        logger.info(f"Will process remaining {len(records)} new records.")

    tasks = [(record, args) for record in records]
    if not tasks:
        logger.info("No new records to process. Task complete.")
        return

    write_mode = "a" if args.reuse and processed_ids else "w"
    with open(args.out_jsonl, write_mode, encoding="utf-8") as out_file:
        max_workers = args.workers if args.workers > 0 else multiprocessing.cpu_count()
        logger.info(f"Using {max_workers} concurrent workers for evaluation...")
        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_record, task) for task in tasks]
            for future in tqdm(as_completed(futures), total=len(tasks), desc="Evaluation:", file=sys.stderr):
                try:
                    result = future.result()
                    out_file.write(json.dumps(result, ensure_ascii=False) + "\n")
                except Exception as e:
                    logger.error(f"An evaluation task failed: {e}", exc_info=True)

    logger.info(f"All records processed. Results written to '{args.out_jsonl}'")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in_json", required=True)
    ap.add_argument("--out_jsonl", required=True)
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--compare", choices=["strict", "loose"], default="strict")
    ap.add_argument("--lang", choices=["c", "cpp"], default="c")
    ap.add_argument("--misra_script", type=str, default="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/misra.py")
    ap.add_argument("--misra_rule", type=str, default="/root/autodl-tmp/workspace/cppcheck-2.17.0/addons/test/misra/rule/misra.txt")
    ap.add_argument("-w", "--workers", type=int, default=0)
    ap.add_argument("--reuse", action="store_true")

    setup_logging("log/pipeline", "harness.log")
    args = ap.parse_args()
    main(args)
