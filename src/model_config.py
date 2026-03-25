import os
from utils.logger_util import get_logger
import json
from time import sleep
# If tiktoken is available, use it for more accurate counting
import tiktoken
"""
Centralized configuration for all model settings.
Each model family should have a unique key (e.g. "openai_v1", "deepseek_official").
The value is a dictionary containing 'api_key', 'base_url', and 'model_name'.

**Security Note**: 
It is strongly recommended to use environment variables for API keys instead of hardcoding them.
os.getenv("YOUR_ENV_VARIABLE_NAME", "your_default_key") will first try to read from the environment variable,
and fall back to the provided default value if not found.
"""

MODELS_CONFIG = {
    "openai": {
        "api_key": os.getenv("OPENAI_API_KEY", "your-api-key-here"),
        "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "model_name": "gpt-5"
    },
    "deepseek": {
        "api_key": os.getenv("DEEPSEEK_API_KEY", "your-api-key-here"),
        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        "model_name": "deepseek-chat"
    },
    "gemini": {
        "api_key": os.getenv("GEMINI_API_KEY", "your-api-key-here"),
        "base_url": os.getenv("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1"),
        "model_name": "gemini-2.5-flash-nothinking"
    },
    "claude": {
        "api_key": os.getenv("CLAUDE_API_KEY", "your-api-key-here"),
        "base_url": os.getenv("CLAUDE_BASE_URL", "https://api.anthropic.com/v1"),
        "model_name": "claude-3-7-sonnet-20250219"
    },
}


@get_logger
def call_model(
    logger, client, messages, model="gpt-4o-mini", temperature=1.0, timeout=1200
):
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        
        logger.debug(f"call_model: Attempt {attempt} for model {model}")

        # Count character length
        chars_length = len(json.dumps(messages, ensure_ascii=False))
        # Count token length using tiktoken if available
        if tiktoken:
            try:
                enc = tiktoken.encoding_for_model(model)
            except Exception:
                enc = tiktoken.get_encoding("cl100k_base")
            tokens_length = sum(len(enc.encode(msg.get("content", ""))) for msg in messages)
        else:
            tokens_length = "Unavailable (tiktoken not installed)"
        logger.info(
            f"call_model: Messages chars_length={chars_length}, tokens_length={tokens_length}"
        )
        try:
            model_response = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                timeout=timeout,
            )
            # 1. Extract response content
            response_content = model_response.choices[0].message.content
            
            # 2. Get precise token usage from API response
            if model_response.usage:
                prompt_tokens = model_response.usage.prompt_tokens
                completion_tokens = model_response.usage.completion_tokens
                total_tokens = model_response.usage.total_tokens
                logger.info(
                    f"call_model: API Usage - Input: {prompt_tokens} tokens, Output: {completion_tokens} tokens, Total: {total_tokens} tokens."
                )
            else:
                logger.info("call_model: API usage stats not provided in the response.")

            # 3. Return response content
            return response_content
        
        
        except Exception as e:
            
            logger.warning(
                f"call_model: Attempt {attempt}/{max_retries} failed with error: {e}"
            )
            if attempt == max_retries:
                logger.error(
                    f"call_model: Failed after {max_retries} attempts. Exiting process."
                )
                # os._exit(1)
                return None
            else:
                sleep_time = 2 * attempt  # Incremental wait
                logger.info(f"Retrying after {sleep_time} seconds...")
                sleep(sleep_time)

