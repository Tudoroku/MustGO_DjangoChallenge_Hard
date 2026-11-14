import math
import os
import socket
import time

from celery import shared_task
from django.db import OperationalError, transaction

from core import drafting
from core.models import DraftBatch, DraftJob
from core.task_base import RetryingNetworkTask
from core.task_base import TransientNetworkError


@shared_task(bind=True, base=RetryingNetworkTask)
def smoke_test(self, token, pause_seconds=0):
    """Return worker identity without reading or writing the application database."""
    if not isinstance(token, str):
        raise ValueError("token must be a string")
    if (
        type(pause_seconds) not in (int, float)
        or not math.isfinite(pause_seconds)
        or not 0 <= pause_seconds <= 5
    ):
        raise ValueError("pause_seconds must be finite and between 0 and 5")
    started_at = time.time()
    time.sleep(pause_seconds)
    return {
        "token": token,
        "task_id": self.request.id,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "started_at": started_at,
        "finished_at": time.time(),
    }


class DraftTask(RetryingNetworkTask):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        token = getattr(self.request, "draft_token", None)
        if token is not None:
            DraftJob.objects.filter(pk=args[0], token=token, state="published").update(
                state="failed", error=f"{type(exc).__name__}: {exc}", lease_until=None,
            )
        super().on_failure(exc, task_id, args, kwargs, einfo)


@shared_task(bind=True, base=DraftTask)
def execute_draft_job(self, job_id):
    """One leased background call per batch/stage; no child result waits."""
    job = None
    try:
        job = drafting.claim_job(job_id)
        if job is None:
            drafting.dispatch_jobs()
            return {"job": job_id, "claimed": False}
        self.request.draft_token = job.token
        if job.kind == "persist":
            drafting.persist_batch(job)
        elif job.kind == "fetch":
            batch = DraftBatch.objects.get(pk=job.batch_id)
            payload = batch.payload
            if payload is None:
                payload = drafting.fetch_json(f"/draft/{batch.pk}")
            drafting.validate_batch(payload)
            # HTTP never holds a database write transaction.
            with transaction.atomic():
                if drafting.owned_job(job):
                    drafting.save_download(batch.pk, payload, discover=True)
                    drafting.finish_job(job)
        elif job.kind == "instructions":
            batch = DraftBatch.objects.get(pk=job.batch_id)
            path = drafting.stage_path(batch.stage_path)
            info = drafting.fetch_json(path)
            if not isinstance(info, dict):
                raise ValueError("next-stage instructions must be a JSON object")
            with transaction.atomic():
                if drafting.owned_job(job):
                    DraftBatch.objects.filter(pk=batch.pk).update(stage_info=info)
                    drafting.finish_job(job)
        else:
            raise ValueError(f"Unknown drafting job kind {job.kind}")
        drafting.dispatch_jobs()
        return {"job": job_id, "claimed": True, "kind": job.kind, "batch": job.batch_id}
    except Exception as exc:
        if job is not None:
            DraftJob.objects.filter(pk=job.pk, token=job.token, state="running").update(
                state="published", error=f"{type(exc).__name__}: {exc}",
            )
        # SQLite lock contention is transient; integrity/schema errors are not.
        if isinstance(exc, OperationalError):
            code = getattr(exc.__cause__, "sqlite_errorcode", 0)
            if code & 255 in (5, 6):  # SQLITE_BUSY / SQLITE_LOCKED
                raise TransientNetworkError("SQLite writer contention") from exc
        raise
