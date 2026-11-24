"""Telegram long polling: receive messages without a public address.

The app asks Telegram for new messages (getUpdates) instead of waiting for a webhook. It is opt-in (TELEGRAM_POLLING=true)
and needs the bot token. Telegram refuses getUpdates while a webhook is registered, so the poller checks first and, if a
webhook exists, does not poll (it never deletes your webhook). Only the scheduler leader polls (core/scheduler_leader.py),
so several app processes cannot fight over the same bot.

Handling is at-least-once: an update is acknowledged to Telegram only after its handler finished, and an update that is
delivered again is ignored while its id is still remembered. The bot token is never written to a log or to the status.
"""
import collections
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, Optional

import requests

logger = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"
_STATE: Dict = {"mode": "off", "handled": 0, "last_error": ""}
_DEFAULT: Optional["TelegramPoller"] = None


def _int_env(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.environ.get(name, "").strip() or default)))
    except ValueError:
        return default


def enabled() -> bool:
    """Polling is on only when TELEGRAM_POLLING=true and a bot token is set."""
    return os.environ.get("TELEGRAM_POLLING", "").strip().lower() == "true" and bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())


def status() -> Dict:
    """What the poller is doing: mode off | waiting_for_leader | polling | webhook_registered | conflict | bad_token | error."""
    return dict(_STATE)


class TelegramPoller:
    """One long-polling loop. step() does one round and returns how many seconds to wait before the next one, so the loop can be
    driven without sleeping in tests."""

    def __init__(self, token: str, handle: Callable[[dict], object], is_leader: Callable[[], bool],
                 http_post=requests.post, poll_timeout: int = 30, workers: int = 4, inline: bool = False):
        self._token, self._handle, self._is_leader, self._post = token, handle, is_leader, http_post
        self._timeout, self._inline = poll_timeout, inline
        self._executor = None if inline else ThreadPoolExecutor(max_workers=workers, thread_name_prefix="tg-poll")
        self._lock = threading.Lock()
        self._inflight, self._max_seen = set(), None
        self._seen, self._seen_order = set(), collections.deque(maxlen=1000)
        self._chat_locks: Dict = {}
        self._webhook_checked, self._failures = False, 0
        self._stop, self._thread = threading.Event(), None
        self._set("waiting_for_leader")

    # ---- status (never holds the token)
    def _set(self, mode: str, error: str = "") -> None:
        _STATE["mode"], _STATE["last_error"] = mode, error

    def _api(self, method: str, payload: dict, timeout: float):
        resp = self._post(API.format(token=self._token, method=method), json=payload, timeout=timeout)
        try:
            body = resp.json()
        except Exception:
            body = {}
        return resp.status_code, body

    # ---- one round
    def _offset(self):
        with self._lock:
            if self._inflight:
                return min(self._inflight)
            return None if self._max_seen is None else self._max_seen + 1

    def step(self) -> float:
        """One round: returns the seconds to wait before the next one."""
        if not self._is_leader():
            self._webhook_checked = False
            self._set("waiting_for_leader")
            return 5.0
        try:
            if not self._webhook_checked:
                code, body = self._api("getWebhookInfo", {}, 15)
                if code in (401, 404):
                    self._set("bad_token", "Telegram refused the bot token")
                    return 300.0
                if code == 200 and (body.get("result") or {}).get("url"):
                    self._set("webhook_registered", "a webhook is registered, so polling is not possible")
                    logger.error("Telegram polling is not started: a webhook is registered for this bot. Remove the webhook "
                                 "(deleteWebhook) or turn TELEGRAM_POLLING off. It was not changed.")
                    return 60.0
                if code != 200:
                    raise RuntimeError("getWebhookInfo status %s" % code)
                self._webhook_checked = True
            payload = {"timeout": self._timeout, "allowed_updates": ["message"]}
            offset = self._offset()
            if offset is not None:
                payload["offset"] = offset
            t0 = time.monotonic()
            code, body = self._api("getUpdates", payload, self._timeout + 10)
            if code == 409:
                self._webhook_checked = False
                self._set("conflict", "Telegram reports another getUpdates or a webhook")
                logger.warning("Telegram polling: conflict (another poller or a webhook is active); checking again later.")
                return 30.0
            if code in (401, 404):
                self._set("bad_token", "Telegram refused the bot token")
                return 300.0
            if code == 429:
                wait = ((body.get("parameters") or {}).get("retry_after")) or 5
                self._set("error", "rate limited by Telegram")
                return float(max(1, min(120, int(wait))))
            if code != 200 or not body.get("ok"):
                raise RuntimeError("getUpdates status %s" % code)
            self._failures = 0
            self._set("polling")
            updates = body.get("result") or []
            for u in updates:
                self._dispatch(u)
            return 1.0 if (not updates and time.monotonic() - t0 < 0.5) else 0.0
        except Exception as e:                                      # the text of a requests error can hold the URL, so only the type is kept
            self._failures += 1
            self._set("error", type(e).__name__)
            logger.warning("Telegram polling error (%s); retrying.", type(e).__name__)
            return float(min(60, 2 ** self._failures))

    # ---- handling
    def _dispatch(self, update: dict) -> None:
        uid = update.get("update_id")
        if not isinstance(uid, int):
            return
        with self._lock:
            self._max_seen = uid if self._max_seen is None else max(self._max_seen, uid)
            if uid in self._seen:
                return                                              # delivered again: ignored while the id is remembered
            if len(self._seen_order) == self._seen_order.maxlen:
                self._seen.discard(self._seen_order[0])
            self._seen_order.append(uid)
            self._seen.add(uid)
            self._inflight.add(uid)
        if self._inline:
            self._run_one(update)
        else:
            self._executor.submit(self._run_one, update)

    def _chat_lock(self, update: dict):
        chat = ((update.get("message") or {}).get("chat") or {}).get("id")
        with self._lock:
            if len(self._chat_locks) > 1000:
                self._chat_locks = {k: v for k, v in self._chat_locks.items() if v.locked()}
            return self._chat_locks.setdefault(chat, threading.Lock())

    def _run_one(self, update: dict) -> None:
        uid = update.get("update_id")
        try:
            with self._chat_lock(update):                           # messages of one chat are handled in order
                self._handle(update)
            _STATE["handled"] = _STATE.get("handled", 0) + 1
        except Exception as e:                                      # a message that fails is not retried forever
            logger.warning("Telegram polling: a handler failed (%s); the update is acknowledged.", type(e).__name__)
        finally:
            with self._lock:
                self._inflight.discard(uid)

    # ---- thread
    def run(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.step()
            except Exception as e:                                  # step() should not raise; the loop must not die
                logger.warning("Telegram polling loop error (%s).", type(e).__name__)
                delay = 5.0
            self._stop.wait(delay)

    def start(self) -> "TelegramPoller":
        self._thread = threading.Thread(target=self.run, name="telegram-poller", daemon=True)
        self._thread.start()
        return self

    def stop(self, join_seconds: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(join_seconds)
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._set("off")


def public_answers() -> bool:
    """True only when TELEGRAM_POLLING_PUBLIC=true: the polled bot then answers ANY sender, as the webhook bot does."""
    return os.environ.get("TELEGRAM_POLLING_PUBLIC", "").strip().lower() == "true"


def owner_only(handle: Callable[[dict], object]) -> Callable[[dict], object]:
    """Wrap a handler so that only the owner's private chat is answered (the approval target on Telegram, see
    core.autonomy.is_owner_sender). Every other message is ignored without a reply, so a stranger who finds the bot learns
    nothing and cannot use its documents or its model budget. A check that cannot be made ignores the message."""
    def owner_only_handler(update: dict):
        message = (update or {}).get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        from_id = (message.get("from") or {}).get("id")
        private = chat.get("type") == "private" and (from_id is None or str(from_id) == str(chat_id))
        try:
            from core import autonomy
            is_owner = bool(chat_id) and private and autonomy.is_owner_sender("telegram", chat_id)
        except Exception:
            is_owner = False
        if not is_owner:
            logger.info("Telegram polling: a message that is not from the owner's private chat was ignored.")
            return False
        return handle(update)
    return owner_only_handler


def start_default() -> Optional[TelegramPoller]:
    """Start the poller from the environment, or return None when polling is not switched on."""
    global _DEFAULT
    if not enabled():
        return None
    from channels.telegram.router import handle_update
    from core import scheduler_leader
    handler = handle_update if public_answers() else owner_only(handle_update)       # polling answers only the owner unless asked
    _DEFAULT = TelegramPoller(os.environ["TELEGRAM_BOT_TOKEN"].strip(), handler, scheduler_leader.is_leader,
                              poll_timeout=_int_env("TELEGRAM_POLL_TIMEOUT", 30, 1, 50),
                              workers=_int_env("TELEGRAM_POLL_WORKERS", 4, 1, 8)).start()
    logger.info("Telegram long polling started.")
    return _DEFAULT
