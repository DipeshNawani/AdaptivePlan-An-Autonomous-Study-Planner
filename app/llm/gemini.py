"""Google Gemini LLM Provider with native function calling, resilient multi-tier fallback, and fast execution."""
import time
import logging
import concurrent.futures
from typing import List, Dict, Any, Optional, Callable
from app.config import GEMINI_API_KEY, GEMINI_MODEL
from app.llm.provider import LLMProvider, LLMResponse, ToolCallRequest
from app.llm.mock import MockLLMProvider

logger = logging.getLogger(__name__)

# Timeout in seconds for any Gemini API call before falling back
DEFAULT_GEMINI_TIMEOUT_SEC = 30.0

# Resilient, low-latency fallback model when a primary model suffers 503 spikes or 504 deadline exceeded
FALLBACK_FAST_MODEL = "gemini-3.1-flash-lite"

def _run_with_timeout(func: Callable, *args, timeout_sec: float = DEFAULT_GEMINI_TIMEOUT_SEC, **kwargs) -> Any:
    """Execute a function inside a ThreadPoolExecutor with a strict timeout."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(func, *args, **kwargs)
        return future.result(timeout=timeout_sec)

class GeminiProvider(LLMProvider):
    """
    Google Gemini provider using the official `google-genai` SDK.
    Features robust error recovery, automatic fallback to low-latency gemini-3.1-flash-lite on 503/504,
    and graceful per-run fallback to MockLLMProvider if Gemini API is unreachable.
    Does NOT mutate global mode on per-run failure.
    """

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or GEMINI_API_KEY
        self.model_name = model or GEMINI_MODEL
        self._client = None

        if self.api_key:
            try:
                from google import genai
                from google.genai import types
                # Set timeout to 30s and attempts=1 to prevent 5-minute tenacity retry loops
                http_options = types.HttpOptions(
                    timeout=int(DEFAULT_GEMINI_TIMEOUT_SEC * 1000),
                    retry_options=types.HttpRetryOptions(attempts=1)
                )
                self._client = genai.Client(api_key=self.api_key, http_options=http_options)
            except Exception as e:
                logger.warning(f"Could not initialize Google GenAI client: {e}.")
                self._client = None

    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        if not self._client:
            mock = MockLLMProvider(is_fallback=True, fallback_reason="Client not initialized")
            return mock.generate(prompt, system_prompt)

        from google.genai import types
        config = types.GenerateContentConfig()
        if system_prompt:
            config.system_instruction = system_prompt

        models_to_try = [self.model_name]
        if self.model_name != FALLBACK_FAST_MODEL:
            models_to_try.append(FALLBACK_FAST_MODEL)

        last_error = None
        for m in models_to_try:
            try:
                def _call():
                    return self._client.models.generate_content(
                        model=m,
                        contents=prompt,
                        config=config
                    )
                response = _run_with_timeout(_call, timeout_sec=DEFAULT_GEMINI_TIMEOUT_SEC)
                return response.text or ""
            except Exception as e:
                last_error = e
                logger.warning(f"Gemini generate API error on {m}: {e}. Trying next option...")

        logger.error(f"All Gemini models failed ({last_error}). Per-run fallback to MockLLMProvider.")
        mock = MockLLMProvider(is_fallback=True, fallback_reason=str(last_error))
        return mock.generate(prompt, system_prompt)

    def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: List[Dict[str, Any]],
        system_prompt: Optional[str] = None
    ) -> LLMResponse:
        if not self._client:
            mock = MockLLMProvider(is_fallback=True, fallback_reason="Gemini client unavailable")
            return mock.generate_with_tools(messages, tools_schema, system_prompt)

        from google.genai import types

        # Format tool declarations for Gemini SDK
        func_declarations = []
        for t in tools_schema:
            func_declarations.append(types.FunctionDeclaration(
                name=t["name"],
                description=t.get("description", ""),
                parameters=t.get("parameters", {})
            ))

        tools = [types.Tool(function_declarations=func_declarations)] if func_declarations else None
        config = types.GenerateContentConfig(tools=tools)
        if system_prompt:
            config.system_instruction = system_prompt

        # Convert message history to string or contents format
        contents = []
        for msg in messages:
            role = "user" if msg.get("role") in ("user", "system") else "model"
            contents.append(f"{role.upper()}: {msg.get('content', '')}")

        prompt_text = "\n".join(contents)

        models_to_try = [self.model_name]
        if self.model_name != FALLBACK_FAST_MODEL:
            models_to_try.append(FALLBACK_FAST_MODEL)

        last_error = None
        for m in models_to_try:
            try:
                def _call():
                    return self._client.models.generate_content(
                        model=m,
                        contents=prompt_text,
                        config=config
                    )
                response = _run_with_timeout(_call, timeout_sec=DEFAULT_GEMINI_TIMEOUT_SEC)

                # Check for function calls
                tool_calls: List[ToolCallRequest] = []
                if response.function_calls:
                    for fc in response.function_calls:
                        args = dict(fc.args) if fc.args else {}
                        tool_calls.append(ToolCallRequest(name=fc.name, arguments=args))

                return LLMResponse(
                    content=response.text,
                    tool_calls=tool_calls,
                    finish_reason="function_call" if tool_calls else "stop",
                    is_mock=False,
                    is_fallback=False,
                    provider_name="Gemini",
                    model_name=m
                )
            except Exception as e:
                last_error = e
                logger.warning(f"Gemini function calling error on {m}: {e}. Trying next option...")

        logger.error(f"All Gemini models failed ({last_error}). Falling back to MockLLMProvider for this run.")
        mock = MockLLMProvider(is_fallback=True, fallback_reason=str(last_error))
        resp = mock.generate_with_tools(messages, tools_schema, system_prompt)
        resp.is_fallback = True
        resp.fallback_reason = str(last_error)
        return resp

def test_gemini_connection() -> Dict[str, Any]:
    """Test Gemini connection with a quick ping call and measure latency."""
    from app.config import GEMINI_API_KEY, GEMINI_MODEL
    if not GEMINI_API_KEY:
        return {
            "success": False,
            "latency_ms": 0,
            "message": "GEMINI_API_KEY is not set in .env"
        }

    t0 = time.time()
    try:
        from google import genai
        from google.genai import types
        # Google requires minimum deadline >= 10s. Set to 20s (20000ms) for reliable ping
        http_options = types.HttpOptions(
            timeout=20000,
            retry_options=types.HttpRetryOptions(attempts=1)
        )
        client = genai.Client(api_key=GEMINI_API_KEY, http_options=http_options)

        def _ping():
            return client.models.generate_content(
                model=GEMINI_MODEL,
                contents="Say 'OK' in one word."
            )

        _run_with_timeout(_ping, timeout_sec=20.0)
        latency = int((time.time() - t0) * 1000)
        return {
            "success": True,
            "latency_ms": latency,
            "message": f"Connected to {GEMINI_MODEL} ({latency}ms)",
            "model": GEMINI_MODEL
        }
    except Exception as e:
        latency = int((time.time() - t0) * 1000)
        return {
            "success": False,
            "latency_ms": latency,
            "message": f"Connection failed: {str(e)}",
            "model": GEMINI_MODEL
        }

def get_llm_provider() -> LLMProvider:
    """
    Factory helper to obtain the active LLM provider.
    Evaluated fresh on every run; no module-level caching.
    """
    from app.db import get_current_llm_mode
    mode = get_current_llm_mode()
    if mode == "live" and GEMINI_API_KEY:
        return GeminiProvider()
    return MockLLMProvider()
