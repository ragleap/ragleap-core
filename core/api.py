"""
RagLeap Core — FastAPI web layer
Minimal HTTP interface over the existing CLI pipeline (core.ingest / core.chat).
"""
import os
import shutil
import logging
import asyncio
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import hmac
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Response, BackgroundTasks
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel

from core.ingest import ingest_document
from core.parsers import extract_text, SUPPORTED_EXTENSIONS as PARSER_EXTENSIONS
from core import parsers as core_parsers
from core.chat import ask, ask_stream
from core.integrations import service as integrations_service
from core.integrations.factory import get_connector
from core.employees import profile as employee_profile
from core.employees import roles as employee_roles
from core.employees import skills as employee_skills
from core.employees import learning as employee_learning
from core.employees import channel_roles as employee_channel_roles
from core.employees import memory as employee_memory
from core import workflows
from core import tasks as core_tasks
from core import proactive_triggers as core_triggers
from core import scheduler_leader
from core import autonomy
from core import agent_loop
from core import auth_throttle
from core import office
from core import document_store as core_documents
from core import settings as core_settings
from core import vector_dims as core_vector_dims
from core import setup_status
from core import observability
from core import folder_ingest
from core import audit
from core import permissions
from core import users as core_users
from core import queue
from core.employees.sensitivity import is_sensitive_role

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ragleap-core.api")

async def _sync_job():
    try:
        sources = integrations_service.list_data_sources()
        for src in sources:
            if not src.get("is_active"):
                continue
                
            sync_interval_minutes = src.get("sync_interval_minutes") or 360
            last_sync_at_str = src.get("last_sync_at")
            
            should_sync = False
            if not last_sync_at_str:
                should_sync = True
            else:
                last_sync_at = datetime.fromisoformat(last_sync_at_str)
                if last_sync_at.tzinfo is None:
                    last_sync_at = last_sync_at.replace(tzinfo=timezone.utc)
                now = datetime.now(timezone.utc)
                diff_minutes = (now - last_sync_at).total_seconds() / 60
                if diff_minutes >= sync_interval_minutes:
                    should_sync = True
                    
            if should_sync:
                # Per maintainer discussion: we ignore sync failures and retry on the next tick
                # regardless. If they continue failing, last_sync_status will just remain 'failed'.
                logger.info("Background sync triggered for source %s (%s)", src['name'], src['id'])
                # Enqueued onto Redis + processed by worker process(es) if REDIS_URL is
                # set (see core/queue.py); otherwise runs inline exactly as before.
                await asyncio.to_thread(queue.enqueue_sync, src['id'])
    except Exception as e:
        logger.error("Error in background sync job: %s", e)

async def _run_due_triggers_job():
    """Fire any due proactive triggers. run_due_triggers never raises; it is
    blocking (LLM calls), so it runs in a worker thread."""
    try:
        await asyncio.to_thread(core_triggers.run_due_triggers)
    except Exception as e:
        logger.error("Error in proactive triggers job: %s", e)

async def _folder_ingest_job():
    """Stage and ingest the folders granted for knowledge. run_cycle never raises; it blocks (copies, embeddings)."""
    try:
        await asyncio.to_thread(folder_ingest.run_cycle)
    except Exception as e:
        logger.error("Error in folder ingest job: %s", e)

@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        import asyncio
        from core import embedding as _embedding, vector_dims
        await asyncio.to_thread(vector_dims.ensure_all, _embedding.configured_dimensions())
    except Exception as e:
        logger.warning("Vector size check skipped: %s", type(e).__name__)
    scheduler = AsyncIOScheduler()
    # Only the leader process (one per database) runs the jobs; see core/scheduler_leader.py
    scheduler.add_job(scheduler_leader.leader_only(_sync_job), "interval", minutes=5)
    scheduler.add_job(scheduler_leader.leader_only(_run_due_triggers_job), "interval", minutes=1,
                      max_instances=1, coalesce=True)
    if folder_ingest.enabled():
        scheduler.add_job(scheduler_leader.leader_only(_folder_ingest_job), "interval",
                          minutes=folder_ingest.interval_minutes(), max_instances=1, coalesce=True)
    scheduler.start()
    logger.info("Background sync scheduler started.")
    yield
    scheduler.shutdown()
    scheduler_leader.release()
    logger.info("Background sync scheduler stopped.")

app = FastAPI(
    title="RagLeap Core API",
    description="Minimal self-hosted RAG pipeline — BYOK, no platform fallback keys.",
    version="0.12.0",
    lifespan=lifespan,
)

# Opt-in API-key auth. Unset (default) means the API has NO authentication --
# fine for local-only use (127.0.0.1, the compose default), dangerous if this
# port is ever reachable from anywhere else. Applied as global middleware
# (not per-route Depends()) so any NEW endpoint added later is protected by
# default automatically, rather than relying on every future route remembering
# to opt in -- that "forget one route" failure mode is exactly the kind of gap
# that led to this middleware existing in the first place.
RAGLEAP_API_KEY = os.environ.get("RAGLEAP_API_KEY", "").strip()
# Optional: refuse the shared key once an active owner has a personal key (core/users.has_live_owner).
SHARED_KEY_DISABLED = os.environ.get("SHARED_KEY_DISABLED", "").strip().lower() in ("1", "true", "yes", "on")
_shared_key_warned = []
# The AI Office page is three fixed static files with no data in them; every data route keeps the key.
API_KEY_EXEMPT_PATHS = {"/health", "/office", "/office/app.js", "/office/app.css"}
API_KEY_EXEMPT_PREFIXES = ("/webhook/",)  # these verify platform signatures themselves

if not RAGLEAP_API_KEY:
    logger.warning(
        "RAGLEAP_API_KEY is not set -- this API has NO authentication. Anyone who can "
        "reach this port can call every endpoint, including changing autonomy settings "
        "and creating employee roles. Set RAGLEAP_API_KEY in .env for any deployment "
        "reachable beyond localhost."
    )


def _too_many_attempts(wait_seconds: int):
    return JSONResponse(
        status_code=429,
        content={"detail": "Too many failed attempts. Try again later."},
        headers={"Retry-After": str(int(wait_seconds))},
    )


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    if not RAGLEAP_API_KEY:
        return await call_next(request)         # open API: /me falls back to the open actor
    path = request.url.path
    if path in API_KEY_EXEMPT_PATHS or path.startswith(API_KEY_EXEMPT_PREFIXES):
        return await call_next(request)
    peer = getattr(getattr(request, "client", None), "host", "") or ""
    client = auth_throttle.client_id(peer, request.headers.get("x-forwarded-for", ""))
    throttle = auth_throttle.enabled()
    if throttle:
        wait = auth_throttle.blocked_seconds(client)
        if wait:
            return _too_many_attempts(wait)
    supplied = request.headers.get("x-api-key", "")
    if not supplied:
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key."})
    if hmac.compare_digest(supplied.encode("utf-8"), RAGLEAP_API_KEY.encode("utf-8")):
        if SHARED_KEY_DISABLED:
            if await asyncio.to_thread(core_users.has_live_owner):
                return JSONResponse(status_code=401, content={
                    "detail": "The shared API key is switched off. Use a personal key."})
            if not _shared_key_warned:
                _shared_key_warned.append(True)
                logger.warning("SHARED_KEY_DISABLED is set, but no active owner has a personal key, "
                               "so the shared key still works.")
        actor = permissions.BOOTSTRAP_ACTOR            # the shared key counts as the owner
    else:
        actor = await asyncio.to_thread(core_users.resolve, supplied)    # a personal key, or None
    if actor is None:
        if throttle:
            locked = auth_throttle.record_failure(client)
            if locked:
                logger.warning("Too many failed API-key attempts; refusing one client for %s seconds.", locked)
                return _too_many_attempts(locked)
        return JSONResponse(status_code=401, content={"detail": "Missing or invalid API key."})
    if throttle:
        auth_throttle.record_success(client)
    need = permissions.required_role(request.method, path)
    if not permissions.allows(actor["role"], need):
        audit.schedule(actor, request.method, path, 403)
        return JSONResponse(status_code=403, content={
            "detail": f"Your role ({actor['role']}) cannot do this. It needs {need} or higher."})
    request.state.actor = actor
    response = await call_next(request)
    audit.schedule(actor, request.method, path, response.status_code)
    return response


ALLOWED_EXTENSIONS = set(PARSER_EXTENSIONS)
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024  # 20 MB
MAX_QUESTION_LENGTH = 2000


class ChatRequest(BaseModel):
    question: str


class ChatResponse(BaseModel):
    answer: str
    sources: list[str]
    chunks_used: int


class UploadResponse(BaseModel):
    document_id: str
    chunks_stored: int


class ProfileUpdateRequest(BaseModel):
    business_name: str | None = None
    industry: str | None = None
    description: str | None = None
    products_services: str | None = None
    target_customers: str | None = None
    working_hours: str | None = None
    location: str | None = None
    tone_preference: str | None = None
    primary_language: str | None = None
    owner_instructions: str | None = None


class WorkflowCreateRequest(BaseModel):
    name: str
    webhook_url: str
    description: str = ""
    channels: list[str] = []
    is_active: bool = False


class WorkflowUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    webhook_url: str | None = None
    channels: list[str] | None = None
    is_active: bool | None = None


class TaskCreateRequest(BaseModel):
    title: str
    description: str = ""
    status: str = "open"
    priority: str = "normal"
    assigned_role: str | None = None
    creator: str = "owner"
    parent_task_id: str | None = None
    due_date: str | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assigned_role: str | None = None
    due_date: str | None = None
    result: str | None = None


class TriggerCreateRequest(BaseModel):
    name: str
    role: str
    prompt: str
    schedule_minutes: int | None = None
    is_active: bool = True
    cron: str | None = None
    timezone: str = "UTC"


class TriggerUpdateRequest(BaseModel):
    name: str | None = None
    role: str | None = None
    prompt: str | None = None
    schedule_minutes: int | None = None
    is_active: bool | None = None
    cron: str | None = None
    timezone: str | None = None


class AutonomySettingsRequest(BaseModel):
    mode: str
    channels: list[str] = []
    actions: list[str] = []
    approval_channel: str = "telegram"
    approval_target: str = ""


class AutonomyActionRequest(BaseModel):
    action_type: str
    channel: str
    target: str
    content: str
    subject: str = ""


class RoleUpdateRequest(BaseModel):
    display_name: str | None = None
    channels: list[str] | None = None
    skill_tags: list[str] | None = None
    personality: str | None = None
    is_active: bool | None = None


class ChannelRoleRequest(BaseModel):
    role: str


class MemoryFeedbackRequest(BaseModel):
    role_memory_ids: list[str]
    success: bool
    # Optional: when supplied alongside a successful outcome, the
    # exchange is also recorded as a new learned skill via
    # learn_from_conversation (not just reinforcing existing
    # memories via role_memory_ids). Omit these to keep the original
    # reinforce-only behavior - fully backward compatible.
    channel: str | None = None
    user_message: str | None = None
    ai_reply: str | None = None


class DataSourceCreateRequest(BaseModel):
    name: str
    source_type: str
    connection_string: str | None = None
    api_endpoint: str | None = None
    api_key: str | None = None
    api_headers: dict = {}
    query_template: str | None = None
    field_mappings: dict = {}
    user_identifier_field: str = "user_id"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type '{ext}'. Currently supported: {', '.join(sorted(ALLOWED_EXTENSIONS))}.",
        )
    try:
        raw_bytes = await file.read()

        if len(raw_bytes) == 0:
            raise HTTPException(status_code=400, detail="Uploaded file is empty.")
        if len(raw_bytes) > MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"File too large ({len(raw_bytes) / 1024 / 1024:.1f} MB). Max size is {MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB.",
            )

        text = extract_text(file.filename, raw_bytes)
        result = ingest_document(file.filename, text)
        logger.info("Ingested %s -> %s", file.filename, result)
        return {
            "document_id": result["document_id"],
            "chunks_stored": result["chunks_stored"],
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Ingestion failed for %s", file.filename)
        logger.error(f"Ingestion failed: {exc}")
        raise HTTPException(status_code=500, detail="Ingestion failed. See server logs for details.") from exc
    finally:
        await file.close()


MAX_URL_LENGTH = 2048


class IngestUrlRequest(BaseModel):
    url: str


@app.post("/ingest/url")
async def ingest_web_page(req: IngestUrlRequest):
    """
    Fetch a public web page and add its readable text as a document. Private and internal
    addresses are refused unless the operator sets INGEST_ALLOW_PRIVATE_URLS=true in .env.
    """
    url = (req.url or "").strip()
    if not url or len(url) > MAX_URL_LENGTH or not url.lower().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Give a full web address that starts with http:// or https://")
    if not core_parsers.url_ingest_available():
        raise HTTPException(status_code=501, detail="Adding web pages needs the ragleap-rag package with its web support installed.")
    try:
        text = await asyncio.to_thread(core_parsers.fetch_url_text, url)
        result = await asyncio.to_thread(ingest_document, url[:500], text)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)[:200]) from exc
    except Exception as exc:
        logger.exception("URL ingestion failed")
        raise HTTPException(status_code=500, detail="Could not add that web page. See server logs for details.") from exc
    return {"document_id": result["document_id"], "chunks_stored": result["chunks_stored"]}


def _validate_question(question: str):
    if not question or not question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Question too long ({len(question)} chars). Max is {MAX_QUESTION_LENGTH} chars.",
        )


@app.post("/chat")
def chat(
    question: str,
    top_k: int = 5,
    temperature: float | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    hybrid: bool = True,
    role: str | None = None,
):
    """
    Ask a question grounded in previously ingested documents.

    top_k: number of chunks to retrieve and ground the answer in.
    temperature: LLM sampling temperature (lower = more deterministic/faithful
        to context; default comes from DEFAULT_TEMPERATURE in .env, 0.3).
    system_prompt: override the default grounded-QA instructions.
    max_tokens: override the default max output length.
    hybrid: use dense+sparse fused retrieval (default True) vs. dense-only.
    """
    _validate_question(question)
    try:
        result = ask(
            question, top_k=top_k, temperature=temperature,
            system_prompt=system_prompt, max_tokens=max_tokens, hybrid=hybrid,
            role=role,
        )
        return {
            "answer": result["answer"],
            "sources": result.get("sources", []),
            "chunks_used": result.get("chunks_used", 0),
            "chunks_sent": result.get("chunks_sent"),
            "detected_language": result.get("detected_language"),
            "provider_used": result.get("provider_used"),
            "usage": result.get("usage"),
            "role_memory_ids": result.get("role_memory_ids", []),
        }
    except Exception as exc:
        logger.exception("Chat failed for question: %s", question)
        logger.error(f"Chat failed: {exc}")
        raise HTTPException(status_code=500, detail="Chat failed. See server logs for details.") from exc


@app.post("/chat/stream")
def chat_stream(
    question: str,
    top_k: int = 5,
    temperature: float | None = None,
    system_prompt: str | None = None,
    max_tokens: int | None = None,
    hybrid: bool = True,
    role: str | None = None,
):
    """
    Same as /chat, but streams the answer text back as it's generated
    (text/plain, chunked transfer) instead of waiting for the full
    response. Sources/chunks_used/detected_language aren't available in
    a streaming response — use /chat if you need those.
    """
    _validate_question(question)

    def _generate():
        try:
            for piece in ask_stream(
                question, top_k=top_k, temperature=temperature,
                system_prompt=system_prompt, max_tokens=max_tokens, hybrid=hybrid,
                role=role,
            ):
                yield piece
        except Exception as exc:
            logger.exception("Streaming chat failed for question: %s", question)
            yield "\n[Error: the request could not be completed]"

    return StreamingResponse(_generate(), media_type="text/plain")


@app.post("/chat/feedback")
def chat_feedback(req: MemoryFeedbackRequest):
    """
    Report the real outcome of a role-based /chat response, so the
    memory entries that fed it get reinforced or decayed accordingly.

    Call this once you actually know whether the response worked --
    e.g. the conversation resolved well, or an owner approved/
    rejected an autonomous action that followed from it.
    role_memory_ids comes from the /chat response for role-based
    requests (empty list if no role was set, in which case this is
    a no-op).

    This is not model training -- no weights change. It is an
    adaptive importance signal on the existing retrieval system.
    """
    updated = employee_learning.record_role_memory_outcome(
        req.role_memory_ids, success=req.success
    )
    if req.channel and req.user_message and req.ai_reply:
        employee_learning.learn_from_conversation(
            req.channel, req.user_message, req.ai_reply,
            resolved=req.success, score=0.85 if req.success else 0.3,
        )
    return {"updated": updated}


@app.get("/webhook/whatsapp")
async def whatsapp_webhook_verification(request: Request):
    """
    Meta WhatsApp Cloud API webhook verification. During setup Meta sends a GET
    with hub.mode=subscribe, hub.verify_token and hub.challenge; when the verify
    token matches META_VERIFY_TOKEN, echo hub.challenge back.
    """
    from channels.whatsapp.router import _verify_meta_webhook_token

    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    if not _verify_meta_webhook_token(mode, token):
        logger.warning("WhatsApp webhook: Meta webhook verification failed (hub.mode/hub.verify_token mismatch)")
        raise HTTPException(status_code=403, detail="Verification failed.")

    return Response(content=challenge, media_type="text/plain")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    """
    WhatsApp webhook. With the default WHATSAPP_PROVIDER=twilio this is a
    Twilio-compatible endpoint: Twilio sends form-encoded POST data (not JSON),
    with fields like 'Body' (message text) and 'From' (sender's phone, prefixed
    'whatsapp:'). With WHATSAPP_PROVIDER=meta it instead takes Meta's WhatsApp
    Cloud API JSON payload, whose X-Hub-Signature-256 header (HMAC-SHA256 of the
    raw body with the Meta app secret) is verified before the payload is trusted.
    """
    from channels.whatsapp.router import (handle_incoming_message, _verify_twilio_signature,
                                          _verify_meta_signature, WHATSAPP_PROVIDER, unsigned_allowed)

    if WHATSAPP_PROVIDER == "meta":
        raw_body = await request.body()
        signature = request.headers.get("x-hub-signature-256", "")

        if signature:
            if not _verify_meta_signature(signature, raw_body):
                logger.warning("WhatsApp webhook: invalid Meta signature")
                raise HTTPException(status_code=403, detail="Invalid signature.")
        elif not unsigned_allowed():
            logger.warning("WhatsApp webhook: request without a Meta signature rejected")
            raise HTTPException(status_code=403, detail="Invalid signature.")
        else:
            logger.warning("WhatsApp webhook: unsigned request accepted because WHATSAPP_ALLOW_UNSIGNED=true (local testing only)")

        payload = await request.json()
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for message in change.get("value", {}).get("messages", []):
                    text = (message.get("text") or {}).get("body", "")
                    meta_from = message.get("from", "")
                    if not text or not meta_from:
                        # Non-text message types (image, button reply, ...) — nothing to answer.
                        continue
                    logger.info(f"WhatsApp webhook: message from {meta_from}: {text[:50]}")
                    handle_incoming_message(meta_from, text)

        return {"ok": True}

    form = await request.form()
    params = dict(form)

    incoming_msg = params.get("Body", "")
    from_phone = params.get("From", "")
    if from_phone.startswith("whatsapp:"):
        from_phone = from_phone.replace("whatsapp:", "")

    if not incoming_msg or not from_phone:
        raise HTTPException(status_code=400, detail="Missing Body or From field.")

    signature = request.headers.get("x-twilio-signature", "")
    if signature:
        url = str(request.url)
        if not _verify_twilio_signature(url, params, signature):
            logger.warning("WhatsApp webhook: invalid Twilio signature")
            raise HTTPException(status_code=403, detail="Invalid signature.")
    elif not unsigned_allowed():
        logger.warning("WhatsApp webhook: request without a Twilio signature rejected")
        raise HTTPException(status_code=403, detail="Invalid signature.")
    else:
        logger.warning("WhatsApp webhook: unsigned request accepted because WHATSAPP_ALLOW_UNSIGNED=true (local testing only)")

    logger.info(f"WhatsApp webhook: message from {from_phone}: {incoming_msg[:50]}")
    handle_incoming_message(from_phone, incoming_msg)

    # Twilio expects a TwiML (XML) response, even if empty — we already
    # sent the reply via the Twilio API directly in handle_incoming_message.
    return Response(content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>', media_type="application/xml")


@app.post("/webhook/telegram")
async def telegram_webhook(request: Request):
    """
    Telegram webhook. Telegram sends JSON payloads with an 'update' object
    containing 'message' -> 'chat' -> 'id' and 'message' -> 'text'.
    """
    from channels.telegram.router import handle_incoming_message, _verify_webhook_secret

    secret = request.headers.get("x-telegram-bot-api-secret-token", "")
    if not _verify_webhook_secret(secret):
        logger.warning("Telegram webhook: invalid secret token")
        raise HTTPException(status_code=403, detail="Invalid secret token.")

    update = await request.json()
    message = update.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    text = message.get("text", "")

    if not chat_id:
        # Not a text message we handle (could be a different update type) — acknowledge anyway.
        return {"ok": True}

    logger.info(f"Telegram webhook: message from chat {chat_id}: {text[:50]}")
    handle_incoming_message(chat_id, text)

    return {"ok": True}


@app.post("/webhook/discord")
async def discord_webhook(request: Request):
    """
    Discord webhook. Handles both the PING verification challenge Discord
    sends when registering the endpoint, and real MESSAGE_CREATE-style
    interaction payloads.
    """
    from channels.discord.router import handle_incoming_message, verify_discord_signature

    signature = request.headers.get("x-signature-ed25519", "")
    timestamp = request.headers.get("x-signature-timestamp", "")
    body = await request.body()

    if not verify_discord_signature(signature, timestamp, body):
        logger.warning("Discord webhook: invalid signature")
        raise HTTPException(status_code=401, detail="Invalid request signature.")

    data = await request.json()

    # Discord's PING verification challenge — must respond with type: 1
    if data.get("type") == 1:
        return {"type": 1}

    channel_id = data.get("channel_id")
    content = data.get("content", "") or (data.get("data", {}) or {}).get("content", "")

    if not channel_id or not content:
        return {"ok": True}

    logger.info(f"Discord webhook: message from channel {channel_id}: {content[:50]}")
    handle_incoming_message(channel_id, content)

    return {"ok": True}


# ============================================================================
# INTEGRATIONS — external data sources (databases, CRMs, SaaS APIs)
# ============================================================================

@app.post("/integrations")
def create_integration(req: DataSourceCreateRequest):
    try:
        result = integrations_service.create_data_source(
            name=req.name,
            source_type=req.source_type,
            connection_string=req.connection_string,
            api_endpoint=req.api_endpoint,
            api_key=req.api_key,
            api_headers=req.api_headers,
            query_template=req.query_template,
            field_mappings=req.field_mappings,
            user_identifier_field=req.user_identifier_field,
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.exception("Failed to create integration")
        logger.error(f"Failed to create integration: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create integration. See server logs for details.") from exc


@app.get("/integrations")
def list_integrations():
    return {"data_sources": integrations_service.list_data_sources()}


@app.post("/integrations/{data_source_id}/test")
def test_integration(data_source_id: str):
    result = integrations_service.test_data_source(data_source_id)
    if not result.get("success") and result.get("error") == "Data source not found":
        raise HTTPException(status_code=404, detail="Data source not found")
    return result


@app.post("/integrations/{data_source_id}/sync")
def sync_integration(data_source_id: str):
    result = integrations_service.sync_data_source(data_source_id)
    if not result.get("success") and result.get("error") == "Data source not found":
        raise HTTPException(status_code=404, detail="Data source not found")
    data_source = integrations_service.get_data_source(data_source_id)
    if data_source:
        result_summary = (
            f"success: {result.get('records_synced', 0)} records synced"
            if result.get("success")
            else f"failed: {result.get('error', 'unknown error')}"
        )
        employee_learning.learn_from_integration_action(data_source.name, "sync", result_summary)
    if result.get("success"):
        return {"success": True, "records_synced": int(result.get("records_synced", 0))}
    logger.error(f"Integration sync failed for {data_source_id}: {result.get('error')}")
    return {"success": False, "error": "Sync failed. See server logs for details."}


@app.get("/profile")
def get_business_profile():
    return employee_profile.get_profile()


@app.patch("/profile")
def update_business_profile(req: ProfileUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    updated = employee_profile.update_profile(**updates)
    if "owner_instructions" in updates:
        employee_learning.learn_from_owner_instruction(updates["owner_instructions"])
    return updated


@app.post("/profile/learn")
def trigger_auto_learn():
    employee_learning.auto_learn_from_all()
    return employee_profile.get_profile()


@app.get("/employees")
def list_employee_roles(active_only: bool = False):
    employee_roles.seed_default_roles()
    employee_memory.seed_default_memory_seeds()
    return {"roles": employee_roles.list_roles(active_only=active_only)}


@app.get("/employees/{role}")
def get_employee_role(role: str):
    r = employee_roles.get_role(role)
    if r is None:
        raise HTTPException(status_code=404, detail=f"Role '{role}' not found.")
    return r


@app.patch("/employees/{role}")
def update_employee_role(role: str, req: RoleUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    result = employee_roles.upsert_role(role, **updates)
    # Tell the caller whether this role is treated as a sensitive domain (forced
    # semi/off autonomy, reasoning and grounding checks).
    result["sensitive_domain"] = is_sensitive_role(role)
    return result


@app.get("/channels/{channel}/role")
def get_channel_role_config(channel: str):
    """
    Get the owner-configured default AI Employee role for this channel
    (falls back to 'support' if never explicitly set).
    """
    return {"channel": channel, "role": employee_channel_roles.get_channel_role(channel)}


@app.post("/channels/{channel}/role")
def set_channel_role_config(channel: str, req: ChannelRoleRequest):
    """
    Assign a default AI Employee role to this channel (e.g. telegram ->
    manager, whatsapp -> support). Any of the 9 default roles or a
    custom role created via PATCH /employees/{role} can be assigned --
    this is a deliberate owner choice, unlike the automatic
    intent-based routing which is restricted to customer-safe roles
    only (see core/employees/channel_roles.py).
    """
    ok = employee_channel_roles.set_channel_role(channel, req.role)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update channel role")
    return {"channel": channel, "role": req.role}


@app.get("/employees/{role}/context")
def get_employee_context(role: str, query: str | None = None, top_k: int = 8):
    return {
        "role": role,
        "personality": employee_skills.get_role_personality(role),
        "context": employee_skills.get_role_skills(role=role, query=query, top_k=top_k),
        "capability_summary": employee_skills.get_capability_summary(),
    }


@app.get("/n8n-workflows")
def list_n8n_workflows(channel: str | None = None, active_only: bool = False):
    return {"workflows": workflows.list_workflows(channel=channel, active_only=active_only)}


@app.post("/n8n-workflows")
def create_n8n_workflow(req: WorkflowCreateRequest):
    return workflows.create_workflow(
        name=req.name, webhook_url=req.webhook_url, description=req.description,
        channels=req.channels, is_active=req.is_active,
    )


@app.get("/n8n-workflows/{workflow_id}")
def get_n8n_workflow(workflow_id: str):
    wf = workflows.get_workflow(workflow_id)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return wf


@app.patch("/n8n-workflows/{workflow_id}")
def update_n8n_workflow(workflow_id: str, req: WorkflowUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    wf = workflows.update_workflow(workflow_id, **updates)
    if wf is None:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return wf


@app.delete("/n8n-workflows/{workflow_id}")
def delete_n8n_workflow(workflow_id: str):
    deleted = workflows.delete_workflow(workflow_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Workflow not found.")
    return {"deleted": True}


@app.get("/tasks")
def list_tasks(status: str | None = None, assigned_role: str | None = None, parent_task_id: str | None = None):
    return {"tasks": core_tasks.list_tasks(status=status, assigned_role=assigned_role, parent_task_id=parent_task_id)}


@app.post("/tasks")
def create_task(req: TaskCreateRequest):
    try:
        return core_tasks.create_task(
            title=req.title, description=req.description, status=req.status, priority=req.priority,
            assigned_role=req.assigned_role, creator=req.creator,
            parent_task_id=req.parent_task_id, due_date=req.due_date,
        )
    except core_tasks.TaskValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/tasks/{task_id}")
def get_task(task_id: str):
    t = core_tasks.get_task(task_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return t


@app.patch("/tasks/{task_id}")
def update_task(task_id: str, req: TaskUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    try:
        t = core_tasks.update_task(task_id, **updates)
    except core_tasks.TaskValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if t is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return t


@app.get("/proactive-triggers")
def list_proactive_triggers(active_only: bool = False):
    return {"triggers": core_triggers.list_triggers(active_only=active_only)}


@app.post("/proactive-triggers")
def create_proactive_trigger(req: TriggerCreateRequest):
    try:
        return core_triggers.create_trigger(
            name=req.name, role=req.role, prompt=req.prompt,
            schedule_minutes=req.schedule_minutes, is_active=req.is_active,
            cron=req.cron, timezone=req.timezone,
        )
    except core_triggers.TriggerValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/proactive-triggers/{trigger_id}")
def get_proactive_trigger(trigger_id: str):
    t = core_triggers.get_trigger(trigger_id)
    if t is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    return t


@app.patch("/proactive-triggers/{trigger_id}")
def update_proactive_trigger(trigger_id: str, req: TriggerUpdateRequest):
    updates = {k: v for k, v in req.dict().items() if v is not None}
    try:
        t = core_triggers.update_trigger(trigger_id, **updates)
    except core_triggers.TriggerValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if t is None:
        raise HTTPException(status_code=404, detail="Trigger not found.")
    return t


@app.delete("/proactive-triggers/{trigger_id}")
def delete_proactive_trigger(trigger_id: str):
    if not core_triggers.delete_trigger(trigger_id):
        raise HTTPException(status_code=404, detail="Trigger not found.")
    return {"deleted": True}


MAX_CSV_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB — stored as a Postgres TEXT column, keep it sane


@app.post("/integrations/csv")
async def create_csv_integration(
    name: str = Form(...),
    user_identifier_field: str = Form("user_id"),
    file: UploadFile = File(...),
):
    """
    Create a CSV data source from an uploaded file. Separate from the
    JSON-only POST /integrations route since this takes actual file
    content, not connection parameters.
    """
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="Only .csv files are supported.")
    try:
        raw_bytes = await file.read()
        if len(raw_bytes) == 0:
            raise HTTPException(status_code=400, detail="Uploaded CSV is empty.")
        if len(raw_bytes) > MAX_CSV_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"CSV too large ({len(raw_bytes) / 1024 / 1024:.1f} MB). Max size is {MAX_CSV_SIZE_BYTES / 1024 / 1024:.0f} MB.",
            )
        csv_text = raw_bytes.decode("utf-8-sig")
        result = integrations_service.create_csv_data_source(
            name=name, csv_content=csv_text, csv_filename=file.filename,
            user_identifier_field=user_identifier_field,
        )
        return result
    except HTTPException:
        raise
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV must be UTF-8 encoded.")
    except Exception as exc:
        logger.exception("CSV data source creation failed for %s", file.filename)
        logger.error(f"Failed to create CSV data source: {exc}")
        raise HTTPException(status_code=500, detail="Failed to create CSV data source. See server logs for details.") from exc
    finally:
        await file.close()


@app.get("/autonomy")
def get_autonomy():
    """Get current Autonomous Loop settings (mode, allowlists, approval config)."""
    return autonomy.get_autonomy_settings()


@app.post("/autonomy")
def set_autonomy(req: AutonomySettingsRequest):
    """
    Configure the Autonomous Loop. mode must be one of "off"/"semi"/"full".
    channels/actions are allowlists - empty means no restriction beyond
    mode itself. approval_channel/approval_target are required for "semi"
    mode (where approval requests are sent).
    """
    if req.mode not in ("off", "semi", "full"):
        raise HTTPException(status_code=400, detail='mode must be "off", "semi", or "full"')
    ok = autonomy.set_autonomy(
        mode=req.mode, channels=req.channels, actions=req.actions,
        approval_channel=req.approval_channel, approval_target=req.approval_target,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to update autonomy settings")
    return autonomy.get_autonomy_settings()


@app.post("/autonomy/execute")
def execute_autonomous_action(req: AutonomyActionRequest):
    """
    Trigger the Autonomous Loop dispatcher for a given action. Respects
    current mode and allowlists - may execute immediately (full mode),
    queue for approval (semi mode), or skip (off mode / not allowlisted).
    """
    return autonomy.execute_or_request(
        action_type=req.action_type, channel=req.channel, target=req.target,
        content=req.content, subject=req.subject,
    )


@app.get("/autonomy/pending")
def get_pending_actions(limit: int = 50):
    """
    Actions waiting for owner approval in semi mode, newest first, each with its
    full content (the exact code, command or URL). Review before approving.
    """
    return {"pending": autonomy.list_pending(limit=limit)}


@app.post("/autonomy/pending/{action_id}/approve")
def approve_pending_action(action_id: str, request: Request = None):
    """Approve and run one pending action (same path as replying YES <id>). The log records who approved it."""
    reply = autonomy.resolve_pending(action_id, approve=True, decided_by=_actor_of(request))
    if reply is None:
        raise HTTPException(status_code=404, detail="No such pending action.")
    return {"result": reply}


@app.post("/autonomy/pending/{action_id}/reject")
def reject_pending_action(action_id: str, request: Request = None):
    """Reject and discard one pending action (same path as replying NO <id>). The log records who rejected it."""
    reply = autonomy.resolve_pending(action_id, approve=False, decided_by=_actor_of(request))
    if reply is None:
        raise HTTPException(status_code=404, detail="No such pending action.")
    return {"result": reply}


def _office_file(path: str):
    got = office.static_response(path)
    if got is None:
        raise HTTPException(status_code=404, detail="Not found.")
    body, content_type, headers = got
    return Response(content=body, media_type=content_type, headers=headers)


@app.get("/office")
def office_page():
    return _office_file("/office")


@app.get("/office/app.js")
def office_js():
    return _office_file("/office/app.js")


@app.get("/office/app.css")
def office_css():
    return _office_file("/office/app.css")


@app.get("/overview")
def office_overview():
    """Counts for the AI Office dashboard (approvals, runs, tasks, triggers, roles, usage, autonomy)."""
    return office.overview()


@app.get("/autonomy/log")
def office_autonomy_log(limit: int = 50, approved: bool | None = None):
    """The autonomy action log as data, newest first."""
    return {"log": office.autonomy_log(limit=limit, approved=approved)}


@app.get("/file-grants")
def office_file_grants():
    """Folder grants and the file-access switches for the dashboard Files tab (names only, never host paths)."""
    from core import file_access
    return file_access.overview()


def _actor_of(request):
    """The signed-in person behind a request (None when called without one)."""
    return getattr(getattr(request, "state", None), "actor", None)


@app.get("/audit")
def audit_events(limit: int = 100, actor: str | None = None):
    """Who did what: state-changing requests by signed-in people, newest first. Admin and owner only."""
    try:
        return {"events": audit.list_events(limit=limit, actor=actor)}
    except Exception as e:
        if e.__class__.__name__ in ("UndefinedTable", "UndefinedColumn"):
            raise HTTPException(status_code=503, detail="The database has no audit table yet (apply db/schema.sql).")
        raise


@app.get("/me")
def whoami(request: Request):
    """Who the API thinks you are: the shared key counts as the owner; a personal key shows its person and role."""
    actor = getattr(request.state, "actor", None) or permissions.OPEN_ACTOR
    return {k: actor.get(k) for k in ("kind", "name", "role", "user_id")}


class UserCreateRequest(BaseModel):
    name: str
    role: str


class UserUpdateRequest(BaseModel):
    role: str | None = None
    status: str | None = None


class KeyCreateRequest(BaseModel):
    label: str = ""


def _users_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except core_users.UserError as e:
        raise HTTPException(status_code=e.status, detail=e.message)
    except Exception as e:
        if e.__class__.__name__ in ("UndefinedTable", "UndefinedColumn"):
            raise HTTPException(status_code=503, detail="The database has no users tables yet (apply db/schema.sql).")
        raise


@app.get("/users")
def users_list():
    """People and how many live personal keys each has. Owner only. Key values are never shown again."""
    return {"users": _users_call(core_users.list_users), "roles": list(permissions.ROLES)}


@app.post("/users")
def users_create(req: UserCreateRequest):
    return _users_call(core_users.create_user, req.name, req.role)


@app.patch("/users/{user_id}")
def users_update(user_id: str, req: UserUpdateRequest):
    return _users_call(core_users.update_user, user_id, req.role, req.status)


@app.post("/users/{user_id}/keys")
def users_key_create(user_id: str, req: KeyCreateRequest):
    """Make a personal key. It is in this answer once and is never stored or shown again."""
    return _users_call(core_users.create_key, user_id, req.label)


@app.delete("/users/{user_id}/keys/{key_id}")
def users_key_revoke(user_id: str, key_id: str):
    return _users_call(core_users.revoke_key, user_id, key_id)


@app.get("/folder-ingest")
def folder_ingest_status():
    """Folder auto-ingest: switches, the last cycle per granted folder, and which folders wait for approval."""
    return folder_ingest.status()


class FolderApproveRequest(BaseModel):
    grant: str


@app.post("/folder-ingest/approve")
async def folder_ingest_approve(req: FolderApproveRequest, background_tasks: BackgroundTasks):
    """The owner accepts the first sync of a staged folder (embeddings can cost money). Ingestion starts at once."""
    code, body = await asyncio.to_thread(folder_ingest.approve_grant, req.grant)
    if code != 200:
        raise HTTPException(status_code=code, detail=body["error"])
    background_tasks.add_task(folder_ingest.run_cycle, False)
    return body


@app.get("/org")
def office_org():
    """Org chart for the AI Office: departments with each role's status, usage, caps and open tasks."""
    return office.org_chart()


class SettingsUpdateRequest(BaseModel):
    values: dict[str, str | int | None]


@app.get("/settings")
def get_dashboard_settings():
    """Every dashboard-editable setting with its source (dashboard/env/default). Secrets are never returned."""
    return {
        "settings": core_settings.describe(),
        "providers": {"llm": core_settings.llm_providers(), "embedding": core_settings.embedding_providers()},
        "encryption_ready": core_settings.encryption_ready(),
    }


@app.put("/settings")
def put_dashboard_settings(req: SettingsUpdateRequest):
    """Save settings. A blank or null value removes the dashboard value (falls back to .env, then default)."""
    values = req.values
    if not values or len(values) > 100:
        raise HTTPException(status_code=400, detail="Send between 1 and 100 settings.")
    bad = core_settings.invalid_names(values)
    if bad:
        raise HTTPException(status_code=400, detail="Some settings are not valid: " + ", ".join(bad))
    if core_settings.needs_encryption(values) and not core_settings.encryption_ready():
        raise HTTPException(status_code=400, detail="ADDON_ENCRYPTION_KEY is not set, so secrets cannot be stored.")
    try:
        updated = core_settings.set_many(values)
    except ValueError:
        raise HTTPException(status_code=400, detail="Could not save these settings.")
    except Exception as e:
        logger.error("Saving settings failed: %s", type(e).__name__)
        raise HTTPException(status_code=500, detail="Could not save settings.")
    vector_status = None
    if any(n in ("EMBEDDING_PROVIDER", "EMBEDDING_DIMENSIONS") for n in updated):
        from core import embedding as _emb
        vector_status = core_vector_dims.ensure_all(_emb.configured_dimensions())
    return {"updated": updated, "vector_status": vector_status}


_TEST_MESSAGES = {
    "not_configured": "The provider is not fully configured (missing key, model or URL).",
    "request_failed": "The provider did not answer. Check the key, model name and URL.",
    "size_mismatch": "The model returned a different vector size than EMBEDDING_DIMENSIONS.",
}


def _test_result(code: str):
    return {"ok": False, "error": code, "hint": _TEST_MESSAGES[code]}


@app.post("/settings/test/llm")
def test_llm_connection():
    """One tiny real call to the configured chat provider. Never returns provider response text."""
    from core import generation
    try:
        svc = generation.GenerationService()
    except ValueError:
        return _test_result("not_configured")
    except Exception:
        return _test_result("request_failed")
    try:
        svc._call_provider(svc.primary_config, "Reply with the single word OK.", 0.0, 16)
    except Exception as e:
        logger.warning("LLM connection test failed: %s", type(e).__name__)
        return _test_result("request_failed")
    return {"ok": True, "provider": svc.primary_config.get("provider"), "model": svc.primary_config.get("model")}


@app.post("/settings/test/embedding")
def test_embedding_connection():
    """One tiny real embedding call. Reports the vector size, never provider response text."""
    from core import embedding
    try:
        svc = embedding.EmbeddingService()
    except ValueError:
        return _test_result("not_configured")
    except Exception:
        return _test_result("request_failed")
    try:
        vec = svc.embed_text("connection test")
    except Exception as e:
        logger.warning("Embedding connection test failed: %s", type(e).__name__)
        return _test_result("request_failed")
    if not vec:
        return _test_result("request_failed")
    if len(vec) != embedding.configured_dimensions():
        return _test_result("size_mismatch")
    return {"ok": True, "provider": svc.provider, "model": svc.model, "dimensions": len(vec)}


@app.get("/setup/status")
def get_setup_status():
    """Read-only setup checklist for the AI Office Setup tab. Never returns secrets or error text."""
    return setup_status.build(api_key_set=bool(RAGLEAP_API_KEY))


@app.get("/usage/summary")
def office_usage_summary():
    """Token usage per role and globally, next to any configured budget caps."""
    return office.usage_summary()


@app.get("/documents")
def list_uploaded_documents(limit: int = 100):
    """Uploaded documents, newest first, with the number of stored chunks."""
    try:
        return {"documents": core_documents.list_documents(limit=limit),
                "formats": sorted(ALLOWED_EXTENSIONS),
                "url_ingest": core_parsers.url_ingest_available(),
                "max_mb": MAX_FILE_SIZE_BYTES // (1024 * 1024)}
    except Exception as exc:
        logger.exception("Failed to list documents")
        raise HTTPException(status_code=500, detail="Failed to list documents. See server logs for details.") from exc


@app.delete("/documents/{document_id}")
def delete_uploaded_document(document_id: str):
    """Delete a document and its chunks (the chunks go with it through ON DELETE CASCADE)."""
    try:
        deleted = core_documents.delete_document(document_id)
    except Exception as exc:
        logger.exception("Failed to delete document")
        raise HTTPException(status_code=500, detail="Failed to delete the document. See server logs for details.") from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="Document not found.")
    return {"deleted": document_id, "graph": core_documents.delete_document_graph(document_id)}


@app.get("/agent-runs")
def list_agent_runs(limit: int = 50, status: str | None = None):
    """Agent-loop runs, newest first (status: running, waiting_approval, done, failed, rejected, budget)."""
    return {"runs": agent_loop.list_runs(limit=limit, status=status)}


@app.get("/agent-runs/{run_id}")
def get_agent_run(run_id: str):
    r = agent_loop.get_run(run_id)
    if r is None:
        raise HTTPException(status_code=404, detail="Run not found.")
    return r


@app.get("/autonomy/report")
def get_autonomy_report():
    """Get today's Autonomous Loop activity report."""
    return {"report": autonomy.generate_autonomy_daily_report()}


@app.get("/observability/report")
def get_observability_report():
    """
    Get today's agent trace report (item #2 of the 9-pattern
    agentic-architecture build). Real aggregation of core.chat.ask()
    calls traced via core.observability.record_trace() - total requests,
    error rate, average latency, breakdown by role and provider. Does
    NOT cover core.chat.ask_stream() - see core/observability.py's
    module docstring for why.
    """
    return {"report": observability.generate_observability_report()}


@app.get("/autonomy/rejection-report")
def get_autonomy_rejection_report():
    """
    Real, honest rejection-pattern report (all-time, grouped by action
    type/channel/role) - see generate_rejection_pattern_report()'s
    docstring. Does not suggest or apply any config change - that part
    (suggest_autonomy_changes()) is intentionally not implemented yet;
    see its docstring for why.
    """
    return {"report": autonomy.generate_rejection_pattern_report()}
