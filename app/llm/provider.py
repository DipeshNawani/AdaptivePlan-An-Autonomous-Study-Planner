"""Abstract LLM Provider interface and response structures."""
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
from pydantic import BaseModel

class ToolCallRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]

class LLMResponse(BaseModel):
    content: Optional[str] = None
    tool_calls: List[ToolCallRequest] = []
    finish_reason: Optional[str] = None
    is_mock: bool = False
    is_fallback: bool = False
    provider_name: str = "Mock LLM"
    model_name: str = "mock"
    fallback_reason: Optional[str] = None

class LLMProvider(ABC):
    """Abstract interface for LLM providers (Gemini, Mock, etc.)."""

    @abstractmethod
    def generate(self, prompt: str, system_prompt: Optional[str] = None) -> str:
        """Simple text generation."""
        pass

    @abstractmethod
    def generate_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools_schema: List[Dict[str, Any]],
        system_prompt: Optional[str] = None
    ) -> LLMResponse:
        """Generate response with function calling support."""
        pass
