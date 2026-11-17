"""
Optional Redis-backed task queue for RagLeap Core.

RagLeap Core works entirely without this - the background integration
sync job (core/api.py's _sync_job) runs each due source's sync inline in
the FastAPI process by default, exactly as before. Setting REDIS_URL
switches to enqueuing that same work onto a real Redis queue instead,
processed by separate `worker` process(es) (see docker-compose.yml's
`worker` service and Dockerfile.worker), which can then be scaled
independently of the API process - horizontally via
`docker compose up --scale worker=N`, or in Kubernetes via a queue-length
based autoscaler such as KEDA's Redis List scaler (see
examples/keda-scaledobject-worker.yaml for a reference ScaledObject).

Deliberately minimal: one queue ("default"), one job type (integration
sync) for this first version. Not a general task-queue framework - more
job types are a natural follow-up once this pattern is proven in real
use, matching the same "narrow first version" approach already used for
core/employees/triggers.py's autonomous escalation trigger.
"""
import logging
import os
import re
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

REDIS_URL = os.environ.get("REDIS_URL", "")

_connection = None
_queue = None
_unavailable_logged = False


def _redact_url(url: str) -> str:
    """The URL without its user and password part, safe to write to a log."""
    return re.sub(r"//[^@/]*@", "//***@", url or "")


def _get_queue():
    """
    Lazily create the RQ Queue on first real use, not at import time -
    so importing this module never requires redis/rq to be installed or
    reachable unless REDIS_URL is actually set. Returns None if queuing
    isn't configured or Redis can't be reached, in which case callers
    should fall back to running the work inline.
    """
    global _connection, _queue, _unavailable_logged

    if not REDIS_URL:
        return None

    if _queue is not None:
        return _queue

    try:
        import redis
        from rq import Queue

        _connection = redis.from_url(REDIS_URL)
        _connection.ping()
        _queue = Queue("default", connection=_connection)
        logger.info("Redis queue connected (%s) - integration syncs will be enqueued, not run inline.", _redact_url(REDIS_URL))
        return _queue
    except Exception as e:
        if not _unavailable_logged:
            logger.warning(
                "REDIS_URL is set but the queue is unavailable (%s) - "
                "falling back to running integration syncs inline, as if "
                "REDIS_URL were unset. Will keep retrying on each call.",
                e,
            )
            _unavailable_logged = True
        return None


def enqueue_sync(data_source_id: str) -> Dict[str, Any]:
    """
    Enqueue an integration sync job if a queue is configured and reachable;
    otherwise run it inline immediately, exactly as core/api.py's _sync_job
    did before this module existed. Either way, the caller gets a small
    status dict back - it does NOT get the sync result itself when queued,
    since that only becomes available once a worker actually processes it
    (see last_sync_status on the data source row for that, same as today).
    """
    from core.integrations import service as integrations_service

    queue = _get_queue()
    if queue is None:
        result = integrations_service.sync_data_source(data_source_id)
        return {"status": "ran_inline", "result": result}

    job = queue.enqueue(integrations_service.sync_data_source, data_source_id)
    return {"status": "enqueued", "job_id": job.id}
