# HI-MISRA

> LLM-based MISRA C Violation Detection & Safe Code Synthesis

## Overview

HI-MISRA is a research project that leverages Large Language Models (LLMs) to detect MISRA C rule violations in C/C++ code and synthesize safer alternatives. It integrates with [Cppcheck](https://cppcheck.sourceforge.io/) for static analysis and supports multiple LLM backends (OpenAI, DeepSeek, Gemini, Claude) for intelligent code reasoning.

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
│   ├── codeflowbench/         # CodeFlowBench evaluation
│   │   ├── generate/          # Code generation pipeline
│   │   └── refine/            # Iterative refinement pipeline
│   ├── knowledge_build/       # MISRA knowledge construction
│   │   ├── check_misra.py     # MISRA rule checking
│   │   └── explain_misra.py   # MISRA rule explanation generation
│   ├── utils/                 # Shared utilities
│   │   ├── logger_util.py     # Logging utilities
│   │   ├── json_util.py       # JSON I/O helpers
│   │   ├── format_codeflow.py # CodeFlowBench data formatting
│   │   └── refine_util.py     # Refinement utilities
│   └── model_config.py        # LLM API configuration (env-based)
├── srcipt/                    # Shell scripts for batch experiments
│   ├── codeflowbench/
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

## Key Modules

### Knowledge Build (`src/knowledge_build`)

MISRA C 规则知识库的自动构建模块，整体流程：

```
misra.txt ──→ explain_misra.py ──→ misra_explaination.json ──→ check_misra.py ──→ 质量修复后的 JSON
 (规则文本)     (批量 LLM 生成)         (知识库)                (质量检查+重试)
```

- **`explain_misra.py`** — 读取 MISRA C 规则文本，多线程并发调用 LLM，为每条规则生成结构化解释（详细说明、违规代码示例、合规代码示例），每 20 条自动保存防止中断丢失。
- **`check_misra.py`** — 扫描已生成的知识库，自动检测异常条目（解析失败、字段缺失等）并调用 LLM 重新生成，支持 `--check_only` 仅检查模式和 `--retry_id` 指定规则重试。

生成的知识库在代码修复流程（`codeflowbench/refine`）中为 LLM 提供 MISRA 规则的详细解释和正反例参考。

## License

This project is for research purposes. Please refer to the license file for details.
