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
from core import fighting
from core.models import Fight, FightBatch


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


class FightTask(RetryingNetworkTask):
    abstract = True

    def on_failure(self, exc, task_id, args, kwargs, einfo):
        Fight.objects.filter(pk=args[0]).exclude(status="completed").update(status="failed", error=str(exc))
        super().on_failure(exc, task_id, args, kwargs, einfo)


@shared_task(bind=True, base=FightTask)
def simulate_fight(self, fight_id):
    started = time.time()
    try:
        applied = fighting.replay_fight(fight_id)
    except OperationalError as exc:
        if getattr(exc.__cause__, "sqlite_errorcode", 0) & 255 in (5, 6):
            raise TransientNetworkError("SQLite fight writer contention") from exc
        raise
    return {"fight_id": fight_id, "applied": applied, "pid": os.getpid(),
            "started_at": started, "finished_at": time.time()}


@shared_task(bind=True, base=RetryingNetworkTask)
def fetch_fight_batch(self, batch_id=0):
    drafting.validate_batch_id(batch_id)
    existing = FightBatch.objects.filter(pk=batch_id).first()
    payload = existing.payload if existing else drafting.fetch_json(f"/fight/{batch_id}")
    saved = fighting.save_fight_batch(batch_id, payload)
    # Each fight is scheduled immediately; never wait for subsequent batches.
    fighting.schedule_fights(saved.pk)
    if saved.next_id is not None and not saved.navigation_issue:
        claimed = FightBatch.objects.filter(pk=saved.pk, next_scheduled=False).update(next_scheduled=True)
        if claimed:
            try:
                fetch_fight_batch.apply_async(args=[saved.next_id], retry=False)
            except Exception:
                FightBatch.objects.filter(pk=saved.pk).update(next_scheduled=False)
                raise
    return {"batch_id": saved.pk, "next_id": saved.next_id, "issue": saved.navigation_issue}
