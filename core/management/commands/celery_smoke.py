import json
from uuid import uuid4

from celery.exceptions import TimeoutError as ResultTimeout
from django.core.management.base import BaseCommand, CommandError
from kombu.exceptions import OperationalError
from redis.exceptions import RedisError

from backend.celery import app
from core.tasks import smoke_test


class Command(BaseCommand):
    help = "Verify Redis and two overlapping tasks on separate worker processes (no DB writes)."

    def handle(self, *args, **options):
        if app.conf.task_always_eager:
            raise CommandError("Smoke verification requires a real worker; eager mode is enabled.")
        results = []
        try:
            with app.connection_for_write() as connection:
                connection.ensure_connection(max_retries=0)
            tokens = [uuid4().hex, uuid4().hex]
            # A pause makes overlap observable, rather than inferring parallelism
            # just from the configured concurrency setting.
            for token in tokens:
                results.append(smoke_test.apply_async(args=[token, 2], retry=False))
            payloads = [result.get(timeout=30) for result in results]
            for result, token, payload in zip(results, tokens, payloads):
                if payload["task_id"] != result.id or payload["token"] != token:
                    raise CommandError("Worker returned an unexpected task identity.")
            identities = {(p["hostname"], p["pid"]) for p in payloads}
            overlap = max(p["started_at"] for p in payloads) < min(
                p["finished_at"] for p in payloads
            )
            if len(identities) != 2 or not overlap:
                raise CommandError("Tasks did not overlap on two processes; check prefork concurrency >= 2.")
            for result in results:
                result.forget()
        except (OperationalError, RedisError, ResultTimeout, OSError) as exc:
            raise CommandError(f"Celery smoke verification failed: {exc}") from exc
        for payload in payloads:
            self.stdout.write(json.dumps(payload, sort_keys=True))
        self.stdout.write(self.style.SUCCESS("Redis round trip and parallel worker execution verified."))
