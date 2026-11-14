# CodeRun 2026 — Django War Simulator HARD

Phase 1 uses the existing `backend` project and `core` app to register people.

## Setup and import

Use Python 3.13 or later. From the repository directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py import_people
```

The command fetches only `GET /people` from
`https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net`.
It uses Python's standard HTTP library with separate connection (10 seconds)
and read (120 seconds) timeouts. The read timeout applies to response headers
and body reads. Override either timeout with positive, finite seconds:

```powershell
python manage.py import_people --connect-timeout 10 --read-timeout 240
```

Timeouts and HTTP 5xx errors retry the complete request up to three total
attempts, with exponential backoff of 1 then 2 seconds. Other HTTP errors and
invalid JSON are not retried. Each attempt closes its connection before retrying.
Network/HTTP failures, invalid JSON, invalid records, and
database failures produce a command error and a nonzero exit status.

To import a previously downloaded response without making an HTTP request:

```powershell
python manage.py import_people --file people.json
```

The file must contain the same JSON object with a `data` array as the API response.
File imports use the same validation and transactional updates as API imports.

`Person.id` is the external integer ID (0–99), explicitly used as the primary
key, so ID 0 is preserved. The model stores `name`, nonnegative integer `age`,
and optional string `key` (empty string when absent). Hellstorm Fireburn's
`key: "fight-info"` is preserved without interpreting it or calling another
endpoint.

The response must contain a `data` array. Each record is validated, including
strict integer types for ID/age, a nonblank name, and a string key when supplied.
Duplicate IDs within one response are rejected. Unknown properties are ignored.
The command accepts subsets without requiring exactly 100 records; it never
deletes local people missing from a response. Empty arrays are a no-op.

The entire response is validated before writing. All writes run in one database
transaction. Existing IDs are updated, so rerunning the import creates no
duplicates. The reported updated count includes unchanged existing records.

## Verification

```powershell
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test
```

Tests mock HTTP responses and require no live API access. They cover ID 0,
the optional key, repeat imports and updates, response validation, network and
HTTP errors, separate timeouts, bounded retries and exponential backoff, local
file imports, invalid JSON, and transaction rollback.

Soldiers, fights, Celery, and Redis are outside Phase 1.
