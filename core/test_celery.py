import io
import json
import os
from http.client import IncompleteRead, RemoteDisconnected
from unittest.mock import Mock, patch

from celery import Celery
from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from backend import celery_app
from core.task_base import RetryingNetworkTask, TransientNetworkError
from core.tasks import smoke_test


class CeleryConfigurationTests(SimpleTestCase):
    def test_django_app_and_task_discovery(self):
        celery_app.autodiscover_tasks(force=True)
        self.assertEqual(celery_app.main, "backend")
        self.assertIs(smoke_test.app, celery_app)
        self.assertIn("core.tasks.smoke_test", celery_app.tasks)
        self.assertEqual(celery_app.conf.broker_url, settings.CELERY_BROKER_URL)
        self.assertEqual(celery_app.conf.result_backend, settings.CELERY_RESULT_BACKEND)
        self.assertTrue(celery_app.conf.broker_url.startswith(("redis://", "rediss://")))

    def test_tasks_are_asynchronous_and_use_json(self):
        self.assertFalse(celery_app.conf.task_always_eager)
        self.assertEqual(celery_app.conf.task_serializer, "json")
        self.assertEqual(celery_app.conf.result_serializer, "json")
        self.assertEqual(celery_app.conf.accept_content, ["json"])
        self.assertEqual(celery_app.conf.worker_prefetch_multiplier, 1)

    def test_environment_overrides_defaults(self):
        # Execute settings in isolation; do not mutate the running Django config.
        namespace = {"__file__": str(settings.BASE_DIR / "backend" / "settings.py")}
        with patch.dict(os.environ, {
            "CELERY_BROKER_URL": "redis://example.test:6380/3",
            "CELERY_RESULT_BACKEND": "redis://example.test:6380/4",
            "DJANGO_SECRET_KEY": "test-only",
            "DJANGO_DEBUG": "false",
            "DJANGO_ALLOWED_HOSTS": " localhost, example.test, ",
        }):
            exec(compile((settings.BASE_DIR / "backend" / "settings.py").read_text(),
                         "settings.py", "exec"), namespace)
        self.assertEqual(namespace["CELERY_BROKER_URL"], "redis://example.test:6380/3")
        self.assertEqual(namespace["CELERY_RESULT_BACKEND"], "redis://example.test:6380/4")
        self.assertEqual(namespace["SECRET_KEY"], "test-only")
        self.assertFalse(namespace["DEBUG"])
        self.assertEqual(namespace["ALLOWED_HOSTS"], ["localhost", "example.test"])

    def test_smoke_task_has_no_database_effects_and_serializes_to_json(self):
        # SimpleTestCase forbids database access. This is a unit test, not evidence
        # of broker/worker execution; manage.py celery_smoke supplies that check.
        with patch("core.tasks.time.sleep"):
            first = smoke_test.apply(args=["same-input"], task_id="first").get()
            second = smoke_test.apply(args=["same-input"], task_id="second").get()
        self.assertEqual(first["token"], second["token"])
        self.assertEqual(first["task_id"], "first")
        self.assertEqual(second["task_id"], "second")
        self.assertGreater(first["pid"], 0)
        json.dumps(first)

    def test_smoke_task_rejects_invalid_arguments_without_sleeping(self):
        with patch("core.tasks.time.sleep") as sleep:
            for token, pause in ((None, 0), ("ok", -1), ("ok", 6),
                                 ("ok", float("nan")), ("ok", True), ("ok", "1")):
                with self.subTest(token=token, pause=pause):
                    result = smoke_test.apply(args=[token, pause])
                    self.assertIsInstance(result.result, ValueError)
        sleep.assert_not_called()

    def test_smoke_command_rejects_eager_mode(self):
        with patch("core.management.commands.celery_smoke.app") as app:
            app.conf.task_always_eager = True
            with self.assertRaisesMessage(CommandError, "requires a real worker"):
                call_command("celery_smoke", stdout=io.StringIO())


class CeleryRetryTests(SimpleTestCase):
    def setUp(self):
        self.app = Celery("retry-tests", broker="memory://", set_as_current=False)
        self.app.conf.update(task_always_eager=True, task_eager_propagates=False)
        self.addCleanup(self.app.close)

    def task_for(self, operation, name="probe"):
        @self.app.task(base=RetryingNetworkTask, name=name, shared=False)
        def probe():
            return operation()
        return probe

    def test_transient_network_errors_and_timeouts_retry_then_succeed(self):
        for error in (
            TimeoutError("timeout"), ConnectionResetError("reset"),
            ConnectionRefusedError("refused"), IncompleteRead(b"partial"),
            RemoteDisconnected("closed"), SoftTimeLimitExceeded(),
            TransientNetworkError("temporary upstream failure"),
        ):
            with self.subTest(error=type(error).__name__):
                # Each task app has a distinct task name to avoid registry reuse.
                operation = Mock(side_effect=[error, "done"])
                task = self.task_for(operation, f"probe.{type(error).__name__}")
                self.assertEqual(task.apply().get(), "done")
                self.assertEqual(operation.call_count, 2)

    def test_retries_stop_at_maximum_and_preserve_original_error(self):
        operation = Mock(side_effect=TimeoutError("still unavailable"))
        task = self.task_for(operation)
        result = task.apply()
        self.assertEqual(operation.call_count, 6)  # initial attempt + five retries
        self.assertEqual(result.state, "FAILURE")
        with self.assertRaisesMessage(TimeoutError, "still unavailable"):
            result.get()

    def test_backoff_grows_exponentially_and_is_capped(self):
        operation = Mock(side_effect=TimeoutError("timeout"))
        task = self.task_for(operation)
        # Remove randomness by selecting the largest allowed full-jitter delay.
        with patch("celery.utils.time.random.randrange", side_effect=lambda stop: stop - 1), \
             patch.object(task, "retry", wraps=task.retry) as retry:
            task.apply()
        self.assertEqual([c.kwargs["countdown"] for c in retry.call_args_list],
                         [2, 4, 8, 16, 32, 60])

    def test_jitter_can_reduce_delay(self):
        operation = Mock(side_effect=[TimeoutError(), TimeoutError(), "done"])
        task = self.task_for(operation)
        with patch("celery.utils.time.random.randrange", side_effect=[0, 2]) as random, \
             patch.object(task, "retry", wraps=task.retry) as retry:
            self.assertEqual(task.apply().get(), "done")
        self.assertEqual(random.call_count, 2)
        self.assertEqual([c.kwargs["countdown"] for c in retry.call_args_list], [0, 2])

    def test_permanent_validation_and_programming_errors_are_not_retried(self):
        for error in (ValidationError("bad data"), ValueError("invalid JSON"),
                      TypeError("wrong type"), RuntimeError("bug"),
                      FileNotFoundError("missing local input")):
            with self.subTest(error=type(error).__name__):
                operation = Mock(side_effect=error)
                task = self.task_for(operation, f"permanent.{type(error).__name__}")
                result = task.apply()
                self.assertEqual(result.state, "FAILURE")
                self.assertEqual(operation.call_count, 1)
