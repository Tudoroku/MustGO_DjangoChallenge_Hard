# Phase 2.5: Celery infrastructure

Historical phase report. [Phase 3](phase3.md) now implements drafting, adds a
writable shared database mount, and verifies real Redis/Linux worker execution.
The read-only mount and unavailable-Docker notes below describe Phase 2.5 only.

This phase adds only background processing infrastructure. The newly supplied
`GET /draft-info` requirements establish that each downloaded drafting batch
will eventually be a separate task, may start immediately after download, and
will add ranks after a database migration. `/draft` currently returns 404; no
request to it is made by this implementation. There is no drafting/fight task,
rank field, rank type, new application table, migration, or guessed response
contract. Hellstorm Fireburn's existing key is preserved by the Person import.

## Components and configuration

- `backend/celery.py` defines the Django Celery app, loads `CELERY_*` settings,
  and discovers `tasks.py` modules; `backend/__init__.py` loads the app.
- Redis database 0 is the broker and database 1 stores short-lived smoke results
  (one hour). Both use the same Redis service; no database result-backend app,
  scheduler, monitoring service, or PostgreSQL service is introduced.
- The worker runs Python 3.13 on Linux with explicit `--pool=prefork` and two
  child processes by default. `CELERY_WORKER_CONCURRENCY` changes the Compose
  command; keep it at least 2 for the parallel smoke check.
- The Windows project directory is bind-mounted at `/app`, so code edits are
  available in the container. Restart the worker after editing Python code;
  rebuild after dependency changes. The worker has its own Linux dependencies
  installed in its image; it does not execute the Windows `.venv`.
- The mount is read-only because the smoke task needs no database writes.
  Future database-writing tasks require an explicit writable data arrangement
  or a shared database server; this phase intentionally does not enable them.
- JSON is the only accepted task/result format. Eager execution is disabled.
  Prefetch is one message per execution slot for fairer parallel scheduling.
- Redis is published only on host loopback. Its AOF data lives in a named
  volume, with `noeviction` to avoid silently evicting broker keys. This is a
  local development setup, not a production availability/durability guarantee.

Settings load the project `.env` using python-dotenv without overriding existing
process environment variables. `.env.example` contains only development values;
`.env` and other `.env.*` files are ignored by Git and Docker build context.
The old checked-in Django development key is replaced by an environment setting
with a public development fallback. Use a private random key, disable debug,
and set appropriate hosts for any deployment. The worker reads `.env` from the
local bind mount; those values are not baked into the image.

Compose overrides broker/result URLs to `redis://redis:6379/0` and `/1`; Windows
Django uses `127.0.0.1`. `localhost` inside the worker would mean the worker
container itself. If you change `REDIS_PORT`, also change the port in both host
URLs in `.env`; the container URLs remain port 6379.

## Exact PowerShell commands

Prerequisites: Python 3.13+, Docker Desktop running **Linux containers**, and
Docker Compose v2. Allow Docker Desktop access to `D:\CodeRun\backend` if asked.
The existing virtual environment can be reused.

Run from the project directory (copy `.env.example` only if `.env` is absent):

```powershell
Set-Location D:\CodeRun\backend
if (!(Test-Path .env)) { Copy-Item .env.example .env }
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# Start Redis, then build/start the Linux prefork worker.
docker compose up -d redis
docker compose up -d --build worker

# Redis connectivity, service status, worker startup and task registration.
docker compose exec redis redis-cli ping
docker compose ps
docker compose logs --tail 100 worker

# Submit from Windows, wait for real worker results, and verify actual overlap.
.\.venv\Scripts\python.exe manage.py celery_smoke

# Start Django on Windows (run in another PowerShell terminal if desired).
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
```

For a fresh database, run `manage.py migrate` before using Django's database
features. Phase 2.5 needs no new migrations and the smoke command never touches
the application database. The existing `import_people` command and its options
continue to work as documented in the README.

The smoke command checks the broker connection, publishes two distinct tasks
with a two-second pause, waits up to 30 seconds per result, verifies task IDs
and tokens, requires distinct `(hostname, pid)` pairs and overlapping execution
intervals, then prints both results and a success message. Overlap uses wall
clocks within the single Compose worker container. Use only that worker for
this local check. A worker with one execution slot fails the parallelism check;
no worker causes a result timeout. An eager task call cannot satisfy it.
Successful results are removed; results left after a failed check expire in an
hour. This verifies the real Redis transport and prefork execution, separately
from unit tests that run Celery's eager test path without Redis.

If the Windows producer is unavailable, run the same check inside the worker
container (this does not test Windows-to-Redis connectivity):

```powershell
docker compose exec worker python manage.py celery_smoke
```

Stop containers without removing the Redis data volume:

```powershell
docker compose down
```

## Reusable retry policy

`core.task_base.RetryingNetworkTask` is an opt-in base for idempotent tasks.
It retries Python network timeouts, connection failures, interrupted HTTP reads,
remote disconnects, Celery soft time limits, and explicit `TransientNetworkError`.
There are at most five retries (six attempts total), with exponential delay
ceilings of 2, 4, 8, 16, and 32 seconds, capped at 60 seconds if the retry count
is later extended. Full jitter chooses a delay from zero to each ceiling.

`ValidationError`, `ValueError` (including JSON decode failures), and `TypeError`
are explicitly excluded. Other errors are not retried unless they match the
transient allowlist. In particular, this does not retry every `Exception` or
`OSError`, database integrity errors, missing files, authentication failures,
or permanent HTTP 4xx failures.

Future HTTP adapters must set finite connection/read timeouts and translate
only known transient failures (for example verified retryable HTTP 5xx,
temporary DNS failures, or a library-specific connection/timeout exception)
into `TransientNetworkError`, preserving the original cause. Do not map all
`URLError`, all DNS errors, all SSL errors, or invalid payloads to it. No HTTP
adapter for `/draft` is implemented in this phase. Soft task limits are caught
by this policy if configured; hard process-killing time limits are not normal
retryable Python exceptions. No time limits are guessed for future workloads.

## Idempotency requirements for future batches (no schema chosen)

The smoke task has no database writes and is safe to repeat. Celery provides
at-least-once delivery, not exactly-once database effects. The retry base uses
late acknowledgements only for tasks whose work is safe to repeat; it is not
a replacement for database constraints or transaction design. Worker-loss
redelivery is not forced, avoiding unbounded crash/requeue loops. Explicit
exception retries and broker redelivery are distinct mechanisms.

Once the real drafting contract is known:

1. Establish a stable batch identity and person identity from evidence. Use a
   database uniqueness constraint for deduplication, not Celery task IDs, an
   in-memory set, or a race-prone existence check. If the API has no stable batch
   identity, durably record a local identity for the downloaded payload before
   enqueueing; do not invent upstream fields or assume hashes define semantics.
2. Validate the entire downloaded batch before writes. Download/network retries
   stay outside write transactions. Never blindly repeat a side-effecting HTTP
   operation unless its own idempotency contract makes that safe.
3. Within one short `transaction.atomic()` block, claim/lock the stable batch
   identity, skip already completed work, apply deterministic updates/upserts
   using verified unique identities, and mark completion in the same transaction.
   Concurrent duplicate delivery must converge through constraints/locking;
   rollback must leave no partial writes or completion marker. Do not increment
   or append effects blindly on every attempt.
4. Publish a task for each downloaded batch immediately after its durable data
   commits, using `transaction.on_commit()` or Celery `delay_on_commit()` via
   `DjangoTask`. If guaranteed delivery across a commit/publish crash window is
   required, add an outbox/reconciliation mechanism at that point. The current
   base inherits `Task`, so use `transaction.on_commit()` with it directly.
5. Before enabling drafting, test retry after rollback, duplicate submission,
   concurrent duplicates, and crash after commit before acknowledgement. Assert
   exactly one set of database effects. These require the actual schema and
   are intentionally not fabricated here.

This pattern allows the first downloaded batch to process while subsequent
batches download, without waiting for the entire dataset. No orchestration or
batch schema is implemented yet.

## SQLite now; PostgreSQL before concurrent simulation writes

The existing `DATABASES` setting remains SQLite with the existing `db.sqlite3`.
SQLite is sufficient for this smoke test because tasks only return process
metadata through Redis. Existing transactional Person import tests remain valid.
This does not establish that concurrent database-writing tasks are safe.

SQLite serializes writes, can raise `database is locked` under concurrent load,
and Django's `select_for_update()` has no effect on it. A Windows/Linux bind
mount adds filesystem locking concerns; increasing lock timeouts does not add
row locking or remove write serialization. Eventual fights that update shared
soldier/health state will need transactional conflict control and likely
PostgreSQL row locks (or carefully designed conditional updates/version checks).
PostgreSQL also permits independent rows to be updated concurrently. The exact
simulation isolation strategy remains dependent on the unconfirmed contracts.

A deliberate PostgreSQL transition would require adding a supported PostgreSQL
service/database and Psycopg dependency, environment-based Django database host,
port, name and credentials for both Windows and Linux, applying migrations,
copying/validating existing Person records (including ID 0 and keys), and testing
transactions, uniqueness, locking, deadlock handling and concurrent retries
against PostgreSQL. Native Windows clients would use a published host port;
containers would use the database service hostname. Back up SQLite before any
data transfer and compare counts/values. None of these changes is made here.

## Verification

Executed on 2026-11-14 with the existing Windows virtual environment:

- Django system checks: passed.
- Migration consistency check: passed, no changes detected.
- Full automated suite: 37 tests passed (26 existing import tests + 11 Celery tests).
- Celery tests separately: 11 passed.
- Installed dependency consistency (`pip check`): passed.

Docker CLI was unavailable in this environment (not on PATH and absent from the
standard Docker Desktop CLI location). Container build/startup, Redis PING, and
the real-worker `celery_smoke` command were **not executed** and require local
verification with Docker Desktop. No eager test is counted as a live worker test.

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test
# Run only the Celery tests if desired:
.\.venv\Scripts\python.exe manage.py test core.test_celery
```

`core.test_celery` tests configuration, discovery, environment overrides,
database-free smoke execution and input validation, and actual Celery autoretry
wrapping with controlled failures, bounded retries, exponential delays, jitter,
and permanent-error exclusion. These are broker-free unit tests; they do not
claim a live Redis/worker verification. Run the separate Docker smoke commands
above to verify container build/startup, actual Redis connectivity, result
transport, and overlapping execution on two Linux worker processes.

Official references: [Django integration](https://docs.celeryq.dev/en/v5.7.0/django/first-steps-with-django.html),
[Celery tasks, retries and acknowledgements](https://docs.celeryq.dev/en/v5.7.0/userguide/tasks.html),
[prefork concurrency](https://docs.celeryq.dev/en/v5.7.0/userguide/concurrency/index.html),
and [Django SQLite limitations](https://docs.djangoproject.com/en/6.1/ref/databases/#sqlite-notes).
