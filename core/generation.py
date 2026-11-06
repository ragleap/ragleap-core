"""
Answer Generation Service for RagLeap Core
Bring-your-own-key only — no system key, no fallback provided by RagLeap
itself. You can configure your OWN fallback chain across providers you
have keys for (see LLM_FALLBACK_PROVIDERS below).

Supports multiple LLM providers via LLM_PROVIDER env var (default: gemini).
Native SDK providers: gemini, anthropic
OpenAI-compatible providers (via openai SDK + custom base_url): everyone else.
"""
import os
import re
import logging
from typing import List, Dict, Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "gemini").lower()
GEMINI_CHAT_MODEL = os.environ.get("GEMINI_CHAT_MODEL", "gemini-3.5-flash")

DEFAULT_TEMPERATURE = float(os.environ.get("DEFAULT_TEMPERATURE", "0.3"))
MAX_OUTPUT_TOKENS = int(os.environ.get("MAX_OUTPUT_TOKENS", "1024"))

# Gemini "thinking" models (e.g. gemini-3.5-flash) spend part of
# max_output_tokens on internal reasoning before the visible answer,
# which can silently truncate the answer on hard queries. Default to
# 0 (thinking disabled) so the full token budget goes to the answer;
# override via GEMINI_THINKING_BUDGET if extended reasoning is wanted.
GEMINI_THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))

# Provider-agnostic truncation safeguard. Any provider/model — cloud or
# local, Gemini thinking-token starvation, a small Ollama model, anything —
# can hit its max_tokens cap mid-answer. Detected via each provider's own
# finish_reason field; on a match, generate_answer() retries once with a
# larger budget before falling through to the next provider in the chain.
# (Streaming cannot retry after tokens are already being sent to the user —
# not covered here; see generate_answer_stream docstring.)
TRUNCATED_FINISH_REASONS = {"MAX_TOKENS", "max_tokens", "length"}
TRUNCATION_RETRY_MULTIPLIER = float(os.environ.get("TRUNCATION_RETRY_MULTIPLIER", "2.0"))
TRUNCATION_MAX_RETRY_TOKENS = int(os.environ.get("TRUNCATION_MAX_RETRY_TOKENS", "4096"))
REASONING_MODE_TOKEN_MULTIPLIER = float(os.environ.get("REASONING_MODE_TOKEN_MULTIPLIER", "2.0"))
TOT_NUM_PATHS = int(os.environ.get("TOT_NUM_PATHS", "3"))
# Judge needs headroom: thinking-style models can spend the whole budget
# on internal reasoning and return an empty string (seen live on Groq at 20).
TOT_JUDGE_MAX_TOKENS = int(os.environ.get("TOT_JUDGE_MAX_TOKENS", "1024"))
TOT_CANDIDATE_TEMPERATURE = float(os.environ.get("TOT_CANDIDATE_TEMPERATURE", "0.8"))
# check_grounding() needs headroom too: an empty reply is treated as "grounded",
# so a thinking-style model exhausting a tiny budget would silently disable the check.
GROUNDING_CHECK_MAX_TOKENS = int(os.environ.get("GROUNDING_CHECK_MAX_TOKENS", "512"))
# Generic text returned to callers when every provider fails: the raw provider error
# (quota details, project ids, key hints) is logged server-side only.
GENERIC_FAILURE_MESSAGE = "Sorry, I couldn't generate an answer; all configured providers failed. Please try again later."

LLM_FALLBACK_PROVIDERS = [
    p.strip().lower() for p in os.environ.get("LLM_FALLBACK_PROVIDERS", "").split(",") if p.strip()
]

# Rough context-size budget for retrieved chunks, in characters (not exact
# tokens — a real per-provider tokenizer call would add latency and cost
# per request just to measure cost, which is counterproductive). ~4 chars
# per token is a widely-used English-text approximation. When the combined
# retrieved context exceeds this, lowest-ranked chunks are dropped first.
# Set to 0 to disable trimming entirely.
MAX_CONTEXT_CHARS = int(os.environ.get("MAX_CONTEXT_CHARS", "12000"))

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
    "qwen":       "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot":   "https://api.moonshot.cn/v1",
    "zhipu":      "https://open.bigmodel.cn/api/paas/v4",
    "yi":         "https://api.01.ai/v1",
    "baidu":      "https://qianfan.baidubce.com/v2",
    "minimax":    "https://api.minimax.chat/v1",
    "custom":     None,  # requires CUSTOM_BASE_URL env var
}

SYSTEM_PROMPT = """You are a helpful assistant that answers questions using ONLY the provided context.
If the answer is not in the context, say clearly that you don't have that information — do not make things up.
Always be concise and cite which document your answer came from when possible."""


def _resolve_provider_config(provider: str, required: bool = True) -> Optional[Dict]:
    """
    Resolve a provider name into {provider, api_key, model, base_url}.
    required=True (primary provider) raises ValueError on missing config.
    required=False (fallback providers) returns None and logs a warning.
    """
    provider = provider.lower()
    from core import settings

    def _fail(msg: str) -> Optional[Dict]:
        if required:
            raise ValueError(msg)
        logger.warning(f"Skipping fallback provider '{provider}': {msg}")
        return None

    if provider == "gemini":
        api_key = settings.get("GEMINI_API_KEY")
        if not api_key:
            return _fail(
                "GEMINI_API_KEY is not set. Get one at "
                "https://aistudio.google.com/apikey and add it to .env."
            )
        return {"provider": "gemini", "api_key": api_key, "model": settings.get("GEMINI_CHAT_MODEL", GEMINI_CHAT_MODEL), "base_url": None}

    elif provider == "anthropic":
        api_key = settings.get("ANTHROPIC_API_KEY")
        if not api_key:
            return _fail(
                "ANTHROPIC_API_KEY is not set. Get one at "
                "https://console.anthropic.com and add it to .env."
            )
        model = settings.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
        return {"provider": "anthropic", "api_key": api_key, "model": model, "base_url": None}

    elif provider in PROVIDER_BASE_URLS:
        key_env = f"{provider.upper()}_API_KEY"
        api_key = settings.get(key_env) or None
        model = settings.get(f"{provider.upper()}_MODEL", "")
        # Any provider's base URL can be overridden via {PROVIDER}_BASE_URL,
        # e.g. OLLAMA_BASE_URL=http://host.docker.internal:11434/v1 when
        # running in Docker (the "localhost" default only works when the
        # app runs on bare metal alongside Ollama, not inside a container).
        # 'custom' keeps its own CUSTOM_BASE_URL name for backward compat.
        if provider == "custom":
            base_url = settings.get("CUSTOM_BASE_URL") or None
        else:
            base_url = settings.get(f"{provider.upper()}_BASE_URL", PROVIDER_BASE_URLS[provider])

        if not api_key and provider != "ollama":
            return _fail(f"{key_env} is not set. Add your {provider} API key to .env.")
        if not base_url:
            return _fail(
                f"No base URL configured for provider '{provider}'. "
                f"Set CUSTOM_BASE_URL in .env if using 'custom'."
            )
        if not model:
            return _fail(f"{provider.upper()}_MODEL is not set in .env.")

        return {"provider": provider, "api_key": api_key, "model": model, "base_url": base_url}

    else:
        return _fail(
            f"Unknown provider '{provider}'. Supported: gemini, anthropic, "
            f"{', '.join(PROVIDER_BASE_URLS.keys())}."
        )


REASONING_SUFFIX = """

Before answering, think through this step by step: identify the relevant facts from the context, note any gaps or ambiguity, and reason toward your conclusion. Write your reasoning under "REASONING:". Then write your final, user-facing answer under "FINAL ANSWER:" -- this final answer should be complete and self-contained (the user will only see this part), not just a one-line reference back to your reasoning."""


def _split_reasoning_and_answer(text: str) -> Tuple[Optional[str], str]:
    """Split model output into (reasoning, final_answer). If the FINAL ANSWER marker is
    missing or the answer part is empty, fall back to (None, full text)."""
    if not text:
        return None, text
    matches = list(re.finditer(r"final\s*answer\s*:", text, re.IGNORECASE))
    if not matches:
        return None, text
    last = matches[-1]
    answer = text[last.end():].strip()
    if not answer:
        return None, text
    reasoning = text[:last.start()].strip()
    reasoning = re.sub(r"^\s*reasoning\s*:", "", reasoning, flags=re.IGNORECASE).strip()
    return (reasoning or None), answer


class GenerationService:
    """
    Generates a grounded answer using the configured LLM_PROVIDER,
    given a query and retrieved chunks. Supports per-call temperature/
    system_prompt/max_tokens overrides, streaming, a fallback chain
    across providers, context-size trimming, and real token usage
    reporting (for blocking calls).
    """

    def __init__(self):
        from core import settings
        name = LLM_PROVIDER
        if settings.source("LLM_PROVIDER") == "dashboard":
            name = settings.get("LLM_PROVIDER", LLM_PROVIDER).lower()
        self.primary_config = _resolve_provider_config(name, required=True)
        self.provider = self.primary_config["provider"]

    def _fallback_chain(self) -> List[Dict]:
        from core import settings
        names = LLM_FALLBACK_PROVIDERS
        if settings.source("LLM_FALLBACK_PROVIDERS") == "dashboard":
            names = [p.strip().lower() for p in settings.get("LLM_FALLBACK_PROVIDERS").split(",") if p.strip()]
        chain = [self.primary_config]
        for name in names:
            if name == self.primary_config["provider"]:
                continue
            config = _resolve_provider_config(name, required=False)
            if config:
                chain.append(config)
        return chain

    def _trim_chunks_to_budget(self, chunks: List[Dict]) -> List[Dict]:
        """
        Drop lowest-ranked chunks (chunks are expected to already be
        sorted by relevance) once the cumulative character count of
        retrieved text exceeds MAX_CONTEXT_CHARS. Set MAX_CONTEXT_CHARS=0
        to disable.
        """
        if MAX_CONTEXT_CHARS <= 0 or not chunks:
            return chunks

        kept = []
        running_total = 0
        for chunk in chunks:
            chunk_len = len(chunk.get("text", ""))
            if running_total + chunk_len > MAX_CONTEXT_CHARS and kept:
                # Keep at least one chunk even if it alone exceeds budget —
                # an answer with some context beats no context at all.
                break
            kept.append(chunk)
            running_total += chunk_len

        if len(kept) < len(chunks):
            logger.info(
                f"Trimmed context: {len(chunks)} -> {len(kept)} chunks "
                f"({running_total} chars, budget {MAX_CONTEXT_CHARS})"
            )
        return kept

    def _build_context(self, chunks: List[Dict]) -> str:
        if not chunks:
            return "No relevant context was found."
        parts = []
        for i, chunk in enumerate(chunks, start=1):
            doc_name = chunk.get("document_name", "unknown document")
            text = chunk.get("text", "")
            parts.append(f"[Source {i}: {doc_name}]\n{text}")
        return "\n\n".join(parts)

    def _build_prompt(self, query: str, chunks: List[Dict], system_prompt: Optional[str] = None, reasoning_mode: bool = False) -> str:
        context = self._build_context(chunks)
        instructions = system_prompt or SYSTEM_PROMPT
        prompt = f"""{instructions}

Context:
{context}

Question: {query}
Answer:"""
        if reasoning_mode:
            prompt = prompt + REASONING_SUFFIX
        return prompt

    def check_grounding(self, answer: str, chunks: List[Dict], query: str) -> Optional[str]:
        """
        Item #3 of the 9-pattern agentic-architecture build (self-
        correction/reflection). A cheap, bounded second LLM pass checking
        whether `answer` is actually supported by `chunks` -- NOT full
        chain-of-thought or a revision/regeneration loop, just one
        temperature=0 classification call.

        Uses only self.primary_config (no fallback chain) -- this is a
        secondary safety check on top of an already-generated answer,
        not the primary generation path, so it isn't worth the
        complexity of retrying across providers. If the check itself
        fails for any reason (provider error, malformed response), it
        is treated as inconclusive and silently skipped -- this must
        never block or replace the actual answer, same best-effort
        philosophy as core.observability.record_trace().

        Returns None if the answer appears grounded (or if the check
        was inconclusive), or a short string describing the concern if
        an unsupported claim was found.
        """
        context = self._build_context(chunks)
        prompt = f"""You are a strict fact-checker. Given the SOURCE CONTEXT and an ANSWER that was supposed to be based only on that context, determine if the ANSWER contains any claim that is NOT supported by the SOURCE CONTEXT.

SOURCE CONTEXT:
{context}

QUESTION: {query}

ANSWER TO CHECK:
{answer}

Respond with EXACTLY one line:
- "GROUNDED" if every factual claim in the ANSWER is supported by the SOURCE CONTEXT (or the answer correctly says it doesn't have the information)
- "NOT_GROUNDED: <short reason>" if the ANSWER contains a claim not supported by the SOURCE CONTEXT"""

        try:
            text, _usage = self._call_provider(self.primary_config, prompt, temperature=0.0, max_tokens=GROUNDING_CHECK_MAX_TOKENS)
            if not (text or "").strip():
                text, _usage = self._call_provider(
                    self.primary_config, prompt, temperature=0.0, max_tokens=GROUNDING_CHECK_MAX_TOKENS * 2
                )
            text = (text or "").strip()
            if text.upper().startswith("NOT_GROUNDED"):
                if ":" in text:
                    return text.split(":", 1)[1].strip() or "unsupported claim detected"
                return "unsupported claim detected"
            return None
        except Exception as e:
            logger.warning(f"Grounding check failed (non-fatal, treated as inconclusive): {e}")
            return None

    def tree_of_thought(self, query: str, chunks: List[Dict], temperature: float,
                        system_prompt: Optional[str] = None,
                        max_tokens: int = MAX_OUTPUT_TOKENS) -> Optional[Dict]:
        """
        Item #5 of the 9-pattern agentic build (tree of thought). Opt-in and
        expensive: TOT_NUM_PATHS candidate answers (separate calls, different
        approach hints) plus one pick call. Primary provider only, no fallback
        chain, same best-effort philosophy as check_grounding(): returns None
        if fewer than 2 candidates could be generated, and the caller then
        falls back to the normal single answer. Never raises.
        Returns {"answer": str, "reasoning": str} on success.
        """
        cfg = self.primary_config
        base_prompt = self._build_prompt(query, chunks, system_prompt)
        hints = [
            "Answer using the most directly relevant passages from the context.",
            "Answer cautiously: state only what the context explicitly supports, and say clearly what is missing.",
            "Answer by first weighing any conflicting or ambiguous passages, then give the best-supported answer.",
        ]
        n = max(2, min(TOT_NUM_PATHS, len(hints)))
        candidates = []
        for hint in hints[:n]:
            try:
                text, _u = self._call_provider(
                    cfg, base_prompt + "\n\nApproach: " + hint, TOT_CANDIDATE_TEMPERATURE, max_tokens
                )
                text = (text or "").strip()
                if text:
                    candidates.append(text)
            except Exception as e:
                logger.warning(f"Tree-of-thought candidate failed (non-fatal): {e}")
        if len(candidates) < 2:
            return None
        listing = "\n\n".join(f"ANSWER {i + 1}:\n{c}" for i, c in enumerate(candidates))
        pick_prompt = f"""You are a strict judge. Choose the ANSWER that is best supported by the SOURCE CONTEXT, invents no facts, and best addresses the QUESTION.

SOURCE CONTEXT:
{self._build_context(chunks)}

QUESTION: {query}

{listing}

Reply with ONLY the number of the best answer."""
        idx = 0
        picked = "default"
        try:
            pick_text, _u = self._call_provider(cfg, pick_prompt, 0.0, TOT_JUDGE_MAX_TOKENS)
            if not (pick_text or "").strip():
                pick_text, _u = self._call_provider(cfg, pick_prompt, 0.0, TOT_JUDGE_MAX_TOKENS * 2)
            m = re.search(r"\d+", pick_text or "")
            if m and 1 <= int(m.group()) <= len(candidates):
                idx = int(m.group()) - 1
                picked = "judge"
        except Exception as e:
            logger.warning(f"Tree-of-thought pick failed (non-fatal, using first candidate): {e}")
        reasoning = f"TREE OF THOUGHT (chosen={idx + 1}, by={picked})\n" + "\n".join(
            f"[{i + 1}] {c[:800]}" for i, c in enumerate(candidates)
        )
        return {"answer": candidates[idx], "reasoning": reasoning}

    def _call_provider(self, config: Dict, prompt: str, temperature: float, max_tokens: int) -> Tuple[str, Optional[Dict]]:
        """Every LLM call goes through here, so each one is recorded in the usage ledger
        (core.budget): provider-reported tokens, or an estimate when the provider reports
        none (e.g. Ollama). Recording is best-effort and never breaks a call."""
        text, usage = self._call_provider_raw(config, prompt, temperature, max_tokens)
        try:
            from core import budget  # lazy: avoids an import cycle
            budget.record_usage(config.get("provider"), config.get("model"), prompt, text, usage)
        except Exception as e:
            logger.warning(f"Usage recording failed (non-fatal): {e}")
        return text, usage

    def _call_provider_raw(self, config: Dict, prompt: str, temperature: float, max_tokens: int) -> Tuple[str, Optional[Dict]]:
        """Returns (answer_text, usage_dict_or_None). usage_dict has
        prompt_tokens/completion_tokens/total_tokens when the provider
        reports them."""
        provider = config["provider"]
        if provider == "gemini":
            return self._call_gemini(prompt, temperature, max_tokens, config["api_key"], config["model"])
        elif provider == "anthropic":
            return self._call_anthropic(prompt, temperature, max_tokens, config["api_key"], config["model"])
        else:
            return self._call_openai_compatible(
                prompt, temperature, max_tokens, config["api_key"], config["model"], config["base_url"]
            )

    def _stream_provider(self, config: Dict, prompt: str, temperature: float, max_tokens: int, result_holder: Optional[Dict] = None) -> Iterator[str]:
        provider = config["provider"]
        if provider == "gemini":
            yield from self._stream_gemini(prompt, temperature, max_tokens, config["api_key"], config["model"], result_holder)
        elif provider == "anthropic":
            yield from self._stream_anthropic(prompt, temperature, max_tokens, config["api_key"], config["model"], result_holder)
        else:
            yield from self._stream_openai_compatible(
                prompt, temperature, max_tokens, config["api_key"], config["model"], config["base_url"], result_holder
            )

    def generate_answer(
        self,
        query: str,
        chunks: List[Dict],
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
        reasoning_mode: bool = False,
        tot_mode: bool = False,
    ) -> Dict:
        """
        Generate an answer to `query` grounded in the given `chunks`.
        Returns: {"answer": str, "sources": List[str], "provider_used": str,
                  "usage": {"prompt_tokens": int, "completion_tokens": int,
                            "total_tokens": int} or None,
                  "chunks_sent": int, "fallback_used": bool,
                  "reasoning": Optional[str]}

        Retrieved chunks are trimmed to MAX_CONTEXT_CHARS before building
        the prompt (see _trim_chunks_to_budget) — chunks_sent reports how
        many actually made it into the prompt, which may be fewer than
        len(chunks) if trimming occurred.
        """
        temp = DEFAULT_TEMPERATURE if temperature is None else temperature
        max_tok = MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens

        trimmed_chunks = self._trim_chunks_to_budget(chunks)
        sources = list({c.get("document_name", "unknown") for c in trimmed_chunks})
        if tot_mode:
            tot = self.tree_of_thought(query, trimmed_chunks, temp, system_prompt, max_tok)
            if tot is not None:
                return {
                    "answer": tot["answer"],
                    "sources": sources,
                    "provider_used": self.primary_config["provider"],
                    "usage": None,
                    "chunks_sent": len(trimmed_chunks),
                    "fallback_used": False,
                    "reasoning": tot["reasoning"],
                }
        prompt = self._build_prompt(query, trimmed_chunks, system_prompt, reasoning_mode)
        if reasoning_mode:
            max_tok = int(max_tok * REASONING_MODE_TOKEN_MULTIPLIER)

        chain = self._fallback_chain()
        last_error = None

        for i, config in enumerate(chain):
            try:
                answer_text, usage = self._call_provider(config, prompt, temp, max_tok)
                if usage and usage.get("finish_reason") in TRUNCATED_FINISH_REASONS and max_tok < TRUNCATION_MAX_RETRY_TOKENS:
                    retry_max_tok = min(int(max_tok * TRUNCATION_RETRY_MULTIPLIER), TRUNCATION_MAX_RETRY_TOKENS)
                    if retry_max_tok > max_tok:
                        logger.warning(
                            f"Provider '{config['provider']}' truncated the answer "
                            f"(finish_reason={usage.get('finish_reason')!r}), retrying once with "
                            f"max_tokens={retry_max_tok} (was {max_tok})"
                        )
                        try:
                            retry_text, retry_usage = self._call_provider(config, prompt, temp, retry_max_tok)
                            answer_text, usage = retry_text, retry_usage
                        except Exception as retry_e:
                            logger.warning(
                                f"Truncation retry failed for '{config['provider']}': {retry_e}; "
                                f"keeping original (possibly truncated) answer"
                            )
                reasoning = None
                if reasoning_mode:
                    reasoning, answer_text = _split_reasoning_and_answer(answer_text)
                if i > 0:
                    logger.info(f"Answer generated via fallback provider '{config['provider']}' (primary failed)")
                return {
                    "answer": answer_text,
                    "sources": sources,
                    "provider_used": config["provider"],
                    "usage": usage,
                    "chunks_sent": len(trimmed_chunks),
                    "fallback_used": i > 0,
                    "reasoning": reasoning,
                }
            except Exception as e:
                last_error = e
                logger.warning(f"Provider '{config['provider']}' failed: {e}")
                continue

        logger.error(f"All providers in the fallback chain failed. Last error: {last_error}")
        return {
            "answer": GENERIC_FAILURE_MESSAGE,
            "sources": [],
            "provider_used": None,
            "usage": None,
            "chunks_sent": 0,
            "fallback_used": False,
            "reasoning": None,
        }

    def _record_stream_usage(self, config: Dict, prompt: str, pieces: List[str], result_holder: Dict) -> None:
        """Streams rarely report token counts, so the ledger estimates from the streamed text
        (core.budget). Best-effort: never breaks a stream."""
        try:
            from core import budget  # lazy: avoids an import cycle
            budget.record_usage(config.get("provider"), config.get("model"), prompt,
                                "".join(pieces), result_holder.get("usage"))
        except Exception as e:
            logger.warning(f"Stream usage recording failed (non-fatal): {e}")

    def generate_answer_stream(
        self,
        query: str,
        chunks: List[Dict],
        temperature: Optional[float] = None,
        system_prompt: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Iterator[str]:
        """
        Same as generate_answer(), but yields answer text incrementally.
        Chunks are trimmed to MAX_CONTEXT_CHARS the same way. Usage
        reporting is not available for streaming responses (each
        provider's streaming API handles usage differently — accurately
        supporting all three would need separate work; not done here).
        """
        temp = DEFAULT_TEMPERATURE if temperature is None else temperature
        max_tok = MAX_OUTPUT_TOKENS if max_tokens is None else max_tokens

        trimmed_chunks = self._trim_chunks_to_budget(chunks)
        prompt = self._build_prompt(query, trimmed_chunks, system_prompt)

        chain = self._fallback_chain()
        last_error = None

        for i, config in enumerate(chain):
            yielded_anything = False
            result_holder: Dict = {}
            collected: List[str] = []
            try:
                for piece in self._stream_provider(config, prompt, temp, max_tok, result_holder):
                    yielded_anything = True
                    collected.append(piece)
                    yield piece
                if result_holder.get("finish_reason") in TRUNCATED_FINISH_REASONS:
                    logger.warning(
                        f"Streamed answer truncated (finish_reason={result_holder.get('finish_reason')!r}); "
                        f"no mid-stream retry possible, notifying user instead"
                    )
                    yield "\n\n_[Note: this answer was cut short by the model's output limit.]_"
                self._record_stream_usage(config, prompt, collected, result_holder)
                if i > 0:
                    logger.info(f"Streamed via fallback provider '{config['provider']}' (primary failed)")
                return
            except Exception as e:
                last_error = e
                logger.warning(f"Provider '{config['provider']}' failed during streaming: {e}")
                if yielded_anything:
                    self._record_stream_usage(config, prompt, collected, result_holder)
                    logger.error(f"Streaming generation interrupted: {e}")
                    yield "\n[Error: generation interrupted]"
                    return
                continue

        logger.error(f"All providers in the fallback chain failed during streaming. Last error: {last_error}")
        yield GENERIC_FAILURE_MESSAGE

    def _call_gemini(self, prompt: str, temperature: float, max_tokens: int, api_key: str, model: str) -> Tuple[str, Optional[Dict]]:
        import google.genai as genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET),
            ),
        )
        text = response.text.strip() if response.text else "No answer generated."
        finish_reason = None
        if response.candidates:
            fr = response.candidates[0].finish_reason
            finish_reason = getattr(fr, "name", str(fr))
        usage = None
        if getattr(response, "usage_metadata", None):
            um = response.usage_metadata
            usage = {
                "prompt_tokens": um.prompt_token_count,
                "completion_tokens": um.candidates_token_count,
                "total_tokens": um.total_token_count,
                "finish_reason": finish_reason,
            }
        return text, usage

    def _stream_gemini(self, prompt: str, temperature: float, max_tokens: int, api_key: str, model: str, result_holder: Optional[Dict] = None) -> Iterator[str]:
        import google.genai as genai
        from google.genai import types
        client = genai.Client(api_key=api_key)
        stream = client.models.generate_content_stream(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=temperature,
                max_output_tokens=max_tokens,
                thinking_config=types.ThinkingConfig(thinking_budget=GEMINI_THINKING_BUDGET),
            ),
        )
        for chunk in stream:
            if chunk.text:
                yield chunk.text
            if result_holder is not None and chunk.candidates:
                fr = chunk.candidates[0].finish_reason
                if fr:
                    result_holder["finish_reason"] = getattr(fr, "name", str(fr))

    def _call_anthropic(self, prompt: str, temperature: float, max_tokens: int, api_key: str, model: str) -> Tuple[str, Optional[Dict]]:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip() if response.content else "No answer generated."
        usage = None
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
                "finish_reason": getattr(response, "stop_reason", None),
            }
        return text, usage

    def _stream_anthropic(self, prompt: str, temperature: float, max_tokens: int, api_key: str, model: str, result_holder: Optional[Dict] = None) -> Iterator[str]:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        with client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                yield text
            if result_holder is not None:
                try:
                    result_holder["finish_reason"] = stream.get_final_message().stop_reason
                except Exception:
                    pass

    def _call_openai_compatible(self, prompt: str, temperature: float, max_tokens: int, api_key: str, model: str, base_url: str) -> Tuple[str, Optional[Dict]]:
        import openai
        client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url)
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
        )
        content = response.choices[0].message.content if response.choices else None
        text = content.strip() if content else "No answer generated."   # same fallback as the Gemini and Anthropic paths
        usage = None
        if getattr(response, "usage", None):
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
                "finish_reason": response.choices[0].finish_reason if response.choices else None,
            }
        return text, usage

    def _stream_openai_compatible(self, prompt: str, temperature: float, max_tokens: int, api_key: str, model: str, base_url: str, result_holder: Optional[Dict] = None) -> Iterator[str]:
        import openai
        client = openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url)
        stream = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
        )
        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
            fr = chunk.choices[0].finish_reason
            if fr and result_holder is not None:
                result_holder["finish_reason"] = fr
