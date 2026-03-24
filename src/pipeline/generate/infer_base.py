"""
======================================================================
infer_base.py (Baseline Generation - Core/Driver Separation)
Unified style with infer_safe_gen.py:
- Threaded generation
- Per-task temp JSON output
- CORE/DRIVER structured output
- System prompt aligned with SafeGen V4 system prompt (for C)
======================================================================
"""

import os
import re  
import json
import uuid
import time
import argparse
import traceback
import copy
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

from openai import OpenAI
from tqdm import tqdm

from utils.logger_util import get_logger, setup_logging, color_message
from model_config import MODELS_CONFIG


def ensure_dir(p: str) -> None:
    if p and (not os.path.exists(p)):
        os.makedirs(p, exist_ok=True)


def _normalize_id(obj: Dict[str, object]) -> str:
    cur_id = str(
        obj.get("problem_id")
        or obj.get("id")
        or obj.get("ID")
        or obj.get("task_id")
        or obj.get("problem-id")
        or obj.get("problem-id")
        or obj.get("problem_id")
        or obj.get("problem-id")
        or ""
    )
    if not cur_id:
        cur_id = str(obj.get("problem-id") or obj.get("problem-id") or "")
    if not cur_id:
        cur_id = str(uuid.uuid4())
    return cur_id


def _fix_broken_includes(code_text: str) -> str:
    """
    Defensive fix for a frequent failure mode observed in base runs:
    lines like 'include <stdint.h>' (missing '#') cause compilation to fail.
    Only fixes standalone include directives at line start.
    """
    if not code_text:
        return code_text

    fixed_lines: List[str] = []
    for line in code_text.splitlines():
        m = re.match(r"^(\s*)include(\s*)([<\"].*[>\"])\s*$", line)
        if m:
            fixed_lines.append(f"{m.group(1)}#include{m.group(2)}{m.group(3)}")
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines)




def _looks_like_cpp(code_text: str) -> bool:
    if not code_text:
        return False
    cpp_markers = [
        "std::", "vector<", "string", "iostream", "namespace", "template<", "nullptr",
        "static_cast", "reinterpret_cast", "dynamic_cast", "const_cast",
        "using namespace", "#include <bits/stdc++.h>", "cout", "cin", "printf <<"
    ]
    t = code_text
    return any(m in t for m in cpp_markers)


def _has_unbalanced_braces(code_text: str) -> bool:
    if not code_text:
        return False
    # Remove strings and char literals to avoid counting braces inside them
    s = re.sub(r'".*?(?<!\\)"', '""', code_text, flags=re.S)
    s = re.sub(r"'.*?(?<!\\)'", "''", s, flags=re.S)
    open_braces = s.count("{")
    close_braces = s.count("}")
    open_paren = s.count("(")
    close_paren = s.count(")")
    open_bracket = s.count("[")
    close_bracket = s.count("]")
    return (open_braces != close_braces) or (open_paren != close_paren) or (open_bracket != close_bracket)

class BaseEngine:
    """
    Baseline generator.
    Uses the same system prompt scaffold as SafeGen V4 (for C),
    but does not inject rule blocks/skeletons or use logit-bias.
    """

    def __init__(
        self,
        client: OpenAI,
        model_name: str,
        logger=None,
        lang: str = "c",
        temperature: float = 0.1,
        timeout: int = 1200,
    ) -> None:
        self.client = client
        self.model_name = model_name
        self.logger = logger
        self.lang = lang
        self.temperature = temperature
        self.timeout = timeout

    def _build_system_prompt(self) -> str:
        """
        Baseline system prompt (C):
        - Single code block output
        - Split CORE/DRIVER using comment markers
        - C11 + MISRA-oriented constraints
        - Minimal comments
        """

        # Only C is aligned to the baseline MISRA C prompt.
        if self.lang != "c":
            return (
                "You are an expert C++ programmer. "
                "Write a complete C++ implementation based on the description. "
                "Only output raw C++ code. No extra text."
            )

        lines: List[str] = []
        lines.append("You are a MISRA C:2012 oriented C11 code generator.")
        lines.append("")
        lines.append("ABSOLUTE REQUIREMENTS:")
        lines.append("1) Output EXACTLY ONE single code block in C: start with ```c and end with ```.")
        lines.append("2) Inside that code block, you MUST include two markers in this order:")
        lines.append("   // --- CORE ---")
        lines.append("   // --- DRIVER ---")
        lines.append("3) [CORE] contains ALL logic: types, structs, macros, helper functions, and core APIs.")
        lines.append("   - No main(). No stdin/stdout I/O.")
        lines.append("4) [DRIVER] contains ONLY main() and all stdin/stdout I/O, and calls [CORE] functions.")
        lines.append("   - Do NOT re-define structs/typedefs from CORE in DRIVER.")
        lines.append("   - Do NOT re-implement CORE functions in DRIVER.")
        lines.append("")
        lines.append("LANGUAGE CONSTRAINTS (C ONLY):")
        lines.append("- Use ISO C11 only. Do NOT use any C++ features or syntax.")
        lines.append("- Forbidden examples: std::, vector<>, string, iostream, namespace, templates, auto, new/delete, nullptr, references (& in types), static_cast, range-for, using.")
        lines.append("")
        lines.append("COMPLETENESS:")
        lines.append("- The output must be complete and compilable as a single C file.")
        lines.append("- Ensure all braces/parentheses are balanced and every function is fully implemented.")
        lines.append("")
        lines.append("COMMENTS:")
        lines.append("- Keep comments to an absolute minimum.")
        lines.append("- If a comment is necessary, it must be one short line only.")
        lines.append("")
        lines.append("FORMAT (EXACT):")
        lines.append("```c")
        lines.append("// --- CORE ---")
        lines.append("/* core code */")
        lines.append("// --- DRIVER ---")
        lines.append("/* driver code */")
        lines.append("```")
        return "\n".join(lines)





    def _build_user_prompt(self, description: str, input_str: str, output_str: str) -> str:
        parts: List[str] = []
        parts.append("[TASK]")
        parts.append(description or "(no description)")
        parts.append("")

        if input_str or output_str:
            parts.append("[IO SPEC]")
            if input_str:
                parts.append("Input description or example:")
                parts.append(input_str)
            if output_str:
                parts.append("")
                parts.append("Output description or example:")
                parts.append(output_str)
            parts.append("")

        parts.append("Generate code following the system OUTPUT FORMAT strictly.")
        return "\n".join(parts)

    def generate(self, description: str, input_str: str, output_str: str) -> Tuple[str, Dict[str, object]]:
        sys_prompt = self._build_system_prompt()
        user_prompt = self._build_user_prompt(description, input_str, output_str)

        self.logger and self.logger.debug(color_message(f"System Prompt:\n{sys_prompt}",'cyan'))
        self.logger and self.logger.debug(color_message(f"User Prompt:\n{user_prompt}",'cyan'))
        
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ]

        if self.logger:
            preview = (description or "")[:120].replace("\n", " ")
            self.logger.info(
                color_message(f"[Base] Calling model={self.model_name} | task_preview='{preview}'", "cyan")
            )

        resp = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=self.temperature,
            timeout=self.timeout,
        )
        text = resp.choices[0].message.content or ""

        # Defensive fix for base runs
        # self.logger.info(f"text: {text}")
        text = _fix_broken_includes(text)
        self.logger.info(f"_fix_broken_includes text: {text}")

        meta = {
            "temperature": self.temperature,
            "timeout": self.timeout,
        }
        return text, meta

@get_logger
def process_record(logger, row: Dict[str, object], args, client: OpenAI, temp_dir: str) -> None:
    try:
        raw_obj = copy.deepcopy(row)
        if "subproblems" in raw_obj:
            del raw_obj["subproblems"]

        cur_id = _normalize_id(raw_obj)
        temp_path = os.path.join(temp_dir, f"{cur_id}.json")

        if args.reuse and os.path.exists(temp_path):
            logger.info(f"Task {cur_id} exists, skip (reuse)")
            return

        description = (
            raw_obj.get("problem-description")
            or raw_obj.get("description")
            or raw_obj.get("LLM_description")
            or ""
        )
        input_str = raw_obj.get("input", "") or ""
        output_str = raw_obj.get("output", "") or ""

        engine = BaseEngine(
            client=client,
            model_name=args.model_name,
            logger=logger,
            lang=args.lang,
            temperature=args.temperature,
            timeout=args.timeout,
        )

        start_ts = time.time()
        final_code, meta_info = engine.generate(str(description), str(input_str), str(output_str))
        cost_time = time.time() - start_ts

        logger.info(
            color_message(
                f"Task {cur_id} generated len={len(final_code)} | Preview:\n{final_code[:400]}",
                "cyan",
            )
        )

        io_tests: List[Dict[str, str]] = []
        st = raw_obj.get("sample-test") or {}
        inp_list = st.get("input") if isinstance(st, dict) else None
        out_list = st.get("output") if isinstance(st, dict) else None
        if isinstance(inp_list, list) and isinstance(out_list, list) and inp_list and out_list:
            stdin_text = inp_list[0]
            stdout_text = out_list[0]
            if not stdin_text.endswith("\n"):
                stdin_text += "\n"
            if not stdout_text.endswith("\n"):
                stdout_text += "\n"
            io_tests.append({"stdin": stdin_text, "stdout": stdout_text})

        raw_obj["gen"] = {
            "model_family": args.model_family,
            "model_name": args.model_name,
            "created_ts": int(time.time()),
            "cost_time": cost_time,
            "LLM_Code": final_code,
            "LLM_Raw": final_code,
            "base_meta": meta_info,
            "io_tests": io_tests,
            "note": "Baseline output saved as a single ```c block split by // --- CORE --- and // --- DRIVER --- markers.",
        }

        ensure_dir(os.path.dirname(temp_path))
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(raw_obj, f, ensure_ascii=False, indent=2)

    except Exception:
        logger.error(f"Task processing failed.\n{traceback.format_exc()}")


@get_logger
def main(logger, args) -> None:
    logger.info("=== Starting Baseline Generation (Core/Driver Separation) ===")

    temp_dir = args.save_dir.strip() or os.path.join(
        f"data/codeflowbench/{args.lang}/{args.model_name}", "temp"
    )
    ensure_dir(temp_dir)
    logger.info(f"Output directory: {temp_dir}")

    if args.model_family not in MODELS_CONFIG:
        raise ValueError(f"Model family '{args.model_family}' not found in model_config.py")

    conf = MODELS_CONFIG[args.model_family]
    client = OpenAI(base_url=conf["base_url"], api_key=conf["api_key"])

    # Load tasks
    records: List[Dict[str, object]] = []
    if args.input_path.endswith(".jsonl"):
        with open(args.input_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(json.loads(line))
    else:
        with open(args.input_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, list):
                records = data
            else:
                records = [data]

    if args.right >= 0:
        records = records[args.left : args.right]
    else:
        records = records[args.left :]

    logger.info(f"Loaded {len(records)} tasks from {args.input_path}")

    n_proc = max(1, int(args.n_proc))
    logger.info(f"Using {n_proc} worker threads")

    with ThreadPoolExecutor(max_workers=n_proc) as executor:
        futures = [
            executor.submit(process_record, row, args, client, temp_dir) for row in records
        ]
        for _ in tqdm(as_completed(futures), total=len(futures), desc="Baseline Generating"):
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("--model_family", required=True)
    parser.add_argument("--model_name", required=True)
    parser.add_argument("--input_path", required=True)
    parser.add_argument("--save_dir", type=str, default="")
    parser.add_argument("--reuse", action="store_true")
    parser.add_argument("--n_proc", type=int, default=1)
    parser.add_argument("--lang", type=str, default="c", choices=["c", "cpp"])
    parser.add_argument("--left", type=int, default=0)
    parser.add_argument("--right", type=int, default=-1)

    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--timeout", type=int, default=1200)

    args = parser.parse_args()

    setup_logging(log_dir="log/base", log_file="infer_base.log")
    main(args)
