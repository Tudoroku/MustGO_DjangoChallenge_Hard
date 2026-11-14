import io
import json
import socket
import uuid
from datetime import timedelta
from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from kombu.exceptions import OperationalError as BrokerError

from core import drafting
from core.models import DraftBatch, DraftJob, Person
from core.task_base import TransientNetworkError


def person(person_id=0, **changes):
    return {"id": person_id, "name": f"Person {person_id}", "age": 21,
            "rank": "Unlisted rank", "health": 100, **changes}


def batch(*rows, next_batch=1):
    return {"status": 200, "data": list(rows or [person()]), "next_batch": next_batch}


class DraftValidationTests(TestCase):
    def test_confirmed_saved_first_batch(self):
        # This fixture is the verified ten-record response, not a guessed last batch.
        payload = json.loads((Path(__file__).parent / "fixtures" / "draft-0.json").read_text())
        rows, pointer, issue = drafting.validate_batch(payload)
        self.assertEqual((len(rows), pointer, issue), (10, 1, ""))
        self.assertEqual(rows[0]["id"], 0)

    def test_invalid_envelopes_and_status(self):
        for payload in (None, [], {}, {"data": []}, {"data": None, "status": 200},
                        {"data": [], "status": True}, {"data": [], "status": 500}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                drafting.validate_batch(payload)

    def test_invalid_and_incomplete_records_validate_before_any_write(self):
        bad = [None, [], {}]
        for field in ("id", "name", "age", "rank", "health"):
            row = person(1)
            del row[field]
            bad.append(row)
        for field, values in (
            ("id", [-1, 100, True, 1.0, "1"]),
            ("age", [-1, True, 1.5, "21", None, 2**64]),
            ("health", [-1, True, 100.0, "100", None, 2**64]),
            ("name", [None, "", " ", 2]), ("rank", [None, "", " ", 1]),
            ("key", [None, True, []]),
        ):
            bad.extend(person(1, **{field: value}) for value in values)
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                drafting.save_download(0, batch(person(), row))
            self.assertFalse(DraftBatch.objects.exists())
            self.assertFalse(Person.objects.exists())

    def test_duplicate_ids_rejected(self):
        with self.assertRaisesMessage(ValueError, "duplicate"):
            drafting.save_download(0, batch(person(), person()))

    def test_pointer_issues_are_visible_and_never_completion(self):
        for pointer in (None, -1, True, 1.0, "1", {}, 2**63):
            with self.subTest(pointer=pointer):
                _, next_id, issue = drafting.validate_batch(batch(next_batch=pointer))
                self.assertIsNone(next_id)
                self.assertTrue(issue)
        payload = batch()
        del payload["next_batch"]
        self.assertIn("missing", drafting.validate_batch(payload)[2])
        self.assertFalse(drafting.draft_status()["complete"])

    def test_empty_data_is_not_a_termination_signal(self):
        payload = {"data": [], "status": 200, "next_batch": 7}
        saved = drafting.save_download(0, payload, discover=True)
        self.assertEqual(saved.next_id, 7)
        self.assertTrue(DraftJob.objects.filter(batch_id=7, kind="fetch").exists())
        self.assertFalse(drafting.draft_status()["complete"])

    def test_unverified_extra_completion_flags_are_ignored(self):
        drafting.save_download(0, {**batch(next_batch=None), "complete": True, "last_batch": True})
        self.assertFalse(drafting.draft_status()["complete"])


class DraftPersistenceTests(TestCase):
    def save(self, payload=None, batch_id=0):
        saved = drafting.save_download(batch_id, payload or batch())
        return drafting.claim_job(DraftJob.objects.get(batch=saved, kind="persist").pk)

    def test_preserves_key_ids_and_combat_health(self):
        Person.objects.create(id=0, name="Original", age=30, key="old-key", health=31)
        job = self.save()
        drafting.persist_batch(job)
        saved = Person.objects.get(pk=0)
        self.assertEqual((saved.rank, saved.draft_health, saved.health, saved.key),
                         ("Unlisted rank", 100, 31, "old-key"))
        self.assertEqual(saved.id, 0)

    def test_nullable_fields_and_initialize_health_once(self):
        saved = Person.objects.create(id=0, name="Original", age=21)
        self.assertIsNone(saved.rank)
        self.assertIsNone(saved.health)
        job = self.save()
        drafting.persist_batch(job)
        Person.objects.filter(pk=0).update(health=43, key="combat-stage")
        drafting.persist_batch(job)
        saved.refresh_from_db()
        self.assertEqual((saved.health, saved.draft_health, saved.key), (43, 100, "combat-stage"))

    def test_updated_key_is_saved_and_next_instructions_enqueued(self):
        job = self.save(batch(person(99, name="Hellstorm Fireburn", key="new-stage")))
        drafting.persist_batch(job)
        self.assertEqual(Person.objects.get(pk=99).key, "new-stage")
        self.assertTrue(DraftJob.objects.filter(batch_id=0, kind="instructions").exists())
        self.assertEqual(DraftBatch.objects.get(pk=0).stage_path, "new-stage")

    def test_omitted_key_does_not_follow_old_key(self):
        Person.objects.create(id=99, name="Hellstorm Fireburn", age=30, key="old-stage")
        drafting.persist_batch(self.save(batch(person(99, name="Hellstorm Fireburn"))))
        self.assertEqual(Person.objects.get(pk=99).key, "old-stage")
        self.assertFalse(DraftJob.objects.filter(kind="instructions").exists())

    def test_idempotent_storage_and_claim(self):
        job = self.save()
        drafting.save_download(0, batch())
        self.assertEqual(DraftBatch.objects.count(), 1)
        self.assertEqual(DraftJob.objects.count(), 1)
        self.assertIsNone(drafting.claim_job(job.pk))
        drafting.persist_batch(job)
        self.assertIsNone(drafting.claim_job(job.pk))
        self.assertEqual(Person.objects.count(), 1)

    def test_conflicting_replacement_rejected(self):
        self.save()
        with self.assertRaisesMessage(ValueError, "immutable"):
            drafting.save_download(0, batch(person(health=90)))
        self.assertEqual(DraftBatch.objects.get(pk=0).payload, batch())

    def test_cross_batch_overlap_rolls_back_other_people(self):
        drafting.persist_batch(self.save(batch(person(1)), batch_id=0))
        job = self.save(batch(person(), person(1)), batch_id=1)
        with self.assertRaisesMessage(ValidationError, "conflicting batches"):
            drafting.persist_batch(job)
        self.assertFalse(Person.objects.filter(pk=0).exists())
        self.assertIsNone(DraftBatch.objects.get(pk=1).processed_at)
        self.assertEqual(DraftJob.objects.get(pk=job.pk).state, "running")

    def test_database_error_rolls_back_all_writes_and_completion(self):
        job = self.save(batch(person(), person(1)))
        from django.db.models.query import QuerySet
        update = QuerySet.update

        def fail_late(query, **kwargs):
            if kwargs.get("draft_batch_id") == 0 and query.filter(pk=1).exists():
                raise IntegrityError("forced late database failure")
            return update(query, **kwargs)

        with patch.object(QuerySet, "update", new=fail_late):
            with self.assertRaises(IntegrityError):
                drafting.persist_batch(job)
        self.assertFalse(Person.objects.exists())
        self.assertIsNone(DraftBatch.objects.get(pk=0).processed_at)
        drafting.persist_batch(job)
        self.assertEqual(Person.objects.count(), 2)

    def test_stale_worker_token_cannot_write(self):
        old = self.save()
        DraftJob.objects.filter(pk=old.pk).update(lease_until=timezone.now() - timedelta(seconds=1))
        new = drafting.claim_job(old.pk)
        self.assertNotEqual(old.token, new.token)
        drafting.persist_batch(old)
        self.assertFalse(Person.objects.exists())
        drafting.persist_batch(new)
        self.assertEqual(Person.objects.count(), 1)

    def test_targeted_update_preserves_key_changed_after_person_read(self):
        Person.objects.create(id=0, name="Old", age=20)
        original = Person.objects.get_or_create

        def read_then_concurrent_change(*args, **kwargs):
            result = original(*args, **kwargs)
            Person.objects.filter(pk=0).update(key="newer-key", health=7)
            return result

        job = self.save()
        with patch.object(Person.objects, "get_or_create", side_effect=read_then_concurrent_change):
            drafting.persist_batch(job)
        saved = Person.objects.get(pk=0)
        self.assertEqual((saved.key, saved.health), ("newer-key", 7))


class DraftNavigationAndOutboxTests(TestCase):
    def test_nonconsecutive_pointer_and_duplicate_discovery(self):
        drafting.save_download(0, batch(next_batch=42), discover=True)
        drafting.save_download(0, batch(next_batch=42), discover=True)
        self.assertEqual(set(DraftBatch.objects.values_list("pk", flat=True)), {0, 42})
        self.assertEqual(DraftJob.objects.filter(batch_id=42, kind="fetch").count(), 1)

    def test_repeated_and_self_pointers_stop_discovery(self):
        saved = drafting.save_download(8, batch(next_batch=8), discover=True)
        self.assertIn("cyclic", saved.navigation_issue)
        self.assertFalse(DraftJob.objects.filter(kind="fetch").exists())
        drafting.save_download(0, batch(next_batch=5), discover=True)
        saved = drafting.save_download(5, batch(person(1), next_batch=0), discover=True)
        self.assertIn("cyclic", saved.navigation_issue)
        self.assertFalse(DraftJob.objects.filter(batch_id=0, kind="fetch").exists())

    def test_unknown_pointers_still_schedule_valid_persistence(self):
        for batch_id, pointer in enumerate((None, "bad", -1)):
            saved = drafting.save_download(batch_id, batch(next_batch=pointer), discover=True)
            self.assertTrue(saved.navigation_issue)
        self.assertEqual(DraftJob.objects.filter(kind="persist").count(), 3)
        self.assertFalse(DraftJob.objects.filter(kind="fetch").exists())

    @patch("core.tasks.execute_draft_job.apply_async")
    def test_publish_persist_before_fetch_and_deduplicate(self, publish):
        drafting.save_download(0, batch(next_batch=17), discover=True)
        self.assertEqual(drafting.dispatch_jobs(), 2)
        kinds = [DraftJob.objects.get(pk=call.kwargs["task_id"]).kind for call in publish.call_args_list]
        self.assertEqual(kinds, ["persist", "fetch"])
        self.assertEqual(drafting.dispatch_jobs(), 0)

    @patch("core.tasks.execute_draft_job.apply_async", side_effect=BrokerError("offline"))
    def test_broker_failure_leaves_recoverable_outbox(self, publish):
        drafting.save_download(0, batch())
        with self.assertRaises(TransientNetworkError):
            drafting.dispatch_jobs()
        self.assertEqual(DraftJob.objects.get().state, "pending")
        with patch("core.tasks.execute_draft_job.apply_async"):
            self.assertEqual(drafting.dispatch_jobs(), 1)

    @patch("core.tasks.execute_draft_job.apply_async")
    def test_expired_publish_and_execution_claims_reconcile(self, publish):
        drafting.save_download(0, batch())
        job = DraftJob.objects.get()
        for state in ("published", "running"):
            DraftJob.objects.filter(pk=job.pk).update(
                state=state, token=uuid.uuid4(), lease_until=timezone.now() - timedelta(seconds=1),
            )
            self.assertEqual(drafting.dispatch_jobs(), 1)
        self.assertEqual(publish.call_count, 2)

    def test_job_uniqueness_is_database_enforced(self):
        saved = drafting.save_download(0, batch())
        with self.assertRaises(IntegrityError), transaction.atomic():
            DraftJob.objects.create(batch=saved, kind="persist")

    def test_global_execution_attempt_limit(self):
        drafting.save_download(0, batch())
        job = DraftJob.objects.get()
        DraftJob.objects.filter(pk=job.pk).update(attempts=6)
        self.assertIsNone(drafting.claim_job(job.pk))
        self.assertEqual(DraftJob.objects.get(pk=job.pk).state, "failed")


class DraftHTTPAndFileTests(TestCase):
    @patch("core.drafting.HTTPSConnection")
    def test_real_numbered_path_timeouts_and_close(self, cls):
        response = io.BytesIO(json.dumps(batch()).encode())
        response.status = 200
        cls.return_value.getresponse.return_value = response
        self.assertEqual(drafting.fetch_json("/draft/7"), batch())
        cls.return_value.request.assert_called_once_with("GET", "/draft/7")
        self.assertEqual(cls.call_args.kwargs["timeout"], settings.DRAFT_CONNECT_TIMEOUT)
        cls.return_value.sock.settimeout.assert_called_once_with(settings.DRAFT_READ_TIMEOUT)
        cls.return_value.close.assert_called_once()

    @patch("core.drafting.HTTPSConnection")
    def test_transient_and_permanent_http_and_json_failures(self, cls):
        for status in (408, 429, 500, 503, 599, 400, 401, 404, 301):
            response = io.BytesIO(b"{}")
            response.status = status
            cls.return_value.getresponse.return_value = response
            error = TransientNetworkError if status in (408, 429) or status >= 500 else ValueError
            with self.subTest(status=status), self.assertRaises(error):
                drafting.fetch_json("/draft/0")
        response = io.BytesIO(b"bad json")
        response.status = 200
        cls.return_value.getresponse.return_value = response
        with self.assertRaises(ValueError):
            drafting.fetch_json("/draft/0")
        self.assertEqual(cls.return_value.close.call_count, 10)

    @patch("core.drafting.HTTPSConnection")
    def test_network_failures_propagate_to_retry_policy(self, cls):
        for error in (TimeoutError(), ConnectionResetError(), IncompleteRead(b"x")):
            cls.return_value.connect.side_effect = error
            with self.subTest(error=error), self.assertRaises(type(error)):
                drafting.fetch_json("/draft/0")
        cls.return_value.connect.side_effect = socket.gaierror(socket.EAI_AGAIN, "temporary")
        with self.assertRaises(TransientNetworkError):
            drafting.fetch_json("/draft/0")
        cls.return_value.connect.side_effect = socket.gaierror(socket.EAI_NONAME, "permanent")
        with self.assertRaises(socket.gaierror):
            drafting.fetch_json("/draft/0")

    def test_stage_keys_stay_on_api_origin(self):
        self.assertEqual(drafting.stage_path("next-info"), "/next-info")
        self.assertEqual(drafting.stage_path("/next-info"), "/next-info")
        for key in ("https://other.test/x", "//other/x", "../x", "a/../x", "a?b=1", "a#b",
                    "a\\b", "a%2fb", "a\nb", " "):
            with self.subTest(key=key), self.assertRaises(ValueError):
                drafting.stage_path(key)

    @patch("core.drafting.fetch_json")
    def test_sync_file_import_never_uses_http_or_broker(self, fetch):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "saved.json"
            path.write_text(json.dumps(batch()))
            for _ in range(2):
                call_command("import_draft", file=str(path), batch_id=0, sync=True, stdout=io.StringIO())
        self.assertEqual(Person.objects.count(), 1)
        self.assertFalse(DraftJob.objects.filter(kind="fetch").exists())
        fetch.assert_not_called()

    def test_file_errors_do_not_create_batches(self):
        with self.assertRaises(CommandError):
            call_command("import_draft", file="does-not-exist.json", batch_id=0, sync=True)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            for content in ("{", json.dumps(batch(person(health=None)))):
                path.write_text(content)
                with self.assertRaises(CommandError):
                    call_command("import_draft", file=str(path), batch_id=0, sync=True)
        self.assertFalse(DraftBatch.objects.exists())

    def test_status_never_claims_incomplete_pipeline_complete(self):
        output = io.StringIO()
        call_command("draft", status=True, stdout=output)
        self.assertFalse(json.loads(output.getvalue())["complete"])
