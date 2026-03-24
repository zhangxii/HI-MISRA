import re
import os

from utils.logger_util import get_logger


@get_logger
def extract_code(logger, text: str) -> str:
    """
    Robustly extract code from LLM-generated text.

    Strategies (in priority order):
    1. Exact match: fenced code block tagged as 'cpp', 'c++', or 'c'.
    2. Generic match: any fenced code block (```).
    3. Fallback delimiter: alternative '···' delimiter.
    4. Fault-tolerant parsing: line-by-line scan for malformed fenced blocks.
    5. Final fallback: return the entire text as code.
    """
    if not isinstance(text, str):
        return ""
    text = text.strip()
    if not text:
        return ""

    # Strategy 1: Exact match for C++/C code block
    match = re.search(r"```(?:cpp|c\+\+|c)\s*\n(.*?)```", text, flags=re.I | re.S)
    if match:
        return match.group(1).strip()

    # Strategy 2: Generic fenced code block
    match = re.search(r"```\w*\s*\n(.*?)```", text, flags=re.S)
    if match:
        return match.group(1).strip()

    # Strategy 3: Alternative ··· delimiter
    match = re.search(r"···\s*\n?(.*?)···", text, flags=re.S)
    if match:
        return match.group(1).strip()

    # Strategy 4: Fault-tolerant line-by-line parsing
    if "```" in text:
        lines = text.splitlines()
        code_lines = []
        in_code_block = False

        start_index = -1
        for i, line in enumerate(lines):
            if line.strip().startswith("```"):
                start_index = i
                in_code_block = True
                break

        if in_code_block:
            for i in range(start_index + 1, len(lines)):
                line = lines[i]
                if line.strip().startswith("```"):
                    break
                code_lines.append(line)
            return "\n".join(code_lines).strip()

    # Strategy 5: Final fallback
    return text


def ensure_dir(p):
    if not os.path.exists(p):
        os.makedirs(p, exist_ok=True)
