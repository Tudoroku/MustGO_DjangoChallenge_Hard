import copy
import io
import json
from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import call, patch

from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase

from core.management.commands.import_people import CONNECT_TIMEOUT, READ_TIMEOUT
from core.models import Person


class ImportPeopleTests(TestCase):
    def setUp(self):
        self.payload = {
            "data": [
                {"id": 0, "name": "First person", "age": 20},
                {"id": 99, "name": "Hellstorm Fireburn", "age": 45, "key": "fight-info"},
            ]
        }
        self.https_connection = patch(
            "core.management.commands.import_people.HTTPSConnection"
        ).start()
        self.connection = self.https_connection.return_value
        self.sleep = patch("core.management.commands.import_people.time.sleep").start()
        self.addCleanup(patch.stopall)

    def run_import(self, payload=None, raw=None):
        if raw is None:
            raw = json.dumps(self.payload if payload is None else payload).encode()
        self.connection.getresponse.return_value = self.response(raw)
        output = io.StringIO()
        call_command("import_people", stdout=output)
        return output.getvalue()

    def response(self, raw=None, status=200):
        response = io.BytesIO(json.dumps(self.payload).encode() if raw is None else raw)
        response.status = status
        response.reason = "test response"
        return response

    def test_import_preserves_zero_and_optional_key(self):
        output = self.run_import()
        self.assertEqual(Person.objects.count(), 2)
        first = Person.objects.get(pk=0)
        self.assertEqual((first.name, first.age, first.key), ("First person", 20, ""))
        hellstorm = Person.objects.get(pk=99)
        self.assertEqual(hellstorm.name, "Hellstorm Fireburn")
        self.assertEqual(hellstorm.key, "fight-info")
        self.assertIn("2 created, 0 updated", output)
        self.https_connection.assert_called_once_with(
            "coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net",
            port=None, timeout=CONNECT_TIMEOUT,
        )
        self.connection.sock.settimeout.assert_called_once_with(READ_TIMEOUT)
        self.connection.request.assert_called_once_with("GET", "/people")
        self.connection.close.assert_called_once()

    def test_repeated_import_has_no_duplicates(self):
        self.run_import()
        output = self.run_import()
        self.assertEqual(Person.objects.count(), 2)
        self.assertEqual(list(Person.objects.order_by("id").values_list("id", flat=True)), [0, 99])
        self.assertIn("0 created, 2 updated", output)

    def test_full_hundred_person_response(self):
        self.run_import({"data": [
            {"id": i, "name": f"Person {i}", "age": i} for i in range(100)
        ]})
        self.assertEqual(Person.objects.count(), 100)
        self.assertTrue(Person.objects.filter(pk=0).exists())

    def test_existing_people_are_updated_and_absent_key_is_cleared(self):
        self.run_import()
        self.payload["data"][0].update(name="Updated person", age=21)
        del self.payload["data"][1]["key"]
        self.run_import()
        person = Person.objects.get(pk=0)
        self.assertEqual((person.name, person.age), ("Updated person", 21))
        self.assertEqual(Person.objects.get(pk=99).key, "")

    def test_empty_array_does_not_delete_people(self):
        self.run_import()
        self.assertIn("Imported 0 people", self.run_import({"data": []}))
        self.assertEqual(Person.objects.count(), 2)

    def test_invalid_envelopes(self):
        for payload in (None, [], {}, {"data": None}, {"data": {}}, {"data": "people"}):
            with self.subTest(payload=payload):
                with self.assertRaises(CommandError):
                    self.run_import(raw=json.dumps(payload).encode())
                self.assertEqual(Person.objects.count(), 0)

    def test_invalid_records_do_not_partially_import(self):
        invalid_records = [None, [], "person", {},
            {"id": 1, "name": "Valid name"},
            {"id": 1, "age": 20}]
        base = {"id": 1, "name": "Valid name", "age": 20}
        for field, values in (
            ("id", [-1, 100, True, 1.0, "1", None]),
            ("age", [-1, True, 20.5, "20", None, 2**64]),
            ("name", ["", "  ", 123, None]),
            ("key", [None, 123, False, []]),
        ):
            invalid_records.extend({**base, field: value} for value in values)
        for record in invalid_records:
            with self.subTest(record=record):
                with self.assertRaises(CommandError):
                    self.run_import({"data": [self.payload["data"][0], record]})
                self.assertEqual(Person.objects.count(), 0)

    def test_duplicate_ids_in_response_are_rejected(self):
        duplicate = copy.deepcopy(self.payload["data"][0])
        duplicate["name"] = "Conflicting name"
        with self.assertRaisesMessage(CommandError, "duplicate id 0"):
            self.run_import({"data": [self.payload["data"][0], duplicate]})
        self.assertEqual(Person.objects.count(), 0)

    def test_invalid_response_keeps_existing_data(self):
        Person.objects.create(id=0, name="Original", age=30)
        with self.assertRaises(CommandError):
            self.run_import({"data": [self.payload["data"][0], {"id": 1}]})
        self.assertEqual(Person.objects.get(pk=0).name, "Original")

    def test_invalid_json_and_encoding(self):
        for raw in (b"not json", b"", b"\xff"):
            with self.subTest(raw=raw):
                with self.assertRaisesMessage(CommandError, "could not decode JSON"):
                    self.run_import(raw=raw)
                self.assertEqual(Person.objects.count(), 0)

    def test_network_timeout_and_http_errors(self):
        errors = [
            TimeoutError("timed out"),
            OSError("connection reset"),
        ]
        for error in errors:
            with self.subTest(error=error):
                self.connection.connect.side_effect = error
                with self.assertRaisesMessage(CommandError, "Could not fetch people"):
                    self.run_import()
                self.assertEqual(Person.objects.count(), 0)

    def test_interrupted_response_body(self):
        self.connection.getresponse.return_value.__enter__.return_value.status = 200
        self.connection.getresponse.return_value.__enter__.return_value.read.side_effect = IncompleteRead(b"{")
        with self.assertRaisesMessage(CommandError, "Could not fetch people"):
            call_command("import_people", stdout=io.StringIO())
        self.assertEqual(Person.objects.count(), 0)

    def test_configurable_timeouts_are_applied_before_request(self):
        self.connection.getresponse.return_value = self.response()
        call_command("import_people", connect_timeout=5, read_timeout=240, stdout=io.StringIO())
        self.assertEqual(self.https_connection.call_args.kwargs["timeout"], 5)
        self.assertEqual(self.connection.mock_calls[:4], [
            call.connect(), call.sock.settimeout(240),
            call.request("GET", "/people"), call.getresponse(),
        ])

    def test_invalid_timeouts_do_not_make_requests(self):
        for name in ("connect_timeout", "read_timeout"):
            for value in (0, -1, float("nan"), float("inf")):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesMessage(CommandError, "positive finite"):
                        call_command("import_people", **{name: value})
        self.https_connection.assert_not_called()

    def test_connection_timeout_retries_then_succeeds(self):
        self.connection.connect.side_effect = [TimeoutError("connect timeout"), None]
        self.run_import()
        self.assertEqual(self.https_connection.call_count, 2)
        self.sleep.assert_called_once_with(1)
        self.assertEqual(self.connection.close.call_count, 2)
        self.assertEqual(Person.objects.count(), 2)

    def test_header_timeout_retries_then_succeeds(self):
        self.connection.getresponse.side_effect = [TimeoutError("headers timeout"), self.response()]
        call_command("import_people", stdout=io.StringIO())
        self.assertEqual(Person.objects.count(), 2)
        self.sleep.assert_called_once_with(1)

    def test_body_timeout_retries_whole_response(self):
        first = self.connection.getresponse.return_value
        first.__enter__.return_value.status = 200
        first.__enter__.return_value.read.side_effect = TimeoutError("read timeout")
        self.connection.getresponse.side_effect = [first, self.response()]
        call_command("import_people", stdout=io.StringIO())
        self.assertEqual(self.connection.request.call_count, 2)
        self.assertEqual(Person.objects.count(), 2)
        first.__exit__.assert_called_once()
        self.sleep.assert_called_once_with(1)

    def test_timeout_retries_are_bounded_with_exponential_backoff(self):
        self.connection.connect.side_effect = TimeoutError("timed out")
        with self.assertRaisesMessage(CommandError, "after 3 attempts: timed out"):
            call_command("import_people")
        self.assertEqual(self.https_connection.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(self.connection.close.call_count, 3)
        self.assertEqual(Person.objects.count(), 0)

    def test_server_errors_retry_then_succeed(self):
        self.connection.getresponse.side_effect = [
            self.response(status=500), self.response(status=503), self.response(),
        ]
        call_command("import_people", stdout=io.StringIO())
        self.assertEqual(Person.objects.count(), 2)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])

    def test_server_error_exhaustion(self):
        self.connection.getresponse.side_effect = [self.response(status=504) for _ in range(3)]
        with self.assertRaisesMessage(CommandError, "after 3 attempts: HTTP 504"):
            call_command("import_people")
        self.assertEqual(self.connection.close.call_count, 3)
        self.assertEqual(self.sleep.call_args_list, [call(1), call(2)])
        self.assertEqual(Person.objects.count(), 0)

    def test_client_error_and_invalid_json_are_not_retried(self):
        for response in (self.response(status=404), self.response(b"bad json")):
            with self.subTest(status=response.status):
                self.https_connection.reset_mock()
                self.connection.getresponse.return_value = response
                with self.assertRaises(CommandError):
                    call_command("import_people")
                self.https_connection.assert_called_once()
                self.sleep.assert_not_called()
                self.assertEqual(Person.objects.count(), 0)

    def test_file_import_is_idempotent_and_updates_without_http(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "people.json"
            path.write_text(json.dumps(self.payload), encoding="utf-8")
            call_command("import_people", "--file", str(path), stdout=io.StringIO())
            self.payload["data"][0]["name"] = "Updated locally"
            path.write_text(json.dumps(self.payload), encoding="utf-8")
            output = io.StringIO()
            call_command("import_people", file=str(path), stdout=output)
        self.assertEqual(Person.objects.count(), 2)
        self.assertEqual(Person.objects.get(pk=0).name, "Updated locally")
        self.assertEqual(Person.objects.get(pk=99).key, "fight-info")
        self.assertIn("0 created, 2 updated", output.getvalue())
        self.https_connection.assert_not_called()

    def test_file_errors_and_invalid_records_preserve_database(self):
        Person.objects.create(id=0, name="Original", age=30)
        with TemporaryDirectory() as directory:
            path = Path(directory) / "people.json"
            with self.assertRaisesMessage(CommandError, "Could not read people file"):
                call_command("import_people", file=str(path))
            for raw in (b"bad json", b"\xff", b"{}", json.dumps({"data": [
                self.payload["data"][0], {"id": 1}
            ]}).encode()):
                with self.subTest(raw=raw):
                    path.write_bytes(raw)
                    with self.assertRaises(CommandError):
                        call_command("import_people", file=str(path))
                    self.assertEqual(Person.objects.count(), 1)
                    self.assertEqual(Person.objects.get(pk=0).name, "Original")
        self.https_connection.assert_not_called()

    def test_database_failure_rolls_back_prior_writes(self):
        Person.objects.create(id=0, name="Original", age=30)
        original_upsert = Person.objects.update_or_create

        def fail_second_write(*args, **kwargs):
            if kwargs["id"] == 99:
                raise DatabaseError("simulated failure")
            return original_upsert(*args, **kwargs)

        with patch.object(Person.objects, "update_or_create", side_effect=fail_second_write):
            with self.assertRaisesMessage(CommandError, "rolled back"):
                self.run_import()
        self.assertEqual(Person.objects.count(), 1)
        self.assertEqual(Person.objects.get(pk=0).name, "Original")


class PersonTests(TestCase):
    def test_model_validation(self):
        Person(id=0, name="First person", age=0).full_clean()
        for values in (
            {"id": -1}, {"id": 100}, {"age": -1}, {"name": ""}
        ):
            with self.subTest(values=values):
                person = Person(**({"id": 0, "name": "Person", "age": 20} | values))
                with self.assertRaises(ValidationError):
                    person.full_clean()

    def test_database_rejects_out_of_range_ids_and_negative_age(self):
        for values in ({"id": -1}, {"id": 100}, {"age": -1}):
            with self.subTest(values=values):
                with self.assertRaises(IntegrityError), transaction.atomic():
                    Person.objects.create(**({"id": 0, "name": "Person", "age": 20} | values))
