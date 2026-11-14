"""Draft validation, immutable inputs, transactional persistence and outbox.

No completion signal is currently verified. A missing/null pointer stops
discovery with a visible issue; it never means that the draft is complete.
"""

import errno
import json
import socket
import uuid
from datetime import timedelta
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlsplit

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from kombu.exceptions import OperationalError as BrokerError

from core.models import DraftBatch, DraftJob, Person
from core.task_base import TransientNetworkError

MAX_BATCH_ID = 2**63 - 1  # Storage bound, not an upstream batch count.
LEASE_SECONDS = 600
MAX_ATTEMPTS = 6


def validate_batch_id(value):
    if type(value) is not int or not 0 <= value <= MAX_BATCH_ID:
        raise ValueError("batch ID must be a nonnegative integer within SQLite's integer range")
    return value


def validate_batch(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("draft response must be an object with a data array")
    if type(payload.get("status")) is not int or payload["status"] != 200:
        raise ValueError("draft response must have integer status 200")
    rows, seen = [], set()
    for index, record in enumerate(payload["data"]):
        prefix = f"data[{index}]"
        if not isinstance(record, dict):
            raise ValueError(f"{prefix}: expected a person object")
        person_id = record.get("id")
        if type(person_id) is not int or not 0 <= person_id <= 99:
            raise ValueError(f"{prefix}: id must be an integer between 0 and 99")
        if person_id in seen:
            raise ValueError(f"{prefix}: duplicate person ID {person_id}")
        seen.add(person_id)
        for field in ("age", "health"):
            if type(record.get(field)) is not int or not 0 <= record[field] <= 2147483647:
                raise ValueError(f"{prefix}: {field} must be a nonnegative 32-bit integer")
        for field in ("name", "rank"):
            if not isinstance(record.get(field), str) or not record[field].strip():
                raise ValueError(f"{prefix}: {field} must be a nonblank string")
        if "key" in record and not isinstance(record["key"], str):
            raise ValueError(f"{prefix}: key must be a string when supplied")
        row = {k: record[k] for k in ("id", "name", "age", "rank", "health")}
        if "key" in record:
            row["key"] = record["key"]
        rows.append(row)
    if "next_batch" not in payload:
        return rows, None, "missing next_batch; termination contract unverified"
    if payload["next_batch"] is None:
        return rows, None, "null next_batch; termination contract unverified"
    try:
        next_id = validate_batch_id(payload["next_batch"])
    except ValueError:
        return rows, None, "invalid next_batch; discovery stopped"
    return rows, next_id, ""


def stage_path(key):
    """Interpret a supplied route only on the configured API origin."""
    url = urlsplit(key)
    if (not key or key != key.strip() or url.scheme or url.netloc or url.query
            or url.fragment or "\\" in key or "%" in key or key.startswith("//")
            or any(part in (".", "..") for part in url.path.split("/"))
            or any(ord(char) < 33 for char in key)):
        raise ValueError("Hellstorm's key must be a plain same-origin API path")
    return "/" + url.path.lstrip("/")


def fetch_json(path):
    """One HTTP attempt; Celery owns retries, backoff and jitter."""
    base = urlsplit(settings.DRAFT_API_BASE_URL)
    if base.scheme not in ("http", "https") or not base.hostname or base.path not in ("", "/"):
        raise ValueError("DRAFT_API_BASE_URL must be an HTTP(S) origin")
    cls = HTTPSConnection if base.scheme == "https" else HTTPConnection
    connection = cls(base.hostname, port=base.port, timeout=settings.DRAFT_CONNECT_TIMEOUT)
    try:
        connection.connect()
        connection.sock.settimeout(settings.DRAFT_READ_TIMEOUT)
        connection.request("GET", path)
        with connection.getresponse() as response:
            if response.status in (408, 429) or 500 <= response.status <= 599:
                raise TransientNetworkError(f"GET {path}: HTTP {response.status}")
            if response.status != 200:
                raise ValueError(f"GET {path}: permanent HTTP {response.status}")
            return json.load(response)
    except socket.gaierror as exc:
        if exc.errno == socket.EAI_AGAIN:
            raise TransientNetworkError("Temporary DNS failure") from exc
        raise
    except OSError as exc:
        if exc.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ETIMEDOUT):
            raise TransientNetworkError("Temporary network failure") from exc
        raise
    finally:
        connection.close()


def ensure_job(batch, kind):
    return DraftJob.objects.get_or_create(batch=batch, kind=kind)[0]


def _cycle(batch_id, next_id):
    seen = {batch_id}
    while next_id is not None:
        if next_id in seen:
            return True
        seen.add(next_id)
        next_id = DraftBatch.objects.filter(pk=next_id).values_list("next_id", flat=True).first()
    return False


def save_download(batch_id, payload, *, discover=False):
    """Validate first, then atomically store the batch and its outgoing jobs.

    File imports never enqueue network requests. The normal fetch task may
    subsequently discover successors from these same immutable saved inputs.
    Caller dispatches the committed outbox, so broker failure cannot lose data.
    """
    validate_batch_id(batch_id)
    _, next_id, issue = validate_batch(payload)
    with transaction.atomic():
        batch, _ = DraftBatch.objects.get_or_create(pk=batch_id)
        if batch.payload is not None and batch.payload != payload:
            raise ValueError(f"batch {batch_id} already has a different immutable payload")
        if _cycle(batch_id, next_id):
            issue = "repeated/cyclic next_batch; discovery stopped"
        batch.payload = payload
        batch.next_id = next_id
        batch.navigation_issue = issue
        batch.downloaded_at = batch.downloaded_at or timezone.now()
        batch.save(update_fields=["payload", "next_id", "navigation_issue", "downloaded_at"])
        ensure_job(batch, "persist")
        if discover and not issue:
            successor, _ = DraftBatch.objects.get_or_create(pk=next_id)
            ensure_job(successor, "fetch")
        return batch


def start_drafting():
    with transaction.atomic():
        batch, _ = DraftBatch.objects.get_or_create(pk=0)
        ensure_job(batch, "fetch")
    return dispatch_jobs()


def claim_job(job_id):
    now = timezone.now()
    with transaction.atomic():
        job = DraftJob.objects.select_for_update().get(pk=job_id)
        if job.state in ("succeeded", "failed"):
            return None
        if job.state == "running" and job.lease_until and job.lease_until > now:
            return None
        if job.attempts >= MAX_ATTEMPTS:
            job.state, job.error = "failed", "execution attempt limit reached"
            job.save(update_fields=["state", "error"])
            return None
        job.token = uuid.uuid4()
        job.state = "running"
        job.attempts += 1
        job.lease_until = now + timedelta(seconds=LEASE_SECONDS)
        job.save(update_fields=["token", "state", "attempts", "lease_until"])
        return job


def owned_job(job):
    # Called only within a write transaction. Token fencing protects against
    # a stale worker returning after reconciliation reassigned its lease.
    return DraftJob.objects.filter(pk=job.pk, state="running", token=job.token).exists()


def finish_job(job):
    DraftJob.objects.filter(pk=job.pk, token=job.token, state="running").update(
        state="succeeded", lease_until=None, error="",
    )


def persist_batch(job):
    with transaction.atomic():
        if not owned_job(job):
            return
        batch = DraftBatch.objects.get(pk=job.batch_id)
        rows, _, _ = validate_batch(batch.payload)
        if batch.processed_at is None:
            for row in sorted(rows, key=lambda row: row["id"]):
                person, _ = Person.objects.get_or_create(
                    pk=row["id"], defaults={"name": row["name"], "age": row["age"]},
                )
                if person.draft_batch_id not in (None, batch.pk):
                    raise ValidationError(f"person {person.pk} appears in conflicting batches")
                updates = {k: row[k] for k in ("name", "age", "rank")}
                updates.update(draft_health=row["health"], draft_batch_id=batch.pk)
                if "key" in row:
                    updates["key"] = row["key"]
                Person.objects.filter(pk=person.pk).update(**updates)
                # Atomic predicate, never a read-modify-write on combat health.
                Person.objects.filter(pk=person.pk, health__isnull=True).update(health=row["health"])
                if row["name"] == "Hellstorm Fireburn" and row.get("key"):
                    batch.stage_path = row["key"]
            batch.processed_at = timezone.now()
            batch.save(update_fields=["processed_at", "stage_path"])
            if batch.stage_path:
                ensure_job(batch, "instructions")
        finish_job(job)


def dispatch_jobs():
    """Publish durable pending work; re-run to repair commit/publish crashes.

    A broker acknowledgement can be lost after publication. Exactly-once
    delivery across SQLite and Redis is impossible; claims deduplicate effects.
    """
    from core.tasks import execute_draft_job

    count = 0
    ready = Q(state="pending") | Q(
        state__in=["published", "running"], lease_until__lte=timezone.now(),
    )
    # Persist is published first, before any subsequent download.
    for kind in ("persist", "fetch", "instructions"):
        ids = list(DraftJob.objects.filter(ready, kind=kind).values_list("pk", flat=True))
        for job_id in ids:
            lease = timezone.now() + timedelta(seconds=LEASE_SECONDS)
            changed = DraftJob.objects.filter(ready, pk=job_id).update(
                state="published", token=None, lease_until=lease,
            )
            if not changed:
                continue
            try:
                execute_draft_job.apply_async(args=[str(job_id)], task_id=str(job_id), retry=False)
            except Exception as exc:
                # Do not mask programming/validation errors as network retries.
                DraftJob.objects.filter(pk=job_id, state="published", lease_until=lease).update(
                    state="pending", lease_until=None,
                )
                if isinstance(exc, (BrokerError, ConnectionError, TimeoutError)):
                    raise TransientNetworkError("Could not publish draft outbox; run draft --resume") from exc
                raise
            count += 1
    return count


def draft_status():
    return {
        "complete": False,
        "completion_reason": "No verified API termination signal is implemented.",
        "batches": list(DraftBatch.objects.order_by("id").values(
            "id", "downloaded_at", "processed_at", "next_id", "navigation_issue", "stage_path", "stage_info",
        )),
        "jobs": list(DraftJob.objects.order_by("batch_id", "kind").values(
            "id", "batch_id", "kind", "state", "attempts", "error",
        )),
    }
