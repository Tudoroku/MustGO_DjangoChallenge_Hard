# Phase 3: concurrent soldier drafting

Historical phase report. [Phase 4](phase4.md) now imports/replays actual fights
and extends `/simulate` from a status page to saved fight results.

The existing Django project, Celery app, Redis services, Linux prefork worker
and `RetryingNetworkTask` are reused. There is no PostgreSQL migration, fight
simulation, commit or push.

## Contract and open questions

The verified `GET /draft/0` response is checked in as
[`core/fixtures/draft-0.json`](../core/fixtures/draft-0.json): ten people with
`id`, `name`, `age`, `rank`, `health`, integer `status: 200`, and `next_batch: 1`.
The organizer confirms more batches. The live run subsequently discovered
batches 0 through 9 by following pointers; batch 9 returned `next_batch: null`.
All 100 people were persisted and all 21 jobs succeeded. This observed stopping
response is saved as [`draft-9.json`](../api_evidence/draft-9.json); it is not
treated as a verified completion contract. Discovery requests only `/draft/{batch_id}`
using the preceding payload's strict, nonnegative integer pointer. It does not
infer counts, consecutive IDs, batch sizes or a final ID.

There is **no verified termination signal**. Missing, null, invalid and cyclic
pointers stop discovery with a persisted `navigation_issue`; valid records are
still persisted. Empty arrays and unverified `complete`/`last_batch` properties
never imply completion. Status always reports `complete: false`. Successful
jobs describe only the work discovered so far. A future completion implementation
must validate an organizer-confirmed termination signal and require every
scheduled job to succeed. HTTP 404 is a permanent fetch failure, not an end marker.

Application validation requires complete person records, string optional keys,
nonblank string ranks (no rank choices), strict integer IDs using the existing
0–99 Person identity constraint, and nonnegative 32-bit integer ages/health
matching the Django field storage policy. The age/health bound is a local
validation policy, not a claimed API maximum; health is not capped at 100.
Duplicate IDs within a batch are rejected. Unknown extra properties are retained
in the immutable source payload but not applied to Person fields. Missing people
are not deleted; missing fields are not filled with guessed defaults.

The importer supports one authoritative draft dataset keyed by upstream batch
ID. Different content for an existing batch is rejected. A person appearing in
different batch IDs is rejected transactionally, even if the attributes match;
this avoids arrival-order-dependent updates until overlap/revision semantics
are confirmed. Changing `DRAFT_API_BASE_URL` does not create a new namespace.
Use a separate database for a different dataset.

`draft_health` stores initial drafting input. `health` is initialized with an
atomic `WHERE health IS NULL` update and is otherwise preserved. Draft tasks
update only supplied fields and never clear an omitted key. These rules protect
future combat state without choosing global versus per-fight replay semantics.
The unchanged Phase 1 `import_people` command retains its original key behavior;
these new preservation guarantees apply to drafting.

When a drafted Hellstorm Fireburn record supplies a nonempty key, its updated
value is saved and a separate instruction job follows that supplied route on
the configured API origin. Absolute URLs, queries, fragments, traversal,
encoded paths and control characters are rejected. The JSON instructions are
stored in `stage_info` and shown by `draft --status`. This performs a read-only
GET and does not implement the stage's instructions or follow further routes.
An omitted key preserves the existing value and does not re-fetch an old route.
Use `draft --status` for the current live Hellstorm route and downloaded
instructions. After the live run, Person 99 is a General with health 100 and
latest key `fight-info`. That route was fetched successfully. Its current
instructions identify `/fight/0` as the starting fight-data endpoint and say to
follow `next` for subsequent batches, save every event, process each fight in
parallel, and display results at `/simulate`. They also point to
`/simulate-fight-info`. The actual response is preserved in
[`fight-info-phase3.json`](../api_evidence/fight-info-phase3.json).
Fight data import and replay remain unimplemented; no fight payload was invented.

## Scheduling, retries and transactions

`DraftBatch` stores immutable downloaded inputs, pointer issues and processing
timestamps. `DraftJob` provides a durable outbox plus database-enforced unique
`(batch, kind)` identities. Each fetch, persistence and instruction stage uses
its own Celery call; tasks never wait for child results.

The fetch task validates the complete batch outside the write transaction,
then stores it together with its persistence job and discovered successor job.
The dispatcher publishes persistence first, immediately after commit. The next
fetch can run while the preceding persistence task runs. Discovery remains
sequential because pointers are learned from responses; SQLite serializes the
short database write transactions while HTTP and task execution can overlap.

Each execution claims a job transactionally with a UUID fencing token and a
ten-minute lease. Duplicate deliveries cannot claim a live execution. An expired
worker cannot commit after a newer token takes ownership. Person changes and
the persistence completion marker commit together. Initial draft provenance
rejects overlapping batch updates. Late acknowledgements reuse the Phase 2.5
policy. Broker delivery is at least once; task IDs alone do not deduplicate it.

The existing policy permits five retries (six attempts) with exponential
full-jitter ceilings of 2, 4, 8, 16 and 32 seconds (cap 60). HTTP 408/429/5xx,
timeouts, connection failures, interrupted reads, temporary DNS/network errors
and SQLite BUSY/LOCKED contention can retry. Permanent HTTP errors, invalid
JSON/records, integrity errors and permanent DNS failures are not retried.
Each HTTP attempt has a 10-second connect and 120-second read timeout and closes
its connection. A per-job six-execution bound also protects reconciliation
from indefinite retry loops. `--retry-failed` explicitly resets that bound.

Publication happens after commit. A process/broker crash in the publication
window leaves recoverable pending work. Expired published/running jobs can be
republished. There is no atomic SQLite+Redis transaction: an ambiguous publish
can produce duplicate messages, whose effects are protected by claims and
completion markers. Recovery is an explicit `draft --resume` operation, not a
new Celery Beat deployment. Schedule that command operationally if unattended
recovery is needed. Ten minutes must elapse before reclaiming unacknowledged
published/running work. Inspect `jobs[].error` and correct permanent failures
before resetting them.

## Shared persistent SQLite

Both native Django and the worker retain `BASE_DIR/db.sqlite3`. Compose mounts
the **same repository directory writable** at `/app`; `/app/db.sqlite3` is the
Windows repository's database, including its journal/lock files. No SQLite file
is copied into the worker image or stored only in its container layer. The
Docker build context excludes application/test databases and journals. The
worker's user must be able to write the repository mount.

SQLite uses `transaction_mode: IMMEDIATE` with a 20-second busy timeout, so a
write transaction reserves the writer before reading. This avoids relying on
SQLite's ineffective `select_for_update()` and avoids read-to-write lock
upgrades. SQLite still has one writer. The design has been tested on this local
Docker Desktop bind mount; this is not a multi-host/NFS guarantee. Future combat
concurrency and health semantics need their own design. The test database is
the reserved file `test-draft.sqlite3`, separate from the application database,
so the worker integration tests use independent real file-backed connections.

## Exact PowerShell verification

Run from the existing repository. Docker Desktop must use Linux containers.
Docker's bin directory is added for both the CLI and its credential helper.

```powershell
Set-Location D:\CodeRun\backend
$env:Path = 'C:\Program Files\Docker\Docker\resources\bin;' + $env:Path
if (!(Test-Path .env)) { Copy-Item .env.example .env }

.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py test --noinput
.\.venv\Scripts\python.exe -m pip check

docker compose up -d --build worker
docker compose exec redis redis-cli ping
docker compose ps
docker compose logs --tail 40 worker
.\.venv\Scripts\python.exe manage.py celery_smoke
```

Verify actual drafting persistence via Redis and the Docker worker with the
confirmed saved first batch. This intentionally updates the application's ten
corresponding people; the command does not discover/fetch later batches.

```powershell
.\.venv\Scripts\python.exe manage.py import_draft --file core/fixtures/draft-0.json --batch-id 0
.\.venv\Scripts\python.exe manage.py shell -c "from core.models import DraftJob,Person; from backend import celery_app; job=DraftJob.objects.get(batch_id=0,kind='persist'); print(celery_app.AsyncResult(str(job.id)).get(timeout=30)); job.refresh_from_db(); assert job.state=='succeeded'; assert Person.objects.filter(draft_batch_id=0).count()==10; print('Shared worker database verified')"
.\.venv\Scripts\python.exe manage.py draft --status
```

The result backend expires after one hour. If the preceding batch was already
processed long ago, re-importing deliberately does not publish a new task. Use
the following real duplicate-delivery check, which also waits for its own fresh
result and verifies every Person field remains unchanged:

```powershell
.\.venv\Scripts\python.exe manage.py shell -c "from core.models import DraftJob,Person; from core.tasks import execute_draft_job; job=DraftJob.objects.get(batch_id=0,kind='persist'); before=list(Person.objects.order_by('id').values()); print(execute_draft_job.apply_async(args=[str(job.id)]).get(timeout=30)); assert before==list(Person.objects.order_by('id').values()); print('Duplicate delivery preserved all people')"
```

For local work without a broker, import the same saved JSON synchronously.
This creates durable provenance/jobs but performs no HTTP or broker calls;
any instruction job remains pending until asynchronous work is explicitly run.

```powershell
.\.venv\Scripts\python.exe manage.py import_draft --file core/fixtures/draft-0.json --batch-id 0 --sync
```

To start actual live discovery, inspect status and recover interrupted jobs:

```powershell
.\.venv\Scripts\python.exe manage.py draft
.\.venv\Scripts\python.exe manage.py draft --status
.\.venv\Scripts\python.exe manage.py draft --resume
# Only after investigating/correcting a failure:
.\.venv\Scripts\python.exe manage.py draft --resume --retry-failed
```

Start always resumes at numbered batch 0 and can reuse its saved response.
It does not guess a final batch. To inspect the next-stage response after an
updated Hellstorm key is processed, read `batches[].stage_info` in status.
Restart the worker after Python changes; rebuild it after dependency changes.

## Verification performed on 2026-11-14

- Applied migration `0002` to the existing application SQLite database and
  compared every original `(id, name, age, key)` before/after: all 100 preserved;
  newly added fields were null.
- Full Django suite: 77 tests passed (final run: 73.753 seconds), including the
  existing 37 tests and 40 drafting/migration/worker tests. Django checks,
  migration consistency/application and `pip check` passed.
- Deadline follow-up: a minimal `/simulate` HTML status page was added. Final
  targeted suite `core.test_drafting core.test_simulate`: 33 tests passed in
  0.293 seconds. Actual local HTTP returned 200 with the expected HTML. This
  page displays saved soldiers/health; it does not implement fight replay.
- Drafting tests cover complete validation, the actual ten-person fixture,
  missing data, pointer navigation/cycles, outbox uniqueness/recovery,
  idempotency, stale leases, targeted updates, omitted keys, health preservation,
  cross-batch conflicts, local files, and transactional rollback.
- Seven tests run a real non-eager Celery consumer with four worker threads,
  memory transport, real localhost HTTP and file-backed SQLite. They verify
  persistence during a later blocked download, real header timeout/503 retries,
  six-attempt exhaustion, malformed responses, concurrent duplicate deliveries,
  a database-trigger rollback followed by retry, cyclic discovery and updated
  Hellstorm instruction retrieval. Only jitter selection in the exhaustion test
  is fixed to zero; actual tasks/HTTP are not mocked. Existing retry tests check
  the exact exponential ceilings and jitter.
- Built/recreated the real Docker worker, then ran the existing Windows-to-Redis
  `celery_smoke`: two overlapping Linux prefork processes succeeded.
- A second real Redis/Docker delivery of the completed batch job was skipped;
  comparison of every Person field confirmed all 100 people were unchanged.
- Enqueued confirmed saved batch 0 from native Django through Redis. The Docker
  worker persisted ten people in one attempt. Native Django observed the same
  job completion and database writes: 100 total people, ten drafted, Hellstorm's
  existing `fight-info` key preserved. Live pointer-driven discovery was then
  started through the same worker. It discovered batches 0–9, stopped on the
  actual null pointer, persisted all 100 people and completed all 21 jobs.
- The live `/simulate` endpoint was checked again after drafting: HTTP 200,
  100 saved soldiers displayed, and the fight-replay limitation visible.

Official behavior references: [Celery tasks, retries and acknowledgements](https://docs.celeryq.dev/en/stable/userguide/tasks.html),
[Django SQLite transactions and limitations](https://docs.djangoproject.com/en/6.0/ref/databases/#sqlite-notes).

## Changed files

- `.dockerignore`, `.gitignore`: exclude test database/journal artifacts.
- `.env.example`: document the drafting origin.
- `backend/settings.py`: reuse SQLite with IMMEDIATE transactions and a separate
  file-backed test database; add drafting origin/timeouts.
- `compose.yaml`: make the existing repository/database bind mount writable.
- `core/models.py`: nullable rank/health, initial health/provenance, batches/jobs.
- `core/migrations/0002_draftbatch_person_draft_health_person_health_and_more.py`:
  additive drafting schema preserving existing people.
- `core/drafting.py`: validation, HTTP adapter, transactions, navigation, leases,
  outbox and status.
- `core/tasks.py`: reuse the existing task policy for drafting stages.
- `core/management/commands/draft.py`: start/status/recovery commands.
- `core/management/commands/import_draft.py`: saved batch import.
- `core/fixtures/draft-0.json`: confirmed first response.
- `core/test_celery.py`: keep test-only probe tasks out of shared global task
  registration, preventing collision warnings in later worker tests.
- `core/test_drafting.py`, `core/test_drafting_migrations.py`,
  `core/test_drafting_worker.py`: new validation/integrity/migration/worker tests.
- `backend/urls.py`, `core/views.py`, `templates/core/simulate.html`: HTML status
  page at `/simulate` and `/simulate/`; no fight replay.
- `core/test_simulate.py`: basic status-page integration check.
- `api_evidence/draft-9.json`, `api_evidence/fight-info-phase3.json`: actual live
  stopping response and updated next-stage instructions.
- `README.md`, `docs/phase2.5.md`, `docs/phase3.md`: current usage, historical
  clarification, implementation decisions and verification.

`db.sqlite3` is ignored by Git and was intentionally migrated and updated with
the complete discovered set of 100 drafted people. Existing saved root JSON
responses are not changed. Django is running at http://127.0.0.1:8000/simulate;
the existing Redis and Docker worker services remain running.
