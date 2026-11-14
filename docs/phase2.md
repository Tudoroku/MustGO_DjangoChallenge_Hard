# Phase 2: Soldier and fight data architecture

Status: investigation and conditional relational design complete; soldier/fight
implementation is deferred pending actual contracts. No new database tables,
migrations, commands, or simulation behavior have been introduced. This is not
a claim that soldier/fight imports are operational.

## Evidence and discovery boundary

Inspected the existing project source, settings, URLs, models, migration,
management command, all 26 existing tests, local `people.json`, and the local
database's table/migration history. There are 100 local Person records and no
soldier, fight, or event tables. The local payload has `data` and `status` keys.
The only person carrying a key is ID 99, Hellstorm Fireburn (age 24), with
`key: "fight-info"`.

API base: `https://coderun2026-csbwf4haa4d0gubu.westus3-01.azurewebsites.net`.
On 2026-11-14 UTC, fetched each of these paths once, receiving HTTP 200:

| Path | Confirmed response | Local response body |
| --- | --- | --- |
| `/fight-info` | JSON `data.message` and `status: "ok"`; prose about fights, events, persistence, parallel background processing; points to `/simulate-fight-info` | [fight-info](../api_evidence/fight-info.txt) |
| `/simulate-fight-info` | JSON `data.message` and `status: "ok"`; prose requiring our `/simulate` page, parallel background processes, defender health decreases, HTML results | [simulate-fight-info](../api_evidence/simulate-fight-info.txt) |
| `/` | JSON `message: "API is running"`, `status: "ok"`; no discovery links | [root](../api_evidence/root.txt) |

These are unmodified response bodies, not soldier/fight data fixtures. The
instruction responses are prose and do not establish the envelope or field
names of future data responses. `/people` was not requested again. Its existing
local response was used instead. No candidate drafting/data routes, OpenAPI
paths, HTTP methods, query parameters, or person-specific URLs were guessed.
The GitHub repository was not fetched: the existing local project was the
implementation under inspection.

## Confirmed requirements

- People have external IDs, names, and ages; the existing import also preserves
  the optional key. Person ID zero is valid and must remain intact.
- A fight contains two soldier IDs and a list of events.
- Each event identifies an attacker, defender, and damage to the defender.
- Every event in each fight must be persisted, preserving list order. Identical
  repeated events must remain separate occurrences.
- Eventually each fight is replayed in its own background process, with fights
  running in parallel. Defender health decreases according to events. Results
  must be HTML on our application's `/simulate` page.
- The current phase does not implement replay or workers.

## Missing contracts and decisions

The available responses do **not** specify:

1. How a person is drafted: source URL, HTTP method, body, eligibility rules,
   selection algorithm, side effects, or whether one person can have multiple
   soldier records. No age threshold or random drafting rule can be inferred.
2. Soldier fields, external ID type/range/namespace, whether soldier ID equals
   person ID, the person-reference field, or any initial health value/type/unit.
3. Fight-data source, payload envelope, field names, pagination, stability across
   requests, or whether fights have stable external IDs.
4. Event field names, external event IDs (if any), explicit sequence numbers
   (if any), damage numeric type/unit/range, or zero/negative damage semantics.
5. Whether fights can reference the same soldier twice or events can name
   soldiers outside the two fight participants. Both are suspicious inputs;
   proposed rejection policies below need confirmation against real data.
6. Whether health carries across fights, resets for each fight, or is supplied
   independently per participant per fight. No default of 100 is justified.
7. What happens after health reaches zero, whether health can go negative,
   whether later events must still replay, and how winners/draws are determined.
8. Whether imported records can be corrected, deleted, or revised, and whether
   replay must retain historical revisions/results.

Provide a documented drafting/data source or saved responses with their source
contracts. A sample can confirm observed field shapes, but cannot by itself
settle lifecycle rules such as health scope or identifier stability.

## Proposed relational design (conditional, not an API schema)

The names below are internal design names, not invented upstream field names.

| Entity | Proposed storage and relationships | Confirmation needed |
| --- | --- | --- |
| Person | Keep the existing external integer primary key, name, age, optional key and ID range constraint | No Phase 2 changes needed |
| Soldier | Local primary key; separate unique external identifier; protected reference to Person once the mapping exists | External ID type/namespace and Person mapping; use a one-to-one reference only if one soldier per person is guaranteed |
| Fight | Local primary key; separate unique external identifier if supplied; two protected Soldier references | Stable fight identity, actual participant fields, whether self-fights are valid |
| FightEvent | Local primary key; protected Fight reference; nonnegative internal position; protected attacker and defender Soldier references; exact damage value | Numeric representation and rules, actual field names, event identifiers |

Separate local keys for new entities avoid assuming soldier IDs are Person IDs.
If external IDs are source-scoped, use a unique `(source, external_id)` key;
otherwise use the verified unique external ID. Preserve identifiers losslessly:
do not coerce strings such as `"001"` to integers or use booleans as integers.
Do not change the existing Person identity strategy just to match new tables.

For FightEvent, the import assigns positions `0..n-1` from the received array,
with a database uniqueness constraint on `(fight, position)` and explicit default
ordering by position. This position is an internal occurrence identity, not a
claim that the API supplies an ordinal. If the API also supplies an event ID or
sequence, preserve it separately and validate its documented meaning. Never
deduplicate events by attacker/defender/damage or sort the array by those values.

Use foreign keys for referential integrity. Proposed deletion policy is PROTECT
for historical fight/event links and the Soldier-to-Person link; reject deletion
of referenced records rather than silently losing history. The two-participant
representation avoids introducing an unnecessary general-purpose roster model.

Subject to confirmed rules, add database checks for distinct participants,
distinct attacker/defender, and valid position/damage ranges. A Django CheckConstraint
cannot join to the parent Fight to enforce event participant membership. Validate
membership in the transactional import service (and model validation for manual
writes), and require all application writers to use that service. Direct ORM/SQL
writes can bypass application validation; stronger database enforcement would
require additional schema design or triggers, not a misleading cross-table check.
Foreign keys and uniqueness constraints alone do not guarantee membership.

No speculative health, winner, completion status, or worker fields are needed
to persist the confirmed event log. Their absence is intentional until the
semantics below are resolved.

## Health scope: unresolved, with materially different designs

**Fight-local health:** if each fight starts independently, keep Soldier identity
and baseline attributes separate from mutable replay state. Add a FightParticipant
state table (unique fight/soldier) or a result/run model with per-participant
starting and current health once its source is known. Separate runs if replay
history is required. Replaying a fight must not overwrite another fight's health.

**Global health:** if damage persists across fights, Soldier (or a one-to-one
soldier state row) owns mutable current health. Fights sharing soldiers contend
for those rows. Two processes updating a shared soldier can lose updates or
produce schedule-dependent results. Atomic arithmetic alone does not define
correct cross-fight event order, death rules, or deterministic outcomes.
The contract must define cross-fight ordering/serialization or explicitly permit
schedule-dependent outcomes. Lock multiple shared rows in a stable order if the
chosen database supports the necessary row locks.

Neither interpretation is confirmed by the wording about decreasing defender
health and watching for concurrency issues. Do not choose either silently. Even
event-log storage can proceed once schemas are known, while health/replay work
remains deferred until this decision is supplied.

## Import design once real schemas are supplied

Keep the Person command unchanged. Add separate soldier/fight import services
and commands for the actual contracts, with `--file` accepting saved JSON in the
same upstream shape and using the same validator as any eventual HTTP path.
No live fetch should occur during file imports or tests. Until a source is
documented, do not invent an HTTP mode or a pseudo-API JSON format.

Validate the entire payload before writing: envelopes, required fields, strict
identifier/numeric types, finite damage, documented ranges, duplicate identities,
event order, references, and participant membership. Resolve Person references
before importing soldiers and Soldier references before importing fights.
Reject missing references; do not manufacture placeholder people or soldiers.
Validate both existing references and records in the same batch.

Use one atomic transaction per import batch, database uniqueness constraints,
and explicit counts of created/unchanged records. Reject conflicting duplicate
IDs within a batch. Empty batches are a no-op; absent records are not deletions.
Errors must include the record/event location and leave existing data intact.

For a stable external fight identity, use that identity plus event position for
repeat-import reconciliation. The conservative proposed policy is to accept an
identical existing fight as unchanged and reject a conflicting event log rather
than silently replacing replay inputs. Corrections/revisions require an explicit
policy, especially after processing begins. Without stable external fight IDs,
idempotency across different files cannot be guaranteed: require a documented
source identity or an explicit user-supplied import identity before implementing
fight imports. Content hashes alone can collapse legitimately distinct identical
fights; list indexes are only stable within a particular captured payload.

Future workers must consume committed immutable inputs. Serialize updates to an
existing fight while importing and claiming it for replay; disallow changes once
claimed unless a revision design is introduced. Unique keys prevent duplicate
identities but do not themselves prevent all concurrent import/replay races.
Use database-backed claim/state transitions when workers are designed in Phase 3.
The current SQLite database does not provide effective `select_for_update` row
locking; do not claim this project already supports concurrent worker safety.
Choose and test a suitable database/concurrency strategy during Phase 3.

## Verification and deferred acceptance tests

The pre-change baseline passed all 26 existing tests. Final Phase 2 verification
on 2026-11-14 also passed:

```text
python manage.py check                         No issues (0 silenced)
python manage.py makemigrations --check --dry-run   No changes detected
python manage.py test                          26 tests, OK
```

Commands were run with `.\.venv\Scripts\python.exe`. The interpreter required
execution outside the sandbox because its Windows Store Python target was
inaccessible inside the sandbox. No runtime code changed, so no new automated
tests were added. Existing tests exercise Person validation, database ID/age
constraints, duplicate imports, invalid inputs, and transaction rollback.

There are no executable soldier/fight integrity or event-order tests yet because
there are no corresponding implemented tables/importers. Once contracts arrive,
add tests for:

- Valid external identities including zero where allowed; exact ID preservation;
  Person/Soldier references; protected deletion and missing-FK database rejection.
- Two participants and event participant membership; self-reference policy;
  preserved array order; repeated identical events; unique fight/position pairs.
- Duplicate and conflicting identities, repeat imports with no duplicated events,
  unchanged imports, and any explicitly supported correction policy.
- Malformed JSON/encoding/envelopes, missing fields, strict numeric types
  (including booleans), nonfinite/out-of-range damage, missing references, and
  invalid late events rolling back all earlier writes in the batch.
- Local files without HTTP; import-time database failure rollback; migrations
  from the existing Person-only state without altering existing people.
- Concurrent imports/claims on the actual Phase 3 database, separately from
  ordinary single-transaction TestCase tests.

## Phase 3 plan

1. Obtain and record drafting/soldier/fight contracts and settle health scope,
   starting health, cross-fight ordering, and replay/death rules.
2. Finish the deferred Phase 2 models, migrations, validated local imports, and
   acceptance tests against those contracts before introducing simulation.
3. Design replay state, deterministic retry/idempotency behavior, database-backed
   worker claims, and shared-state locking on a verified database backend.
4. Implement ordered event replay in parallel background processes, then expose
   `/simulate` and HTML results with appropriate run/status behavior. Select
   worker infrastructure only when authorized for that phase.
5. Test repeat requests/retries, concurrent shared-soldier fights, failures,
   health boundaries, result rendering, and the settled winner/death rules.
