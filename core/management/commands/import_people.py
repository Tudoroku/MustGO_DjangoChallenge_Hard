import json
import math
import time
from http.client import HTTPException, HTTPSConnection
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, transaction

from core.models import Person


PEOPLE_URL = (
    "https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net/people"
)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 120
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = 1


def fetch_people(connect_timeout, read_timeout):
    """Retry the complete GET, including response headers and body reads."""
    url = urlsplit(PEOPLE_URL)
    for attempt in range(MAX_ATTEMPTS):
        connection = HTTPSConnection(url.hostname, port=url.port, timeout=connect_timeout)
        try:
            connection.connect()
            # Switch before requesting: waiting for headers also uses the read timeout.
            connection.sock.settimeout(read_timeout)
            connection.request("GET", url.path)
            with connection.getresponse() as response:
                if 500 <= response.status <= 599:
                    error = f"HTTP {response.status}: {response.reason}"
                elif not 200 <= response.status <= 299:
                    raise CommandError(
                        f"Could not fetch people from the API: HTTP {response.status}: "
                        f"{response.reason}"
                    )
                else:
                    try:
                        return json.load(response)
                    except (ValueError, UnicodeError) as exc:
                        raise CommandError("Invalid API response: could not decode JSON.") from exc
        except TimeoutError as exc:
            error = str(exc)
        except (OSError, HTTPException) as exc:
            raise CommandError(f"Could not fetch people from the API: {exc}") from exc
        finally:
            connection.close()

        if attempt == MAX_ATTEMPTS - 1:
            raise CommandError(
                f"Could not fetch people from the API after {MAX_ATTEMPTS} attempts: {error}"
            )
        time.sleep(BACKOFF_SECONDS * 2**attempt)


def load_people_file(path):
    try:
        with open(path, "rb") as source:
            return json.load(source)
    except OSError as exc:
        raise CommandError(f"Could not read people file '{path}': {exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise CommandError("Invalid people file: could not decode JSON.") from exc


def validate_people(payload):
    """Validate the complete response before any database changes."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise CommandError("Invalid API response: expected an object with a data array.")

    people = []
    seen_ids = set()
    for index, record in enumerate(payload["data"]):
        prefix = f"Invalid person at data[{index}]"
        if not isinstance(record, dict):
            raise CommandError(f"{prefix}: expected an object.")
        person_id = record.get("id")
        if type(person_id) is not int or not 0 <= person_id <= 99:
            raise CommandError(f"{prefix}: id must be an integer between 0 and 99.")
        if person_id in seen_ids:
            raise CommandError(f"{prefix}: duplicate id {person_id} in response.")
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            raise CommandError(f"{prefix}: name must be a nonempty string.")
        age = record.get("age")
        if type(age) is not int or age < 0:
            raise CommandError(f"{prefix}: age must be a nonnegative integer.")
        key = record.get("key", "")
        if not isinstance(key, str):
            raise CommandError(f"{prefix}: key must be a string when provided.")

        person = Person(id=person_id, name=name, age=age, key=key)
        try:
            # Existing IDs are valid for an upsert. Check field values here.
            person.full_clean(validate_unique=False, validate_constraints=False)
        except ValidationError as exc:
            raise CommandError(f"{prefix}: {exc}") from exc
        people.append(person)
        seen_ids.add(person_id)
    return people


class Command(BaseCommand):
    help = "Import people from the CodeRun API, updating existing external IDs."

    def add_arguments(self, parser):
        parser.add_argument("--file", help="Import a downloaded JSON response without HTTP.")
        parser.add_argument("--connect-timeout", type=float, default=CONNECT_TIMEOUT,
                            help="Connection timeout in seconds (default: 10).")
        parser.add_argument("--read-timeout", type=float, default=READ_TIMEOUT,
                            help="Read timeout in seconds (default: 120).")

    def handle(self, *args, **options):
        if options["file"] is not None:
            payload = load_people_file(options["file"])
        else:
            for name in ("connect_timeout", "read_timeout"):
                value = options[name]
                if not math.isfinite(value) or value <= 0:
                    raise CommandError(f"--{name.replace('_', '-')} must be a positive finite number.")
            payload = fetch_people(options["connect_timeout"], options["read_timeout"])

        people = validate_people(payload)
        created_count = 0
        try:
            with transaction.atomic():
                for person in people:
                    _, created = Person.objects.update_or_create(
                        id=person.id,
                        defaults={
                            "name": person.name,
                            "age": person.age,
                            "key": person.key,
                        },
                    )
                    created_count += int(created)
        except DatabaseError as exc:
            raise CommandError("Could not save people; the import was rolled back.") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Imported {len(people)} people: {created_count} created, "
                f"{len(people) - created_count} updated."
            )
        )
