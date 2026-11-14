"""Real non-eager Celery consumer/pool + real HTTP + shared file-backed SQLite.

The transport is in-memory to run without Docker/Redis. These tests exercise
actual task publication, worker execution, concurrency, autoretry and failure
hooks. They do not claim Linux prefork or Redis transport coverage.
"""

import json
import threading
import time
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from celery import Celery, current_app
from celery.contrib.testing.worker import start_worker
from django.db import connection
from django.test import TransactionTestCase, override_settings

from core import drafting
from core.models import DraftBatch, DraftJob, Person
from core.test_drafting import batch, person


class DraftWorkerTests(TransactionTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.previous_app = current_app._get_current_object()
        cls.app = Celery("draft-worker-tests", broker="memory://", backend="cache+memory://")
        cls.app.conf.update(
            task_always_eager=False, task_serializer="json", accept_content=["json"],
            result_serializer="json", worker_prefetch_multiplier=1,
            broker_transport_options={"polling_interval": 0.05},
        )
        cls.app.set_current()
        cls.app.finalize()
        cls.worker_context = start_worker(
            cls.app, pool="threads", concurrency=4, perform_ping_check=False,
            shutdown_timeout=15, loglevel="CRITICAL",
        )
        cls.worker_context.__enter__()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.worker_context.__exit__(None, None, None)
            cls.app.close()
        finally:
            cls.previous_app.set_current()
            cls.previous_app.set_default()
            super().tearDownClass()

    def setUp(self):
        self.calls = Counter()
        self.lock = threading.Lock()
        self.routes = {}
        self.release_download = threading.Event()
        self.download_started = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                with owner.lock:
                    owner.calls[self.path] += 1
                    attempt = owner.calls[self.path]
                route = owner.routes.get(self.path)
                if route is None:
                    status, payload = 404, {"error": "unknown route"}
                else:
                    status, payload = route(attempt)
                raw = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(raw)))
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass  # Expected when the timeout test closes its connection.

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.overrides = override_settings(
            DRAFT_API_BASE_URL=f"http://127.0.0.1:{self.server.server_port}",
            DRAFT_CONNECT_TIMEOUT=1, DRAFT_READ_TIMEOUT=5,
        )
        self.overrides.enable()
        self.app.set_current()
        self.task = self.app.tasks["core.tasks.execute_draft_job"]

    def tearDown(self):
        self.release_download.set()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        self.overrides.disable()
        # Wait until no pool thread is still changing the database before flush.
        self.wait_for(lambda: not DraftJob.objects.filter(state="running").exists())
        super().tearDown()

    def wait_for(self, predicate, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.03)
        self.fail(f"Timed out waiting for real worker; status={drafting.draft_status()}")

    def wait_succeeded(self):
        self.wait_for(lambda: DraftJob.objects.exists() and not DraftJob.objects.exclude(state="succeeded").exists())

    def test_pipeline_persists_during_later_download_retries_and_follows_updated_key(self):
        self.routes["/draft/0"] = lambda attempt: (200, batch(next_batch=7))

        def later(attempt):
            if attempt == 1:
                return 503, {"error": "try again"}
            self.download_started.set()
            self.release_download.wait(timeout=8)
            return 200, batch(person(99, name="Hellstorm Fireburn", key="stage-three-info"), next_batch=None)

        self.routes["/draft/7"] = later
        self.routes["/stage-three-info"] = lambda attempt: (200, {"data": {"message": "saved instructions"}})
        self.assertFalse(self.app.conf.task_always_eager)
        drafting.start_drafting()
        self.assertTrue(self.download_started.wait(timeout=10))
        self.wait_for(lambda: Person.objects.filter(pk=0, rank="Unlisted rank").exists())
        self.assertFalse(Person.objects.filter(pk=99).exists())
        self.assertIsNotNone(DraftBatch.objects.get(pk=0).processed_at)
        self.assertEqual(DraftJob.objects.get(batch_id=7, kind="fetch").state, "running")
        self.release_download.set()
        self.wait_succeeded()
        self.assertEqual(Person.objects.get(pk=99).key, "stage-three-info")
        self.assertEqual(DraftBatch.objects.get(pk=7).stage_info["data"]["message"], "saved instructions")
        self.assertEqual(dict(self.calls), {"/draft/0": 1, "/draft/7": 2, "/stage-three-info": 1})
        self.assertEqual(DraftJob.objects.get(batch_id=7, kind="fetch").attempts, 2)
        self.assertFalse(drafting.draft_status()["complete"])

    def test_actual_http_header_timeout_retries(self):
        def slow_first(attempt):
            if attempt == 1:
                time.sleep(0.25)
            return 200, batch(next_batch=None)

        self.routes["/draft/0"] = slow_first
        with override_settings(DRAFT_READ_TIMEOUT=0.08):
            drafting.start_drafting()
            self.wait_succeeded()
        self.assertEqual(self.calls["/draft/0"], 2)
        self.assertEqual(DraftJob.objects.get(kind="fetch").attempts, 2)
        self.assertEqual(Person.objects.count(), 1)

    def test_real_worker_exhausts_bounded_retries(self):
        self.routes["/draft/0"] = lambda attempt: (503, {})
        # Select a valid zero full-jitter delay; HTTP, publication, worker
        # execution and retries are real. Exact ceilings have separate tests.
        with patch("celery.utils.time.random.randrange", return_value=0):
            drafting.start_drafting()
            self.wait_for(lambda: DraftJob.objects.filter(kind="fetch", state="failed").exists())
        self.assertEqual(self.calls["/draft/0"], 6)
        self.assertEqual(DraftJob.objects.get().attempts, 6)
        self.assertFalse(Person.objects.exists())
        self.assertFalse(drafting.draft_status()["complete"])

    def test_incomplete_batch_fails_without_saving_or_discovery(self):
        payload = batch(person(), {"id": 1}, next_batch=9)
        self.routes["/draft/0"] = lambda attempt: (200, payload)
        drafting.start_drafting()
        self.wait_for(lambda: DraftJob.objects.filter(state="failed").exists())
        self.assertFalse(Person.objects.exists())
        self.assertIsNone(DraftBatch.objects.get(pk=0).payload)
        self.assertFalse(DraftBatch.objects.filter(pk=9).exists())
        self.assertEqual(self.calls["/draft/0"], 1)

    def test_actual_concurrent_duplicate_deliveries_do_not_reapply(self):
        for index in range(2):
            drafting.save_download(index, batch(person(index), next_batch=None))
        ids = list(DraftJob.objects.values_list("pk", flat=True))
        results = [self.task.apply_async(args=[str(job_id)]) for _ in range(5) for job_id in ids]
        for result in results:
            self.assertIn("claimed", result.get(timeout=15))
            result.forget()
        self.wait_succeeded()
        self.assertEqual(Person.objects.count(), 2)
        self.assertEqual(list(DraftJob.objects.order_by("batch_id").values_list("attempts", flat=True)), [1, 1])
        Person.objects.filter(pk=0).update(health=17, key="newer-key")
        self.task.apply_async(args=[str(ids[0])]).get(timeout=15)
        self.assertEqual((Person.objects.get(pk=0).health, Person.objects.get(pk=0).key), (17, "newer-key"))

    def test_real_database_failure_rolls_back_then_explicit_retry_succeeds(self):
        drafting.save_download(0, batch(person(), person(1), next_batch=None))
        with connection.cursor() as cursor:
            cursor.execute("""CREATE TRIGGER draft_test_abort BEFORE INSERT ON core_person
                              WHEN NEW.id = 1 BEGIN SELECT RAISE(ABORT, 'forced rollback'); END""")
        try:
            drafting.dispatch_jobs()
            self.wait_for(lambda: DraftJob.objects.filter(state="failed").exists())
            self.assertFalse(Person.objects.exists())
            self.assertIsNone(DraftBatch.objects.get(pk=0).processed_at)
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TRIGGER draft_test_abort")
        DraftJob.objects.update(state="pending", attempts=0, token=None, lease_until=None)
        drafting.dispatch_jobs()
        self.wait_succeeded()
        self.assertEqual(Person.objects.count(), 2)

    def test_real_worker_detects_cycle_without_duplicate_fetch(self):
        self.routes["/draft/0"] = lambda attempt: (200, batch(next_batch=6))
        self.routes["/draft/6"] = lambda attempt: (200, batch(person(1), next_batch=0))
        drafting.start_drafting()
        self.wait_succeeded()
        self.assertEqual(dict(self.calls), {"/draft/0": 1, "/draft/6": 1})
        self.assertIn("cyclic", DraftBatch.objects.get(pk=6).navigation_issue)
        self.assertFalse(drafting.draft_status()["complete"])
