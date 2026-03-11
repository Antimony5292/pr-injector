"""LLM client wrapper for semantic injection (Azure OpenAI + litellm)."""

from __future__ import annotations

from tenacity import retry, stop_after_attempt, wait_exponential

from pr_injector.core.exceptions import SemanticInjectionFailed
from pr_injector.core.logging import get_logger
from pr_injector.llm.prompts import build_semantic_injection_prompt
from pr_injector.llm.validator import (
    estimate_confidence,
    extract_diff_from_response,
    validate_diff_syntax,
)

logger = get_logger(__name__)


class LLMClient:
    """Wrapper for Level 3 semantic injection.

    Supports two providers:
    - ``azure``: Azure OpenAI with Azure AD authentication (DefaultAzureCredential).
    - ``litellm``: Any model via litellm (API-key based).
    """

    def __init__(
        self,
        provider: str = "azure",
        # Azure-specific
        azure_endpoint: str = "",
        azure_deployment: str = "",
        azure_api_version: str = "2024-12-01-preview",
        # litellm-specific
        model: str = "claude-sonnet-4-20250514",
        api_key: str | None = None,
        # Common
        temperature: float = 0.2,
        max_tokens: int = 4096,
        max_retries: int = 3,
    ) -> None:
        self.provider = provider
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

        if provider == "azure":
            self.azure_deployment = azure_deployment
            self.model = azure_deployment
            self._azure_client = self._create_azure_client(
                azure_endpoint, azure_api_version
            )
        else:
            self.model = model
            self.api_key = api_key
            self._azure_client = None

    @staticmethod
    def _create_azure_client(endpoint: str, api_version: str):
        """Create an AzureOpenAI client with Azure AD token provider."""
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
        from openai import AzureOpenAI

        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        return AzureOpenAI(
            azure_endpoint=endpoint,
            azure_ad_token_provider=token_provider,
            api_version=api_version,
        )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        reraise=True,
    )
    async def _call_llm(
        self, system_prompt: str, user_prompt: str
    ) -> tuple[str, int, int]:
        """Make a single LLM API call.

        Returns:
            Tuple of (response_text, prompt_tokens, completion_tokens).
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        if self.provider == "azure":
            response = await self._call_azure(messages)
        else:
            response = await self._call_litellm(messages)

        content, prompt_tokens, completion_tokens = response
        self._total_prompt_tokens += prompt_tokens
        self._total_completion_tokens += completion_tokens

        return content, prompt_tokens, completion_tokens

    async def _call_azure(
        self, messages: list[dict]
    ) -> tuple[str, int, int]:
        """Call Azure OpenAI using the native SDK (sync client in thread)."""
        import asyncio

        loop = asyncio.get_running_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._azure_client.chat.completions.create(
                model=self.azure_deployment,
                messages=messages,
                max_completion_tokens=self.max_tokens,
            ),
        )

        choice = response.choices[0]
        content = choice.message.content or ""

        # Debug: log response metadata to diagnose empty responses
        logger.info(
            "azure_response_metadata",
            finish_reason=choice.finish_reason,
            content_length=len(content),
            has_refusal=getattr(choice.message, "refusal", None),
        )

        if not content and choice.finish_reason == "content_filter":
            logger.warning("azure_content_filtered", finish_reason=choice.finish_reason)

        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        return content, prompt_tokens, completion_tokens

    async def _call_litellm(
        self, messages: list[dict]
    ) -> tuple[str, int, int]:
        """Call LLM via litellm."""
        import litellm

        litellm.suppress_debug_info = True

        kwargs: dict = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key

        response = await litellm.acompletion(**kwargs)

        content = response.choices[0].message.content or ""
        usage = response.usage
        prompt_tokens = usage.prompt_tokens if usage else 0
        completion_tokens = usage.completion_tokens if usage else 0
        return content, prompt_tokens, completion_tokens

    async def generate_semantic_injection(
        self,
        issue_description: str,
        original_diff: str,
        current_files: dict[str, str],
        target_functions: list[str] | None = None,
    ) -> tuple[str, float, int, int]:
        """Generate a semantic bug injection using LLM.

        Args:
            issue_description: Original issue/PR description.
            original_diff: The original fix diff.
            current_files: Dict of {file_path: file_content}.
            target_functions: Optional function names to focus on.

        Returns:
            Tuple of (unified_diff, confidence_score, prompt_tokens, completion_tokens).

        Raises:
            SemanticInjectionFailed: If LLM cannot produce valid injection.
        """
        system_prompt, user_prompt = build_semantic_injection_prompt(
            issue_description=issue_description,
            original_diff=original_diff,
            current_files=current_files,
            target_functions=target_functions,
        )

        logger.info(
            "llm_injection_start",
            model=self.model,
            provider=self.provider,
            files=list(current_files.keys()),
        )

        try:
            response_text, prompt_tokens, completion_tokens = await self._call_llm(
                system_prompt, user_prompt
            )
        except Exception as e:
            raise SemanticInjectionFailed(f"LLM API call failed: {e}") from e

        # Extract diff from response
        logger.debug(
            "llm_raw_response",
            response_length=len(response_text),
            response_preview=response_text[:500],
        )
        diff = extract_diff_from_response(response_text)
        if diff is None:
            logger.warning(
                "llm_diff_extraction_failed",
                response_preview=response_text[:1000],
            )
            raise SemanticInjectionFailed(
                "LLM response does not contain a valid diff"
            )

        # Validate diff syntax
        valid, errors = validate_diff_syntax(diff)
        if not valid:
            raise SemanticInjectionFailed(
                f"LLM-generated diff has syntax errors: {'; '.join(errors)}"
            )

        # Estimate confidence
        confidence = estimate_confidence(diff, original_diff)

        logger.info(
            "llm_injection_complete",
            confidence=confidence,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )

        return diff, confidence, prompt_tokens, completion_tokens

    @property
    def total_prompt_tokens(self) -> int:
        """Total prompt tokens used across all calls."""
        return self._total_prompt_tokens

    @property
    def total_completion_tokens(self) -> int:
        """Total completion tokens used across all calls."""
        return self._total_completion_tokens

    @property
    def total_tokens(self) -> int:
        """Total tokens used."""
        return self._total_prompt_tokens + self._total_completion_tokens
