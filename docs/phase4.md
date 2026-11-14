# Phase 4: parallel fight replay

The real `fight-0.json` was imported and live `/fight/{batch_id}` discovery was
started. Every fight gets a separate Celery call on the existing Linux prefork
worker. Batches schedule fights immediately after their transaction commits.
The organizer says `next`; the actual saved/live responses use `next_batch`.
Both are accepted, conflicting pointers rejected, and missing/null/invalid or
cyclic pointers stop discovery. No batch count or final batch is assumed.

Health is **persistent across fights**. Each entire fight runs in one SQLite
`IMMEDIATE` write transaction. Worker processes execute concurrently; SQLite
serializes writer transactions, including overlapping soldiers. Commit order
defines the cross-fight order and intermediate health. Final health equals
initial health minus all completed attack damage. Every supplied attack is
applied in its original order, including below zero: there is no confirmed
death/clamping/winner rule. Health storage is now signed 64-bit. Draft health
remains separate and drafting retries cannot reset combat health.

Fight IDs are preserved. `(fight, position)` is unique; identical attacks at
different positions remain separate events. Initial import saves every event;
replay saves its defender's remaining health. Health, event results and the
fight completion marker commit together. Duplicate/retried deliveries skip a
completed fight. A failed transaction rolls back all its damage and results.
Conflicting immutable fight/batch replacements are rejected.

## Run

```powershell
Set-Location D:\CodeRun\backend
$env:Path = 'C:\Program Files\Docker\Docker\resources\bin;' + $env:Path
.\.venv\Scripts\python.exe manage.py migrate --noinput
docker compose up -d --build worker
docker compose restart worker

# Local saved batch: imports events and starts one task per fight.
.\.venv\Scripts\python.exe manage.py import_fights --file fight-0.json --batch-id 0
# Live mode: starts at /fight/0, reuses saved inputs, follows returned pointers.
.\.venv\Scripts\python.exe manage.py import_fights
# Recovery after broker/process interruption; completed fights remain unchanged.
.\.venv\Scripts\python.exe manage.py import_fights --resume

.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8000
# Open http://127.0.0.1:8000/simulate
```

`/simulate` is read-only: it shows fight participants, pending/completed/failed
status, start/end health and every original attack with remaining health.
Refresh for new background results. Pending includes queued/executing work
until the atomic replay commits. No winner is invented.

```powershell
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py makemigrations --check --dry-run
.\.venv\Scripts\python.exe manage.py test core.test_fighting core.test_simulate --noinput
.\.venv\Scripts\python.exe manage.py shell -c "from core.models import Fight,FightEvent,FightBatch; from django.db.models import Count; print(list(Fight.objects.values('status').annotate(count=Count('id')))); print('Events:',FightEvent.objects.count()); print(list(FightBatch.objects.values('id','next_id','navigation_issue')))"
docker compose logs --tail 30 worker
```

Verification: migration `0003` applied; Django checks and migration consistency
passed; eight targeted tests passed (0.345s), covering replay, ordered/identical
events, signed health, rollback/retry, idempotency, navigation, overlapping
fights on real file-backed SQLite, and HTML. Worker rebuilt/restarted; real
Redis/Linux execution showed overlapping tasks on prefork PIDs 9 and 10.
At the first verification snapshot 180 fights had completed. All 100 soldiers'
health matched initial health minus completed event damage; real duplicate
fight 0 delivery returned `applied: false`. Actual HTTP returned 200 with saved
fight results and attack histories. Live discovery continues until its returned
pointer stops; use the status command above for current counts.

Publication is recoverable with `--resume`; there is no atomic SQLite/Redis
transaction. This minimum implementation does not add a second outbox system.
HTTP tasks reuse Phase 2.5's bounded exponential retries and jitter. SQLite
BUSY/LOCKED replay failures also use that policy. Other failures are surfaced.
No independent fight health pools, random damage, fabricated events or automatic
commits/pushes are used.

Changed for Phase 4: `core/models.py`, migration `0003`, `core/fighting.py`,
`core/tasks.py`, `core/management/commands/import_fights.py`, `core/views.py`,
`templates/core/simulate.html`, `core/test_fighting.py`, `core/test_simulate.py`,
`README.md`, `docs/phase3.md`, and this guide. The existing local `fight-0.json`
was used without changing its contents.
