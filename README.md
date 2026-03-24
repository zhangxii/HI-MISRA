# HI-MISRA

> LLM-based MISRA C Violation Detection & Safe Code Synthesis

## Overview

HI-MISRA is a research project that leverages Large Language Models (LLMs) to detect MISRA C rule violations in C/C++ code and synthesize safer alternatives. It integrates with [Cppcheck](https://cppcheck.sourceforge.io/) for static analysis and supports multiple LLM backends (OpenAI, DeepSeek, Gemini, Claude) for intelligent code reasoning.

![HI-MISRA Workflow](figures/workflow.png)

## Project Structure

```
HI-MISRA/
├── src/
│   ├── check/                 # Core MISRA checking module
│   │   ├── misra_check.py     # Cppcheck + MISRA violation detection engine
│   │   ├── check_main.py      # Batch checking entry point
│   │   ├── check_humaneval_cpp.py
│   │   ├── check_leetcode_cpp.py
│   │   └── check_codeflow_bench.py
│   ├── pipeline/              # Code generation & refinement pipeline
│   │   ├── generate/          # Code generation pipeline
│   │   └── refine/            # Iterative refinement pipeline
│   ├── knowledge_build/       # MISRA knowledge construction
│   │   ├── check_misra.py     # Knowledge quality check & retry
│   │   └── explain_misra.py   # Rule explanation generation
│   ├── utils/                 # Shared utilities
│   │   ├── logger_util.py     # Logging utilities
│   │   ├── json_util.py       # JSON I/O helpers
│   │   └── refine_util.py     # Refinement utilities
│   └── model_config.py        # LLM API configuration (env-based)
├── srcipt/                    # Shell scripts for batch experiments
│   ├── pipeline/
│   ├── humanevalx/
│   ├── knowledge_build/
│   ├── codeflaws/
│   └── manybugs/
├── empirical_study/           # Empirical study scripts
├── setup.py                   # Package setup
├── pyproject.toml             # Build system config
└── .gitignore
```

## Getting Started

### Prerequisites

- Python ≥ 3.8
- [Cppcheck](https://cppcheck.sourceforge.io/) installed and available in `PATH`
- LLM API keys (set via environment variables)

### Installation

```bash
git clone <repo-url> && cd HI-MISRA

pip install -e .
```

### Environment Variables

Configure your LLM API keys via environment variables:

```bash
export OPENAI_API_KEY="your-key"
export OPENAI_BASE_URL="https://api.openai.com/v1"

export DEEPSEEK_API_KEY="your-key"
export GEMINI_API_KEY="your-key"
export CLAUDE_API_KEY="your-key"
```

## Knowledge Build (`src/knowledge_build`)

Automated construction module for the MISRA C rule knowledge base. The overall pipeline:

```
misra.txt ──→ explain_misra.py ──→ misra_explaination.json ──→ check_misra.py ──→ Quality-checked JSON
 (rule text)    (batch LLM gen)       (knowledge base)          (quality check + retry)
```

- **`explain_misra.py`** — Reads MISRA C rule text, calls LLM concurrently with multi-threading to generate structured explanations for each rule (detailed description, non-compliant code examples, and compliant code examples). Auto-saves every 20 rules to prevent data loss on interruption.
- **`check_misra.py`** — Scans the generated knowledge base, automatically detects abnormal entries (parse failures, missing fields, etc.) and re-generates them via LLM. Supports `--check_only` mode for inspection only and `--retry_id` for targeted rule retry.

The generated knowledge base provides MISRA rule explanations and compliant/non-compliant examples as reference context for the LLM during the code refinement pipeline (`pipeline/refine`).

## License

This project is for research purposes. Please refer to the license file for details.
