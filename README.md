# CodeRun 2026 — Django War Simulator HARD

Phase 4 is runnable: [fight import, parallel replay, health semantics and commands](docs/phase4.md).
`/simulate` now shows actual fight results and ordered attack histories. The
phase reports below describe their respective implementation stages.

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

## Phase 2 investigation

See [the Phase 2 architecture and API findings](docs/phase2.md) for confirmed
contracts, the proposed relational design, import/integrity requirements, health
semantics, and the Phase 3 plan. The existing Person model, migration, and import
command are unchanged. Soldier/fight schemas and data sources have not been
provided by the discoverable contracts, so their models and importers are
deferred. Phase 2 introduced no simulation, Celery, or Redis.

## Phase 2.5: Celery infrastructure

See [the Celery setup and verification guide](docs/phase2.5.md) for the Linux
prefork worker, Redis, exact PowerShell startup commands, real background smoke
check, retry policy, future batch idempotency requirements, and database limits.
The existing Person import remains unchanged. Phase 3 extends drafting using
this infrastructure; the Phase 2.5 guide records the earlier setup.

## Phase 3: concurrent soldier drafting

See [the Phase 3 implementation and verification guide](docs/phase3.md) for
numbered batch discovery, immediate background persistence, retries, shared
SQLite storage, saved-file imports, recovery and exact PowerShell commands.
The confirmed first batch has ten records and points to batch 1. No final batch
or termination signal is assumed. `draft --status` therefore reports
`complete: false`, even after all currently scheduled work succeeds.

```powershell
.\.venv\Scripts\python.exe manage.py migrate
docker compose up -d --build worker
.\.venv\Scripts\python.exe manage.py draft
.\.venv\Scripts\python.exe manage.py draft --status
```

`Person.rank`, `health`, `draft_health` and drafting provenance are nullable for
existing people. Drafting records initial health separately and initializes
mutable health only when it is null. It preserves omitted keys, rejects
conflicting batch replacements and overlapping people across different batches,
and never deletes absent people. Updated Hellstorm route keys trigger one
same-origin background instruction download. Fight simulation remains deferred.

`/simulate` (also `/simulate/`) serves an HTML status page with the drafted
soldiers and saved initial/current health. It explicitly reports that fight
replay is unavailable; no fight events or results are fabricated. Start Django
with `.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000` and open
http://127.0.0.1:8000/simulate.

For a broker-free saved-file import (does not follow pointers or fetch keys):

```powershell
.\.venv\Scripts\python.exe manage.py import_draft --file core/fixtures/draft-0.json --batch-id 0 --sync
```
