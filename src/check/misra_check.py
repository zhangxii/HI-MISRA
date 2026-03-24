'''
Description: 
version: 
Author: zhangyating
Date: 2025-05-08 11:23:45
LastEditTime: 2025-05-13 14:36:39
'''

import re
import os
import subprocess
import pandas as pd
import csv
import json
from utils.logger_util import get_logger, color_message, setup_logging
from tqdm import tqdm


def load_rule_texts(rule_txt_path):
    """Extract rule IDs and descriptions from MISRA text file (supports 'Rule 11.4 Advisory' format)"""
    rule_map = {}
    current_rule = None

    with open(rule_txt_path, 'r', encoding='utf-8') as f:
        lines = [line.strip() for line in f if line.strip()]
        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith("Rule "):
                # Parse rule ID and level
                parts = line.split()
                if len(parts) >= 3:
                    rule_id = parts[1]
                    key = f"misra-c2012-{rule_id}"
                    # Use next line as description
                    if i + 1 < len(lines):
                        description = lines[i + 1].strip()
                        rule_map[key] = description
                        i += 1  # Skip description line
            i += 1

    return rule_map


@get_logger
def run_cppcheck_dump(logger, c_file_path, code_type='c', timeout_sec=120):
    """Run cppcheck to generate .dump file, returns (dump_path, stderr)"""
    try:
        base_command = [
            'cppcheck', 
            '--dump', 
            '--enable=all', 
            '--check-level=exhaustive'  # <--- Enable exhaustive check level
        ]
        if code_type == 'cpp':
            commands = base_command + ['--std=c++03', c_file_path]
        elif code_type == 'c':
            commands = base_command + ['--std=c11', c_file_path]
        else:
            logger.error(f"Unknown code_type: {code_type}, expected 'c' or 'cpp'")
            raise ValueError(f"Unknown code_type: {code_type}")
        result = subprocess.run(
            commands, 
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_sec  # Add timeout
        )
        logger.debug(f"-----------------run_cppcheck_dump Cmd: \n{' '.join(commands)}")
        logger.debug(f"Generated .dump file path: {c_file_path}.dump")
        if result.returncode != 0 or result.stderr:
            logger.debug(f"cppcheck dump error: {result.stderr}")
        return c_file_path + ".dump", result.stderr
    except subprocess.TimeoutExpired:
        logger.error(f"cppcheck dump timeout (> {timeout_sec}s): {c_file_path}")
        return c_file_path + ".dump", f"cppcheck timeout (> {timeout_sec}s)"


@get_logger
def run_misra_check(logger, misra_script_path, rule_txt_path, dump_file_path, timeout_seconds=300):
    """
    Run MISRA check script with timeout protection.
    
    :param timeout_seconds: Timeout in seconds, default 5 minutes.
    :return: (stdout, stderr) tuple
    """
    commands = ['python', misra_script_path, '--rule-texts=' + rule_txt_path, dump_file_path]
    logger.debug(f"-----------------run_misra_check Cmd: \n{' '.join(commands)}")
    
    try:
        result = subprocess.run(
            commands,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout_seconds  # <<<--- Core change: add timeout parameter
        )
        
        logger.debug(color_message(f"-----------------run_misra_check Out: \n{result.stdout}", 'blue'))
        if result.stderr:
            logger.debug(color_message(f"-----------------run_misra_check Error: \n{result.stderr}", 'cyan'))
        
        return result.stdout, result.stderr

    except subprocess.TimeoutExpired as e:
        # --- Core change: catch and handle timeout exception ---
        error_message = f"MISRA check process timed out after {timeout_seconds} seconds."
        logger.error(color_message(f"-----------------run_misra_check Error: \n{error_message}", 'red'))
        
        # On timeout, return empty stdout and a clear error message
        return "", error_message


@get_logger
def run_cppcheck_misra(logger, file_path, misra_script_path, rule_txt_path):
    """Run cppcheck and MISRA check, return violation info"""
    if not os.path.exists(file_path):
        logger.error(f"❌ File not found: {file_path}")
        return [{
            "file": os.path.basename(file_path),
            "line": "",
            "rule_id": "CheckFailed",
            "rule_description": "",
            "code": "",
            "raw_message": f"Check failed: file not found"
        }]


    try:
        logger.debug(f"🔍 Checking file: {file_path}")
        dump_file_path, dump_error = run_cppcheck_dump(file_path)
        if not os.path.exists(dump_file_path):
            logger.error(f"cppcheck dump file not generated: {dump_file_path}")
        
        error_keywords = ["error:", "syntaxError", "Can't process file", "Unmatched", "invalid", "not supported"]
        for kw in error_keywords:
            if kw in dump_error:
                return [{
                    "file": os.path.basename(file_path),
                    "line": "",
                    "rule_id": "CheckFailed",
                    "rule_description": "",
                    "code": "",
                    "raw_message": f"Check failed: {dump_error.strip()}"
                }]
            
        logger.debug(f"✅ cppcheck dump phase passed: {file_path}")
        misra_output, misra_error = run_misra_check(misra_script_path, rule_txt_path, dump_file_path)
        violations = extract_violations(misra_output, misra_error, file_path, rule_txt_path)
        if not violations:
            return [{
                    "file": os.path.basename(file_path),
                    "line": "",
                    "rule_id": "Compliant",
                    "rule_description": "",
                    "code": "",
                    "raw_message": misra_error
                }]
        logger.debug(f"✅ MISRA Check completed: {file_path}, found {len(violations)} violations")
        return violations   # ← Ensure we always return a list
    except Exception as e:
        logger.error(f"❌ run_cppcheck_misra failed: {file_path}\nReason: {e}")
        return [{
            "file": os.path.basename(file_path),
            "line": "",
            "rule_id": "CheckFailed",
            "rule_description": "",
            "code": "",
            "raw_message": f"Check failed: {e}"
        }]

@get_logger
def extract_violations(logger, misra_output, misra_error, file_path, rule_txt_path):
    rule_descriptions = {}
    # rule_txt_path = "/root/autodl-tmp/workspace/safe_synth/data/misra/misra-cpp-2008.txt"
    # Load misra.txt content (if available)
    rule_descriptions = load_rule_texts(rule_txt_path)
    """Extract violation info from MISRA check output"""
    error_keywords = ["error:", "syntaxError", "Can't process file", "Unmatched", "invalid", "not supported"]
    for kw in error_keywords:
        if kw in misra_error or kw in misra_output:
            return [{
                "file": os.path.basename(file_path),
                "line": "",
                "rule_id": "CheckFailed",
                "rule_description": "",
                "code": "",
                "raw_message": f"Check failed: {misra_error.strip()}"
            }]
    violations = []
    pattern = re.compile(r"\[([^\]:]+):(\d+)\](.*?)\[(misra-[\w\d\-\.]+)\]")
    for match in pattern.finditer(misra_error):
        file, line, message, rule_id = match.groups()
        if "misra" not in rule_id:
            logger.warning("Non-misra rule found in misra_error, unexpected.")
            continue
        line = int(line)
        code_line = extract_line_from_file(file_path, line)
        violations.append({
            "file": os.path.basename(file),
            "line": line,
            "rule_id": rule_id,
            "rule_description": rule_descriptions.get(rule_id, "Description not found"),
            "code": code_line,
            "raw_message": match.group(0).strip()
        })
    # Fallback: if no violations, return Compliant
    if not violations:
        return [{
            "file": os.path.basename(file_path),
            "line": "",
            "rule_id": "Compliant",
            "rule_description": "",
            "code": "",
            "raw_message": ""
        }]
    logger.debug(color_message(f"violations: \n{json.dumps(violations, indent=2)}", "cyan"))
    return violations


def extract_line_from_file(file_path, line_num):
    """Extract a specific line of code from a file"""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if 0 < line_num <= len(lines):
                return lines[line_num - 1].strip()
    except Exception:
        return ""
    return ""

@get_logger
def analyze_misra_csv(logger, csv_file_path, summary_output_path, statistics_output_path):
    """Analyze MISRA check results CSV, compile compliance statistics"""
    # Read MISRA check results CSV
    df = pd.read_csv(csv_file_path)

    # Remove empty rows (if any)
    df = df.dropna(subset=["file", "rule_id"])

    # Classify by file, priority: CheckFailed > non-Compliant > Compliant
    file2rule = {}
    for _, row in df.iterrows():
        fname = row["file"]
        rule_id = row["rule_id"]
        if fname not in file2rule:
            file2rule[fname] = set()
        file2rule[fname].add(rule_id)

    compliant_tasks = set()
    non_compliant_tasks = set()
    failed_tasks = set()

    for fname, rules in file2rule.items():
        if "CheckFailed" in rules:
            failed_tasks.add(fname)
        elif any(r != "Compliant" for r in rules):
            non_compliant_tasks.add(fname)
        else:
            compliant_tasks.add(fname)

    # Output statistics
    summary = {
        "total_tasks": len(df["file"].unique()),
        "compliant_tasks": len(compliant_tasks),
        "non_compliant_tasks": len(non_compliant_tasks),
        "check_failed_tasks": len(failed_tasks)
    }

    logger.info("✅ MISRA Compliance statistics:")
    logger.info(f"Total tasks: {summary['total_tasks']}")
    logger.info(f"✅ Compliant tasks: {summary['compliant_tasks']}")
    logger.info(f"❌ Non-compliant tasks: {summary['non_compliant_tasks']}")
    logger.info(f"⚠️ Failed tasks: {summary['check_failed_tasks']}")

    # Only count violation rules for non-compliant tasks
    non_compliant_df = df[df["file"].isin(non_compliant_tasks)]
    violations_summary = (
        non_compliant_df
        .groupby("file")
        .agg({
            "rule_id": lambda x: list(set(x)),
            "rule_description": lambda x: list(set(x))
        })
        .reset_index()
    )
    violations_summary.columns = ["file", "violated_rules", "violated_descriptions"]

    violations_summary.to_csv(summary_output_path, index=False)
    logger.info(f"📄 Violated rules per non-compliant task saved to: {summary_output_path}")

    with open(statistics_output_path, "w", encoding="utf-8") as f:
        f.write("✅ MISRA Compliance statistics:\n")
        f.write(f"Total tasks: {summary['total_tasks']}\n")
        f.write(f"✅ Compliant tasks: {summary['compliant_tasks']}\n")
        f.write(f"❌ Non-compliant tasks: {summary['non_compliant_tasks']}\n")
        f.write(f"⚠️ Failed tasks: {summary['check_failed_tasks']}\n")

    return summary, violations_summary

@get_logger
def find_c_files(logger, folder_path, ends='.c'):
    """Find all .c and .cpp files in the specified folder"""
    target_files = []
    for root, _, files in os.walk(folder_path):
        for file in files:
            if file.endswith(ends):
                target_files.append(os.path.join(root, file))
    # target_files = target_files[0:1] #zyt: for test
    logger.info(f"🔍 Found {len(target_files)} {ends} files, starting check")
    return target_files


@get_logger
def check_files(logger, files, misra_script_path, rule_txt_path, records):
    logger.info("Starting to check each file...")
    """Check individual files and return violation info"""
    for c_file in tqdm(files, desc="Checking files", unit="file"):
        try:
            violations = run_cppcheck_misra(c_file, misra_script_path, rule_txt_path)
            if violations:
                for v in violations:
                    records.append({
                        "file": v["file"],
                        "full_path": c_file,
                        "line": v["line"],
                        "rule_id": v["rule_id"],
                        "rule_description": v["rule_description"],
                        "code": v["code"],
                        "raw_message": v["raw_message"]
                    })
            else:
                records.append({
                    "file": os.path.basename(c_file),
                    "full_path": c_file,
                    "line": "",
                    "rule_id": "Compliant",
                    "rule_description": "",
                    "code": "",
                    "raw_message": ""
                })
        except Exception as e:
            # Record check failures separately
            records.append({
                "file": os.path.basename(c_file),
                "full_path": c_file,
                "line": "",
                "rule_id": "CheckFailed",
                "rule_description": "",
                "code": "",
                "raw_message": f"Check failed: {e}"
            })


@get_logger
def check_folder_and_save_csv(logger, folder_path, misra_script_path, rule_txt_path, csv_output_path):
    """Check all files in folder and save MISRA check results to CSV (with full paths)"""
    c_files= find_c_files(folder_path, "c")
    cpp_files = find_c_files(folder_path, "cpp")

    records = []
    check_files(c_files, misra_script_path, rule_txt_path, records)
    check_files(cpp_files, misra_script_path, rule_txt_path, records)
   

    with open(csv_output_path, "w", newline='', encoding="utf-8") as csvfile:
        fieldnames = ["file", "full_path", "line", "rule_id", "rule_description", "code", "message"]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        for r in records:
            writer.writerow(r)

    logger.info(f"✅ MISRA Detailed check results saved as CSV: {csv_output_path}")
