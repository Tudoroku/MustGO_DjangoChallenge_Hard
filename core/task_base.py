"""Opt-in retry policy for tasks whose effects are safe to repeat."""

from http.client import IncompleteRead, RemoteDisconnected

from celery import Task
from celery.exceptions import SoftTimeLimitExceeded
from django.core.exceptions import ValidationError


class TransientNetworkError(Exception):
    """An HTTP adapter may map a verified transient failure to this exception."""


class RetryingNetworkTask(Task):
    """Use only after making database writes idempotent; see docs/phase2.5.md."""

    abstract = True
    autoretry_for = (
        TimeoutError,
        ConnectionError,
        IncompleteRead,
        RemoteDisconnected,
        SoftTimeLimitExceeded,
        TransientNetworkError,
    )
    dont_autoretry_for = (ValidationError, ValueError, TypeError)
    max_retries = 5
    retry_backoff = 2
    retry_backoff_max = 60
    retry_jitter = True
    acks_late = True
