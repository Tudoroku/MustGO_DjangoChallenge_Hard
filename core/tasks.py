import math
import os
import socket
import time

from celery import shared_task

from core.task_base import RetryingNetworkTask


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
