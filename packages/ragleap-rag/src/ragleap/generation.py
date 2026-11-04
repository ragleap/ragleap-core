"""
Answer generation for ragleap-rag.
Bring-your-own-key: supports Gemini, Anthropic, and any OpenAI-compatible
provider. Configure explicitly via ProviderConfig, or let it fall back to
environment variables for convenience (useful for scripts/notebooks).
"""
import os
import json
import logging
from dataclasses import dataclass, field
from typing import List, Dict, Iterator, Optional, Tuple

from ragleap.structured import parse_and_validate, parse_and_validate_object

logger = logging.getLogger(__name__)

PROVIDER_BASE_URLS = {
    "openai":     "https://api.openai.com/v1",
    "mistral":    "https://api.mistral.ai/v1",
    "groq":       "https://api.groq.com/openai/v1",
    "together":   "https://api.together.xyz/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama":     "http://localhost:11434/v1",
    "deepseek":   "https://api.deepseek.com/v1",
    "demonroute": "https://api.demonroute.com/v1",
    "cheaperinference": "https://api.cheaperinference.com/v1",
    "xai":        "https://api.x.ai/v1",
    "cohere":     "https://api.cohere.ai/v1",
    "perplexity": "https://api.perplexity.ai",
}

DEFAULT_SYSTEM_PROMPT = """You are a helpful assistant that answers questions using ONLY the provided context.
If the answer is not in the context, say clearly that you don't have that information — do not make things up.
Always be concise and cite which document your answer came from when possible."""


@dataclass
class ProviderConfig:
    """Explicit provider configuration. Use this for library integration
    rather than relying on environment variables."""
    provider: str
    api_key: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None

    def __post_init__(self):
        self.provider = self.provider.lower()

        # Convenience env-var fallback (e.g. for quick scripts) — explicit
        # values passed to the constructor always take precedence. No model
        # is ever hardcoded as a silent default: provider model names and
        # deprecations change frequently (learned the hard way - see
        # CHANGELOG v0.8.1/v0.9.0), so ragleap-rag always requires you to
        # know and specify which model you're using, one way or another.
        if self.provider == "gemini":
            self.api_key = self.api_key or os.environ.get("GEMINI_API_KEY")
            self.model = self.model or os.environ.get("GEMINI_CHAT_MODEL")
        elif self.provider == "anthropic":
            self.api_key = self.api_key or os.environ.get("ANTHROPIC_API_KEY")
            self.model = self.model or os.environ.get("ANTHROPIC_MODEL")
        elif self.provider in PROVIDER_BASE_URLS:
            self.api_key = self.api_key or os.environ.get(f"{self.provider.upper()}_API_KEY")
            self.model = self.model or os.environ.get(f"{self.provider.upper()}_MODEL")
            self.base_url = self.base_url or PROVIDER_BASE_URLS[self.provider]
        elif self.provider == "custom":
            self.base_url = self.base_url or os.environ.get("CUSTOM_BASE_URL")
        else:
            raise ValueError(
                f"Unknown provider '{self.provider}'. Supported: gemini, anthropic, custom, "
                f"{', '.join(PROVIDER_BASE_URLS.keys())}."
            )

        if not self.api_key and self.provider != "ollama":
            raise ValueError(f"No API key for provider '{self.provider}'. Pass api_key= explicitly.")
        if self.provider not in ("gemini", "anthropic") and not self.base_url:
            raise ValueError(f"No base_url for provider '{self.provider}'. Pass base_url= explicitly.")
        if not self.model and self.provider != "custom":
            env_var_name = "GEMINI_CHAT_MODEL" if self.provider == "gemini" else "ANTHROPIC_MODEL" if self.provider == "anthropic" else f"{self.provider.upper()}_MODEL"
            raise ValueError(
                f"No model specified for provider '{self.provider}'. Pass model= explicitly "
                f"to ProviderConfig(), or set {env_var_name} in your environment. "
                f"ragleap-rag never hardcodes a default model - provider model names and "
                f"deprecations change too frequently for a baked-in default to stay reliable."
            )
        if self.provider not in ("gemini", "anthropic") and not self.model:
            raise ValueError(f"No model for provider '{self.provider}'. Pass model= explicitly.")


class GenerationService:
    """
    Generates a grounded answer using the configured provider, with an
    optional fallback chain, streaming, and real token usage reporting.
    """

    def __init__(
        self,
        primary: ProviderConfig,
        fallbacks: Optional[List[ProviderConfig]] = None,
        default_temperature: float = 0.3,
        default_max_tokens: int = 1024,
        max_context_chars: int = 12000,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    ):
        self.primary = primary
        self.fallbacks = fallbacks or []
        self.default_temperature = default_temperature
        self.default_max_tokens = default_max_tokens
        self.max_context_chars = max_context_chars
        self.system_prompt = system_prompt

    def _chain(self, override_provider: Optional[ProviderConfig] = None) -> List[ProviderConfig]:
        if override_provider is not None:
            return [override_provider]
        return [self.primary] + [f for f in self.fallbacks if f.provider != self.primary.provider]

    def describe_image(self, image_bytes: bytes, mime_type: str = "image/jpeg", prompt: Optional[str] = None) -> str:
        """
        Use a vision-capable model to describe an image's contents -
        for photos, diagrams, or charts with no readable text to OCR.
        Currently only supports Gemini as the vision provider (the
        primary provider's Gemini config is used regardless of what
        the text-generation primary/fallback chain is set to).
        """
        gemini_config = None
        for config in self._chain():
            if config.provider == "gemini":
                gemini_config = config
                break
        if gemini_config is None:
            raise ValueError(
                "Vision captioning currently requires a Gemini provider configured "
                "(as primary or a fallback) - no Gemini config found in this chain."
            )

        import google.genai as genai
        from google.genai import types

        client = genai.Client(api_key=gemini_config.api_key)
        instruction = prompt or "Describe this image in detail, including any visible text, objects, people, charts, or diagrams."
        response = client.models.generate_content(
            model=gemini_config.model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                instruction,
            ],
        )
        text = response.text.strip() if response.text else ""
        if not text:
            raise ValueError("Vision model returned no description for this image.")
        return text

    def _trim_chunks_to_budget(self, chunks: List[Dict]) -> List[Dict]:
        if self.max_context_chars <= 0 or not chunks:
            return chunks
        kept, running_total = [], 0
        for chunk in chunks:
            chunk_len = len(chunk.get("text", ""))
            if running_total + chunk_len > self.max_context_chars and kept:
                break
            kept.append(chunk)
            running_total += chunk_len
        if len(kept) < len(chunks):
            logger.info(f"Trimmed context: {len(chunks)} -> {len(kept)} chunks ({running_total} chars)")
        return kept

    def _build_context(self, chunks: List[Dict]) -> str:
        if not chunks:
            return "No relevant context was found."
        parts = []
        for i, chunk in enumerate(chunks, start=1):
            doc_name = chunk.get("document_name", "unknown document")
            chunk_index = chunk.get("chunk_index", "?")
            parts.append(f"[Source {i}: {doc_name}, chunk {chunk_index}]\n{chunk.get('text', '')}")
        return "\n\n".join(parts)

    def _build_citations(self, chunks: List[Dict]) -> List[Dict]:
        """
        Structured citation list mapping each [Source N] label used in
        the prompt to the specific chunk it refers to - resolves the
        ambiguity of whether a citation like "(Source 1)" in an answer
        means a whole document or a specific passage. It is always the
        latter: chunk-level, not document-level.
        """
        citations = []
        for i, chunk in enumerate(chunks, start=1):
            text = chunk.get("text", "")
            citations.append({
                "source_number": i,
                "document_name": chunk.get("document_name", "unknown document"),
                "document_id": chunk.get("document_id"),
                "chunk_id": chunk.get("chunk_id"),
                "chunk_index": chunk.get("chunk_index"),
                "text_preview": text[:150] + ("..." if len(text) > 150 else ""),
            })
        return citations

    def _build_prompt(self, query: str, chunks: List[Dict], system_prompt: Optional[str], history_prefix: str = "") -> str:
        context = self._build_context(chunks)
        instructions = system_prompt or self.system_prompt
        return f"{instructions}\n\n{history_prefix}Context:\n{context}\n\nQuestion: {query}\nAnswer:"

    def generate_answer(
        self,
        query: str,
        chunks: List[Dict],
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        history_prefix: str = "",
        override_provider: Optional[ProviderConfig] = None,
        response_format: Optional[Dict] = None,
    ) -> Dict:
        """
        Returns: {"answer": str, "sources": List[str], "provider_used": str,
                  "usage": dict|None, "chunks_sent": int}
        """
        temp = self.default_temperature if temperature is None else temperature
        max_tok = self.default_max_tokens if max_tokens is None else max_tokens

        trimmed = self._trim_chunks_to_budget(chunks)
        # Each document once, in the order its first chunk was sent (a set would give a different order every run).
        sources = list(dict.fromkeys(c.get("document_name", "unknown") for c in trimmed))
        citations = self._build_citations(trimmed)
        prompt = self._build_prompt(query, trimmed, system_prompt, history_prefix)

        last_error = None
        for i, config in enumerate(self._chain(override_provider)):
            try:
                answer_text, usage, enforcement = self._call_provider(config, prompt, temp, max_tok, response_format)
                if i > 0:
                    logger.info(f"Answer generated via fallback provider '{config.provider}'")

                result = {
                    "answer": answer_text, "sources": sources, "citations": citations,
                    "provider_used": config.provider, "model_used": config.model, "usage": usage,
                    "chunks_sent": len(trimmed),
                }
                if response_format is not None:
                    structured, is_valid, method = parse_and_validate(answer_text, response_format)
                    result["structured"] = structured
                    result["structured_valid"] = is_valid
                    result["structured_enforcement"] = enforcement
                    result["structured_validation_method"] = method
                return result
            except Exception as e:
                last_error = e
                logger.warning(f"Provider '{config.provider}' failed: {e}")
                continue

        failure = {
            "answer": f"Sorry, all configured providers failed. Last error: {last_error}",
            "sources": [], "citations": [], "provider_used": None, "model_used": None, "usage": None, "chunks_sent": 0,
        }
        if response_format is not None:
            failure["structured"] = None
            failure["structured_valid"] = False
            failure["structured_enforcement"] = None
            failure["structured_validation_method"] = None
        return failure

    def generate_answer_stream(
        self,
        query: str,
        chunks: List[Dict],
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        history_prefix: str = "",
        override_provider: Optional[ProviderConfig] = None,
    ) -> Iterator[str]:
        """Streams the answer incrementally. Usage reporting isn't
        available for streaming (each provider handles it differently)."""
        temp = self.default_temperature if temperature is None else temperature
        max_tok = self.default_max_tokens if max_tokens is None else max_tokens

        trimmed = self._trim_chunks_to_budget(chunks)
        prompt = self._build_prompt(query, trimmed, system_prompt, history_prefix)

        last_error = None
        for i, config in enumerate(self._chain(override_provider)):
            yielded = False
            try:
                for piece in self._stream_provider(config, prompt, temp, max_tok):
                    yielded = True
                    yield piece
                return
            except Exception as e:
                last_error = e
                logger.warning(f"Provider '{config.provider}' failed during streaming: {e}")
                if yielded:
                    yield f"\n[Error: generation interrupted — {e}]"
                    return
                continue

        yield f"Sorry, all configured providers failed. Last error: {last_error}"

    def _call_provider(self, config: ProviderConfig, prompt: str, temperature: float, max_tokens: int, response_format: Optional[Dict] = None) -> Tuple[str, Optional[Dict], Optional[str]]:
        if config.provider == "gemini":
            return self._call_gemini(prompt, temperature, max_tokens, config.api_key, config.model, response_format)
        elif config.provider == "anthropic":
            return self._call_anthropic(prompt, temperature, max_tokens, config.api_key, config.model, response_format)
        else:
            return self._call_openai_compatible(prompt, temperature, max_tokens, config.api_key, config.model, config.base_url, response_format)

    def _stream_provider(self, config: ProviderConfig, prompt: str, temperature: float, max_tokens: int) -> Iterator[str]:
        if config.provider == "gemini":
            yield from self._stream_gemini(prompt, temperature, max_tokens, config.api_key, config.model)
        elif config.provider == "anthropic":
            yield from self._stream_anthropic(prompt, temperature, max_tokens, config.api_key, config.model)
        else:
            yield from self._stream_openai_compatible(prompt, temperature, max_tokens, config.api_key, config.model, config.base_url)

    def _call_gemini(self, prompt, temperature, max_tokens, api_key, model, response_format: Optional[Dict] = None) -> Tuple[str, Optional[Dict], Optional[str]]:
        import google.genai as genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        config_kwargs = {"temperature": temperature, "max_output_tokens": max_tokens}
        enforcement = None
        if response_format is not None:
            config_kwargs["response_mime_type"] = "application/json"
            config_kwargs["response_schema"] = response_format
            enforcement = "native"
        response = client.models.generate_content(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(**config_kwargs),
        )
        text = response.text.strip() if response.text else "No answer generated."
        usage = None
        if getattr(response, "usage_metadata", None):
            um = response.usage_metadata
            usage = {"prompt_tokens": um.prompt_token_count, "completion_tokens": um.candidates_token_count, "total_tokens": um.total_token_count}
        return text, usage, enforcement

    def _stream_gemini(self, prompt, temperature, max_tokens, api_key, model) -> Iterator[str]:
        import google.genai as genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        stream = client.models.generate_content_stream(
            model=model, contents=prompt,
            config=types.GenerateContentConfig(temperature=temperature, max_output_tokens=max_tokens),
        )
        for chunk in stream:
            if chunk.text:
                yield chunk.text

    def _call_anthropic(self, prompt, temperature, max_tokens, api_key, model, response_format: Optional[Dict] = None) -> Tuple[str, Optional[Dict], Optional[str]]:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        enforcement = None
        if response_format is not None:
            # Anthropic has no OpenAI-style response_format - the documented
            # mechanism is forcing a single tool call whose input_schema is
            # the desired output shape, then reading .input directly (it's
            # already a parsed dict, not a JSON string to re-parse).
            tool = {"name": "structured_response", "description": "Return the answer in the required structure.", "input_schema": response_format}
            response = client.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=[{"role": "user", "content": prompt}],
                tools=[tool], tool_choice={"type": "tool", "name": "structured_response"},
            )
            tool_use_block = next((b for b in response.content if b.type == "tool_use"), None)
            if tool_use_block is None:
                text = "No answer generated."
            else:
                text = json.dumps(tool_use_block.input)
            enforcement = "native"
        else:
            response = client.messages.create(model=model, max_tokens=max_tokens, temperature=temperature, messages=[{"role": "user", "content": prompt}])
            text = response.content[0].text.strip() if response.content else "No answer generated."
        usage = None
        if getattr(response, "usage", None):
            usage = {"prompt_tokens": response.usage.input_tokens, "completion_tokens": response.usage.output_tokens, "total_tokens": response.usage.input_tokens + response.usage.output_tokens}
        return text, usage, enforcement

    def _stream_anthropic(self, prompt, temperature, max_tokens, api_key, model) -> Iterator[str]:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(model=model, max_tokens=max_tokens, temperature=temperature, messages=[{"role": "user", "content": prompt}]) as stream:
            for text in stream.text_stream:
                yield text

    def _call_openai_compatible(self, prompt, temperature, max_tokens, api_key, model, base_url, response_format: Optional[Dict] = None) -> Tuple[str, Optional[Dict], Optional[str]]:
        import openai
        client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url)
        enforcement = None
        kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}], "temperature": temperature, "max_tokens": max_tokens}
        if response_format is not None:
            # Not every OpenAI-compatible provider supports strict schema
            # mode (json_schema) - honestly fall back to unconstrained
            # json_object mode rather than failing the whole call, and
            # flag which one actually ran via `enforcement`.
            try:
                strict_kwargs = dict(kwargs)
                strict_kwargs["response_format"] = {"type": "json_schema", "json_schema": {"name": "structured_response", "schema": response_format, "strict": True}}
                response = client.chat.completions.create(**strict_kwargs)
                enforcement = "native"
            except Exception as e:
                logger.warning(f"Provider does not support strict json_schema mode, falling back to json_object: {e}")
                fallback_kwargs = dict(kwargs)
                fallback_kwargs["response_format"] = {"type": "json_object"}
                response = client.chat.completions.create(**fallback_kwargs)
                enforcement = "json_object_fallback"
        else:
            response = client.chat.completions.create(**kwargs)
        text = response.choices[0].message.content.strip() if response.choices else "No answer generated."
        usage = None
        if getattr(response, "usage", None):
            usage = {"prompt_tokens": response.usage.prompt_tokens, "completion_tokens": response.usage.completion_tokens, "total_tokens": response.usage.total_tokens}
        return text, usage, enforcement

    def _stream_openai_compatible(self, prompt, temperature, max_tokens, api_key, model, base_url) -> Iterator[str]:
        import openai
        client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url)
        stream = client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], temperature=temperature, max_tokens=max_tokens, stream=True)
        for chunk in stream:
            delta = chunk.choices[0].delta.content if chunk.choices else None
            if delta:
                yield delta
