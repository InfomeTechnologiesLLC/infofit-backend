# HealthySync integration

The backend accepts HealthySync's schema-v2 uploads directly. The iPhone app reads
Apple Health and posts JSON to `POST /api/apple-health/sync`. The route has no
trailing slash; a slash variant is also accepted without a redirect.

Contract checked against HealthySync's public `APIClient.swift`, `HealthNormalizer.swift`,
`HealthKitManager.swift`, and `Models/SyncBatch.swift` on 2026-11-03:

- <https://healthysync.app/support#api>
- <https://github.com/megabyte0x/healthykit>

## Set up an employee

### Updating an existing production installation

This release adds `fitness.0003_apple_health_sync`. **MySQL 5.7 deployments use a
maintenance window with this application's writers stopped until the updated
code is running.** A rolling reload with old workers still writing is unsuitable
for this database version.

The migration preserves existing source IDs, active-device assignments, sync
sequences, activity records, and reward data. Existing sources receive
`kind="health_connect"`; old daily rows receive `verified_metrics={}` and retain
their existing scoring. Existing exercise rows receive `duration_seconds=NULL`.
Three new tables hold Apple Health connections, records, and sync batches.
Existing Android leaderboard totals are preserved as well.

#### Confirmed CentOS installation (2026-11-04)

- Application directory:
  `/home/infomeglobal/public_html/infome-fit.infoeventz.com/infofit-backend`.
- Interpreter: `./newenv/bin/python`, Python 3.9.6, Django 4.1, mysqlclient 2.2.7.
- Database: MySQL 5.7.44, InnoDB/Dynamic, `latin1` tables, strict SQL mode.
- Applied fitness migration: `0002_manual_entries_and_recording_method`.
- DRF 3.16.1, SimpleJWT 5.5.1 and CORS headers 4.9.0 are already installed.
  Their reported Django dependency conflicts predate this release. This rollout
  uses the existing environment, including its installed migration files.
- OpenSSL 1.0.2k-fips; no SQLite module, Firebase SDK, Redis Python client,
  urllib3, or cryptography package. Apple Health and the employee web app add no
  package dependencies. Firebase delivery still requires its existing optional
  SDK; a credentials file alone does not provide it.

The supplied production `config/settings.py` has a server-specific `DATABASES`
dictionary with `OPTIONS = {"sql_mode": "traditional"}`, host/CSRF settings, and
additional `GATE_*` configuration. Preserve that file and merge only the new
`"employee_web"` entry into `INSTALLED_APPS`, immediately after `"portal"`. Keep
production credentials out of repository changes and deployment logs. Retain
`.env`, `SECRET_KEY`, media, and the existing virtual environments. The main requirements
file describes new/Docker environments and is not an installation step for this
server. A deployment job must not install it as part of this rollout.

The reported token-blacklist history includes `0008_auto_20261008_1209`, a custom
migration. Use the **fitness-only target** below so this release does not schedule
unrelated JWT migrations. Preserve installed package files and migration history.

#### Maintenance-window procedure

1. Retain the previous application release, production settings, and a verified
   database backup. Confirm how this specific application's Gunicorn and scheduled
   jobs are controlled, including whether a push triggers automatic deployment.
   The host runs many other Gunicorn services; its service name is not known from
   the diagnostic process list.
2. Put this application into maintenance and stop/drain its Gunicorn workers.
   Pause its cron/Celery jobs and let any in-flight writes finish. Apply this to
   this application only, using its actual service manager.
3. Deploy the complete release's source, templates, assets, and migrations. This
   includes all new `fitness/apple_health*` files, `fitness/migration_compat.py`,
   `fitness/migrations/0003_apple_health_sync.py`, the updated fitness/challenge
   code, portal changes, `config/urls.py`, and the entire `employee_web/` directory.
   Merge the settings entry described above. Exclude development databases,
   virtual environments, `.git`, `.opencode`, and local media from file uploads.
4. From the application directory, run these checks with its existing interpreter:

   ```sh
   ./newenv/bin/python -B manage.py check
   ./newenv/bin/python -B manage.py migrate fitness 0003_apple_health_sync --plan
   ```

   On the reported server, the plan should contain only
   `fitness.0003_apple_health_sync`. If it lists other migrations, reports inconsistent
   history, or the new columns already exist despite this migration being unapplied,
   investigate that difference before proceeding.
5. While writers remain stopped, apply that migration and collect static assets:

   ```sh
   ./newenv/bin/python -B manage.py migrate fitness 0003_apple_health_sync --noinput
   ./newenv/bin/python -B manage.py collectstatic --noinput
   ./newenv/bin/python -B manage.py showmigrations fitness
   ./newenv/bin/python -B manage.py check
   ```

   `showmigrations` should mark `0003_apple_health_sync` with `[X]`.
6. Start the updated application using its existing Gunicorn service and environment.
   Verify an existing Android account's history, rankings, token refresh, and next
   sync; then verify staff login, iPhone setup, an upload, and `/app/` sign-in. Resume
   this application's scheduled jobs and end maintenance.

MySQL DDL is not transactional. If a migration fails partway through, retain
maintenance and inspect the actual columns/tables before retrying; do not mark a
failed migration as applied with `--fake`. After a completed upgrade, starting the
old code alone is not a rollback: old workers cannot insert the new non-null JSON
column without a SQL default. A rollback must restore a compatible schema and
application release together while writers are stopped.

#### How the compatibility operation works

`db_default` is a Django 5.0+ field option. On Django 3.2/4.x the models and migration
use `fitness/migration_compat.py` without passing that option. When SQL defaults are
supported, the operation retains them. On MySQL 5.7 it delegates JSON-column creation
to Django's normal schema editor, which populates existing rows with a one-off
`UPDATE` and leaves no permanent JSON default. New workers supply the Python default
on subsequent inserts. The string default for source `kind` remains supported.

The migration's final model state and name are unchanged; installations that already
successfully applied `0003` do not need to run it again. Deploy the models, migration,
and compatibility helper together.

### Connecting HealthySync

Employees can also open **`/app/`** on the backend in Safari and sign in with their
HealthySync Auth token. The employee web app shows synced activity, rankings,
challenges and community posts. See `employee_web/README.md` for its behavior and
deployment. Rotation or revocation also ends browser access on the next request.

Complete the deployment procedure above once before onboarding the first iPhone.

Then use the staff portal for routine iPhone onboarding (no SSH needed):

1. Open **Employees → Add employee**, or select an existing employee.
2. On their employee page, choose **Set up iPhone** / **Manage iPhone sync**.
3. Choose **Generate token**, then **Copy setup instructions** and send those details
   to the employee. The token is displayed only in the generation response; only its
   hash is saved. The page also shows the backend base URL and connection status.
4. To replace a lost token on the same phone, choose **Rotate token**. The old token
   stops working immediately; update HealthySync with the replacement.
5. For a new phone or reinstall, select **This is a new iPhone or a HealthySync
   reinstall** and, if a device is already active, **Replace the active fitness device**.
   Switching from Android also requires **Replace the active fitness device**.
6. **Revoke token** stops uploads and keeps previously synced activity.

Only authorized portal staff can manage employee connections. Deactivated employees
must be reactivated before generating a token.

Alternatively, issue a token for an existing employee from the command line:

```sh
python manage.py issue_apple_health_token employee@example.com
```

If they already have an active Android or different iPhone source, add
`--replace-existing`. For a replacement iPhone or a HealthySync reinstall whose
device ID changed, add **both** `--replace-existing --new-device`.

In HealthySync settings:

1. Choose **Your own backend**.
2. Enter the base HTTPS URL, e.g. `https://fit.example.com`.
3. Paste the generated token into the auth-token field.
4. Enable the desired Apple Health categories and run a sync.

HealthySync appends `/api/apple-health/sync` itself: do not paste the full endpoint
into its backend-URL field. First successful upload binds the token to the app's
device ID. The installation ID used internally by Infome Fit is separate.

For example, to exercise the endpoint without an iPhone:

```sh
curl --fail-with-body https://fit.example.com/api/apple-health/sync \
  -H "Authorization: Bearer $HEALTH_SYNC_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'X-Device-Id: test-iphone' \
  --data '{
    "schema_version": 2,
    "device_id": "test-iphone",
    "export_id": "11111111-1111-4111-8111-111111111111",
    "generated_at": "2026-10-01T12:00:00Z",
    "timezone": "Asia/Kolkata",
    "source": "ios-healthkit",
    "date_range": {"start": "2026-09-30T00:00:00Z", "end": "2026-10-01T00:00:00Z"},
    "metrics": [{
      "id": "healthkit:22222222-2222-4222-8222-222222222222",
      "type": "step_count", "value": 1200, "unit": "count",
      "start_at": "2026-09-30T08:00:00Z", "end_at": "2026-09-30T09:00:00Z",
      "source_name": "iPhone", "source_bundle_id": "com.example.source", "metadata": {}
    }],
    "workouts": [], "deletions": []
  }'
```

Use a separate test account for this example: it binds the connection to
`test-iphone`. Generate a new-device token when moving that account to a real phone.

## Token-management API

These routes use the employee's **normal account JWT**, not the upload token:

| Method | URL | Behavior |
| --- | --- | --- |
| GET | `/api/v1/fitness/apple-health/token/` | Connection status, device/installation IDs, base URL, last sync; never returns a token |
| POST | Same | Creates or rotates the upload token and activates its Apple Health source |
| DELETE | Same | Revokes uploading while retaining previously synced history |

POST accepts:

```json
{"replace_existing": false, "new_device": false, "device_label": "My iPhone"}
```

The response contains `token`, `backend_url`, `installation_id`, and `device_id`.
Plaintext tokens are returned only when issued, with `Cache-Control: no-store`;
only their SHA-256 hashes are stored. Rotation preserves the device binding and
history unless `new_device` is requested. A new device gets a new fitness source.
Switching back to an existing Apple Health source reprojects its retained history.
Only one fitness source is active per employee.

Upload tokens cannot read account data, use other fitness APIs, or issue tokens.
Inactive employees and rotated/revoked tokens receive HTTP 401. A source/device
conflict receives HTTP 409. If `X-Device-Id` is present it must match `device_id`.

## Ingestion and normalization

Every successful upload responds with the fields HealthySync decodes:

```json
{"ok": true, "received": 1, "duplicates": 0, "deleted": 0, "export_id": "..."}
```

`received` counts inserted/updated records; `duplicates` counts unchanged, stale,
or already-deleted records; `deleted` counts live records removed. Unknown deletion
IDs are retained as tombstones. Retrying the same export returns its original
response. Reusing an export ID with changed content returns HTTP 409.

- Schema v2 is required. At most 1,000 metric/workout/deletion entries are accepted
  per batch (the app currently sends chunks of 100).
- Timestamps must include offsets. Incremental sync's zero-length `date_range`
  is valid; record timestamps may lie outside that range.
- Input validation, raw storage, projections, and challenge recalculation run in
  one transaction. HTTP 200 is returned only after successful processing.
- Raw metric/workout payloads are retained in `AppleHealthRecord`, scoped to the
  employee's source and record kind. Types without a shared fitness field, such
  as HRV, heart rate, weight, or nutrition, are retained without being summed into
  activity totals.
- Latest `generated_at` wins record corrections; an older delayed upload cannot
  overwrite a newer record. Deletions leave tombstones so queued uploads cannot
  resurrect the same sample ID.
- An omitted category is not a deletion or an observed zero. Apple Health's
  permission state is not exported; missing values remain null/`no_data`.

| HealthySync input | Shared projection |
| --- | --- |
| `step_count` | `DailyActivity.steps` |
| `distance_walking_running` | `distance_m` (metres) |
| `active_energy` | `active_kcal` (kilocalories) |
| `apple_exercise_time` | `exercise_minutes` |
| `sleep_analysis` | Asleep stages → `SleepRecord` and `sleep_minutes` |
| `workouts` | `ExerciseRecord`, normalized activity names, and workout-duration fallback for exercise minutes |

Quantity intervals crossing local midnight are prorated using actual elapsed
seconds, including DST. Stored records retain the exporting timezone. Workouts
belong to their local end date. Nearby asleep stages (up to a two-hour gap, within
a 24-hour session) are grouped onto the waking date; overlaps count once and gaps
do not count as sleep. Awake/in-bed stages are retained but do not add sleep time.

Workout start/end timestamps and active `duration_seconds` are preserved. Challenge
scoring uses active duration, respects its existing session caps, and excludes
manual workouts. Because pause timestamps are not exported, overlap calculations
represent active workout duration as a continuous interval beginning at the start.

## Summaries, source overlap, and challenge credit

Historical quantity exports can contain `healthkit:aggregate:TYPE:DATE` records
with `metadata.aggregation = "daily_sum"`. These replace overlapping raw values
for **Home, staff totals, rankings, and challenge credit**, rather than being added
to them. Raw readings outside a partial summary's interval still count. Aggregate
corrections replace their previous value.
A known raw deletion invalidates an older overlapping summary until it is refreshed.
An unknown deletion ID carries no date/type, so it cannot invalidate an unrelated
summary. Refresh the summary to reflect that deletion. Summaries are snapshots:
late-arriving samples inside an already summarized interval also require a fresh
summary for an exact displayed total.

HealthySync does not export Apple's source-priority list or a manual-entry breakdown
for summaries. Consequently:

- For raw quantity intervals, overlapping sources use the largest per-source rate;
  they are not added together. This is an approximation, not an exact reproduction
  of Apple Health's private overlap policy. Sources with the same exported bundle
  ID cannot always be distinguished.
- `HKWasUserEntered` identifies manual activity to subtract from competition credit,
  including known manual readings covered by a summary. Unknown manual content inside
  a summary is accepted; lacking a breakdown does not make the daily total unavailable.
- `DailyActivity.verified_metrics` stores summary-first daily totals minus identified
  manual activity. The field name is retained for API compatibility. Summary-only
  history counts for step rankings and step/distance/calorie/step-day challenges.
  Adding overlapping raw samples does not double-count or reduce summary credit to
  a partial raw total. Existing challenge caps, date windows, and reward corrections apply.
- Shared fitness responses expose `verified_metrics` and `source: "apple_health"`.
  Android days retain their existing total-minus-manual scoring behavior.

The phone controls background scheduling. Check the connection's last successful
sync timestamp and use manual sync/backfill when needed.

### Recalculate history saved before summary credit was enabled

After deploying the updated backend and restarting its application workers, run:

```sh
./newenv/bin/python -B manage.py rebuild_apple_health_activity
```

This reprojects retained records for each account's active Apple Health source,
then recalculates enrolled challenge progress, points, and badges in the same
per-account transaction. Already-saved summary-only and partially credited days
become eligible without requiring another phone upload. Repeated runs do not
duplicate rewards. Android-active accounts and inactive Apple Health sources are
skipped. No schema migration is needed for this change.

Pass an employee email to recalculate only that account:

```sh
./newenv/bin/python -B manage.py rebuild_apple_health_activity employee@example.com
```

## Verification

Run these on a development/test database, not the production database. Use MySQL
5.7 with `latin1` test-table defaults and strict mode for legacy compatibility;
SQLite alone cannot exercise the JSON-default restriction.

```sh
python manage.py test fitness challenges employee_web portal accounts community
python manage.py makemigrations --check --dry-run
```

The populated-upgrade test runs the real `0002` → `0003` migration, checks every old
fitness column, new-worker defaults, pre-upgrade access/refresh tokens, cached sync
retries, and the next Android sync. On MySQL 5.7 it also checks that the new JSON
column is non-nullable with no SQL default. Databases that support permanent
defaults additionally exercise old-ORM inserts. The API tests cover iPhone
onboarding, uploading, and browser reads with optional service SDK imports blocked.

Local verification on 2026-11-04:

- Python 3.9.25, Django 4.1, mysqlclient 2.2.7, and MySQL 5.7.44 with InnoDB,
  `latin1` test-table defaults, and the production `sql_mode: traditional` option:
  **302 tests passed** (including the existing Firebase tests with that SDK installed).
- A separate environment containing the server's listed application-package
  versions, without Firebase/Redis/urllib3/cryptography and with `_sqlite3` imports
  blocked: **299 tests passed** on MySQL. Three existing tests that require the
  Firebase SDK were excluded from this run.
- Django 4.1/SQLite: **302 tests passed**. Migration drift check: **no changes detected**.

These runs used synthetic test data and local client libraries, not production
credentials, the production database, its custom JWT migration files, or its exact
Python 3.9.6/OpenSSL build. Confirm the targeted migration plan on the server using
the procedure above; live outbound TLS and real-device uploads remain deployment
checks.

The integration tests cover the wire contract, units, timestamps/DST, summaries,
overlapping sources, workout pauses, sleep stages, deletion/retry ordering,
authentication and source isolation, token lifecycle, and reward corrections.
They exercise the Django endpoint with representative HealthySync payloads; a
real-device sync is the final deployment check.
