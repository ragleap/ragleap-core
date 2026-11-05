"""
Chat Pipeline for RagLeap Core
Wires together: query language detection -> embedding the query -> hybrid
(dense + sparse, graph-boosted) retrieval -> answer generation (blocking
or streaming).
"""
import logging
import time
from typing import Iterator, Optional

from core.embedding import EmbeddingService
from core.retrieval import VectorRetrievalService
from core.generation import GenerationService, SYSTEM_PROMPT as DEFAULT_SYSTEM_PROMPT
from core.language import language_detector
from core.employees import skills as employee_skills
from core.employees import memory as employee_memory
from core.employees.defaults import SENSITIVE_DOMAIN_ROLES
from core.employees.sensitivity import is_sensitive_role
from core.employees.supervisor import route_task
from core.employees.team import run_team
from core.employees.actions import maybe_act, describe_action
from core import agent_loop
from core import budget
from core.observability import record_trace

logger = logging.getLogger(__name__)


def _prepare(query: str, top_k: int, hybrid: bool):
    """Shared setup for both ask() and ask_stream(): detect language,
    embed the query, retrieve chunks. Returns (chunks, detected_language,
    embedding_failed: bool).

    hybrid=True (default) uses dense+sparse fusion (search_hybrid_chunks).
    hybrid=False uses dense-only, graph-boosted retrieval (the older
    search_similar_chunks_with_graph) — kept as an option since it's a
    cheaper query path (one search instead of two) for callers that
    don't need the sparse/keyword-matching benefit.
    """
    embedder = EmbeddingService()
    retriever = VectorRetrievalService()

    try:
        detected_language, _confidence = language_detector.detect_query_language(query)
    except Exception as e:
        logger.warning(f"Query language detection failed (non-fatal): {e}")
        detected_language = None

    query_embedding = embedder.embed_text(query)
    if query_embedding is None:
        return [], detected_language, True

    if hybrid:
        chunks = retriever.search_hybrid_chunks(query, query_embedding, top_k=top_k)
    else:
        chunks = retriever.search_similar_chunks_with_graph(query, query_embedding, top_k=top_k)

    return chunks, detected_language, False


def _build_system_prompt(role: Optional[str], query: str, base_system_prompt: Optional[str]):
    """
    When a role is given, layers the role's personality and role-scoped
    context (owner instructions, business profile, learned patterns) on
    top of a grounding instruction that explicitly treats BOTH the role
    context and the retrieved document chunks as valid context to answer
    from. Without a role, behaves exactly as before (returns
    base_system_prompt as-is, including None).

    Note: the plain DEFAULT_SYSTEM_PROMPT says to answer "using ONLY the
    provided context" — when a model reads that alongside role/business
    info elsewhere in the prompt, it correctly interprets "context" as
    meaning only the retrieved-chunks block, and ignores the role info
    entirely. So when a role is set, the grounding instruction itself is
    rewritten to be explicit that both sources are legitimate context —
    this was verified to matter in testing (identical answers with/without
    role until this fix).
    """
    if not role:
        return base_system_prompt, []

    personality = employee_skills.get_role_personality(role)
    role_context, role_memory_ids = employee_skills.get_role_skills_with_ids(role=role, query=query)

    grounding = base_system_prompt or (
        "You are a helpful assistant. Answer questions using the business "
        "context and role information below AND the retrieved document "
        "context. If the answer isn't in either, say clearly that you "
        "don't have that information — do not make things up. Always be "
        "concise and cite which document your answer came from when the "
        "answer draws on a document."
    )

    parts = []
    if role_context and "OWNER INSTRUCTIONS" in role_context:
        owner_block, _, rest = role_context.partition("\n\n")
        parts.append(
            "MANDATORY: The following owner instructions must be followed in "
            "every response, regardless of whether the specific fact is also "
            "found in the retrieved documents:\n" + owner_block
        )
        role_context = rest
    if personality:
        parts.append(personality)
    if role_context:
        parts.append("Business and role context (treat as authoritative, alongside retrieved documents):\n" + role_context)
    parts.append(grounding)
    return "\n\n".join(parts), role_memory_ids


def _augment_query_with_reminder(role, query):
    """
    Appends a short, position-close reminder of mandatory owner
    instructions directly next to the question. Long system prompts
    suffer from recency bias -- a model tends to weight instructions
    near the question/context block more heavily than ones stated once
    near the top of a long prompt. Verified in testing: identical owner
    instructions in the system prompt were silently ignored until moved
    here. Only used for the prompt sent to generation -- retrieval/
    embedding already ran on the original, unmodified query.
    """
    if not role:
        return query
    owner_text = employee_memory.get_owner_instructions()
    if not owner_text:
        return query
    return query + "\n\n(Reminder -- you must follow this regardless of what else is in the context: " + owner_text.replace("=== OWNER INSTRUCTIONS (obey always) ===\n", "") + ")"


def _budget_blocked_result(query, blocked, role, trace_start):
    record_trace(
        query=query, role=role, error=f"budget reached ({blocked['scope']} {blocked['period']})",
        latency_ms=int((time.monotonic() - trace_start) * 1000),
    )
    return {
        "answer": budget.BUDGET_MESSAGE, "sources": [], "chunks_used": 0,
        "detected_language": None, "provider_used": None,
        "budget_exceeded": {"scope": blocked["scope"], "period": blocked["period"]},
    }


def ask(
    query: str,
    top_k: int = 5,
    temperature: Optional[float] = None,
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
    hybrid: bool = True,
    role: Optional[str] = None,
    tot_mode: bool = False,
    trusted: bool = False,
    allow_actions: bool = False,
) -> dict:
    """
    Answer a question grounded in previously ingested documents.
    Returns: {"answer": str, "sources": List[str], "chunks_used": int, "detected_language": str}

    temperature: overrides the generation service's default temperature for this call.
    system_prompt: overrides the default grounded-QA instructions.
    max_tokens: overrides the default max output length for this call.
    hybrid: use dense+sparse fused retrieval (default) vs. dense-only.
    role: optional AI Employee role (see core/employees/) — layers the
        role's personality and learned business context into the prompt.
    """
    _trace_start = time.monotonic()

    # Token budgets (core/budget.py): global caps are checked before any routing or team
    # splitting spends tokens; a concrete role's own caps are checked as well.
    _pre_role = None if role in ("auto", "team") else role
    budget.set_role(_pre_role)
    _blocked = budget.check_budget(_pre_role)
    if _blocked:
        return _budget_blocked_result(query, _blocked, _pre_role, _trace_start)

    generator = GenerationService()
    # Item #7 (sub-agent spawning): role="team" splits a multi-part request into
    # sub-tasks, each answered via role="auto"; sub-agents only answer (no actions).
    if role == "team":
        return run_team(
            query,
            lambda q, **kw: ask(
                q, top_k=top_k, temperature=temperature, system_prompt=system_prompt,
                max_tokens=max_tokens, hybrid=hybrid, **kw
            ),
            generator, trusted=trusted,
        )
    # Item #6 (supervisor): role="auto" lets the supervisor pick the role.
    # trusted=False (default) restricts it to the customer-facing safe roles.
    routed = None
    if role == "auto":
        routed = route_task(query, trusted=trusted, service=generator)
        role = routed["role"]
        budget.set_role(role)
        _blocked = budget.check_budget(role)
        if _blocked:
            return _budget_blocked_result(query, _blocked, role, _trace_start)
    chunks, detected_language, embedding_failed = _prepare(query, top_k, hybrid)

    if embedding_failed:
        record_trace(
            query=query, role=role, detected_language=detected_language,
            chunks_retrieved=len(chunks), error="embedding failed",
            latency_ms=int((time.monotonic() - _trace_start) * 1000),
        )
        return {
            "answer": "Sorry, I couldn't process your question (embedding failed).",
            "sources": [],
            "chunks_used": 0,
            "detected_language": detected_language,
        }

    effective_system_prompt, role_memory_ids = _build_system_prompt(role, query, system_prompt)
    generation_query = _augment_query_with_reminder(role, query)
    # Item #4 (chain of thought): same single generation call, longer output,
    # only for the 7 sensitive-domain roles (no extra API call, unlike item #3).
    # Runtime-created roles count too (name/tags markers), not just the built-in list.
    role_is_sensitive = is_sensitive_role(role)
    reasoning_mode = role_is_sensitive
    result = generator.generate_answer(
        generation_query, chunks, temperature=temperature, system_prompt=effective_system_prompt,
        max_tokens=max_tokens, reasoning_mode=reasoning_mode, tot_mode=tot_mode,
    )
    # Reasoning is for the audit trace only -- never returned to the caller.
    reasoning = result.pop("reasoning", None)
    result["chunks_used"] = len(chunks)
    result["detected_language"] = detected_language
    result["role_memory_ids"] = role_memory_ids
    if routed:
        result["routed_role"] = routed["role"]
        result["routing_method"] = routed["method"]

    # Item #3 of the 9-pattern agentic-architecture build (self-correction/
    # reflection): a second, cheap LLM pass checking whether the answer is
    # actually supported by the retrieved chunks. Deliberately scoped to
    # ONLY the 7 sensitive-domain roles (legal/healthcare/tax/etc via
    # SENSITIVE_DOMAIN_ROLES), not every request -- this doubles generation
    # cost per call, which isn't justified for routine support/sales
    # questions but is directly relevant to the still-open compliance
    # questions on these specific roles (see ROADMAP.md). Only runs when
    # a real answer was actually generated (provider_used is set).
    reflection_concern = None
    if role_is_sensitive and result.get("provider_used"):
        reflection_concern = generator.check_grounding(result["answer"], chunks, query)
        if reflection_concern:
            result["answer"] += (
                "\n\nNote: part of this answer may not be fully supported by the "
                "available documents -- please verify independently before relying on it."
            )

    # Phase 2 of "broad actionable": opt-in, TRUSTED callers only. The model may propose
    # one action, but it only runs through core.autonomy.execute_or_request() (modes,
    # allowlists, approval, sensitive-role forcing). Never exposed on the HTTP /chat route.
    if allow_actions and result.get("provider_used"):
        if trusted:
            if agent_loop.enabled():
                action_outcome = agent_loop.run(query, result["answer"], generator, role,
                                               tainted=agent_loop.taint_for_retrieval(chunks))
            else:
                action_outcome = maybe_act(query, result["answer"], generator, role,
                                           tainted=agent_loop.taint_for_retrieval(chunks))
        else:
            action_outcome = {"status": "skipped", "tool": None, "target": None,
                              "detail": "actions require a trusted caller"}
        if action_outcome:
            result["action"] = action_outcome
            result["answer"] += "\n\n" + (agent_loop.describe_run(action_outcome) if action_outcome.get("run_id")
                                           else describe_action(action_outcome))

    usage = result.get("usage") or {}
    record_trace(
        query=query, role=role, detected_language=detected_language,
        chunks_retrieved=len(chunks), chunks_sent=result.get("chunks_sent"),
        provider_used=result.get("provider_used"), fallback_used=bool(result.get("fallback_used")),
        prompt_tokens=usage.get("prompt_tokens"), completion_tokens=usage.get("completion_tokens"),
        total_tokens=usage.get("total_tokens"),
        latency_ms=int((time.monotonic() - _trace_start) * 1000),
        error=None if result.get("provider_used") else "all providers failed",
        reflection_concern=reflection_concern,
        reasoning=reasoning,
    )
    return result


def ask_stream(
    query: str,
    top_k: int = 5,
    temperature: Optional[float] = None,
    system_prompt: Optional[str] = None,
    max_tokens: Optional[int] = None,
    hybrid: bool = True,
    role: Optional[str] = None,
) -> Iterator[str]:
    """
    Same as ask(), but yields the answer text incrementally as it's
    generated. Sources/chunks_used/detected_language aren't available
    until generation completes — callers needing those should use
    ask() instead.
    """
    # Token budgets (core/budget.py): check before any embedding or model call is spent.
    budget.set_role(role)
    _blocked = budget.check_budget(role)
    if _blocked:
        yield budget.BUDGET_MESSAGE
        return
    generator = GenerationService()
    chunks, detected_language, embedding_failed = _prepare(query, top_k, hybrid)

    if embedding_failed:
        yield "Sorry, I couldn't process your question (embedding failed)."
        return

    effective_system_prompt, _role_memory_ids = _build_system_prompt(role, query, system_prompt)
    generation_query = _augment_query_with_reminder(role, query)
    yield from generator.generate_answer_stream(
        generation_query, chunks, temperature=temperature, system_prompt=effective_system_prompt, max_tokens=max_tokens
    )


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python -m core.chat \"<your question>\" [--stream]")
        sys.exit(1)

    stream_mode = "--stream" in sys.argv
    args = [a for a in sys.argv[1:] if a != "--stream"]
    question = " ".join(args)

    if stream_mode:
        print(f"\nQuestion: {question}")
        print("Answer: ", end="", flush=True)
        for piece in ask_stream(question):
            print(piece, end="", flush=True)
        print()
    else:
        result = ask(question)
        print(f"\nQuestion: {question}")
        print(f"Detected language: {result.get('detected_language')}")
        print(f"Provider used: {result.get('provider_used')}")
        print(f"Answer: {result['answer']}")
        print(f"Sources: {result['sources']}")
        print(f"Chunks used: {result['chunks_used']}")
        print(f"Chunks sent to LLM: {result.get('chunks_sent')}")
        print(f"Token usage: {result.get('usage')}")
