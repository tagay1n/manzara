# PostgreSQL backup and recovery

## Managed Aiven logical backups

Manzara supplements Aiven's managed backups with portable logical dumps in the
private Backblaze B2 `ttbackups` bucket. The GitHub Actions workflow
`.github/workflows/nightly-postgres-backup.yml` runs at 01:17 UTC every day and
can also be dispatched manually. Every object is a complete, independently
restorable `pg_dump` custom archive; there is no incremental logical-backup
chain.

The workflow uses PostgreSQL 18 client tools from the official `postgres:18`
container. It dumps the `monocorpus` and `public` schemas plus the `pg_trgm`
extension with ownership and privileges omitted. It validates the archive with
`pg_restore --list`, records its SHA-256 as object metadata, requests Backblaze
SSE-B2 AES-256 encryption, and verifies the uploaded size, checksum metadata,
and encryption response before succeeding.

Backups use two retention tiers:

- `logical/manzara/daily/YYYY/MM/manzara-<UTC timestamp>.dump` stores every
  successful run.
- `logical/manzara/monthly/YYYY/manzara-YYYY-MM.dump` stores the first
  successful run in each UTC month. Later runs keep that recovery point intact.

The monthly object is the same complete dump as that night's daily object. It
is retained longer; it is not an incremental backup.

### GitHub and Backblaze setup

Download the Aiven project CA outside the repository. Configure these GitHub
Actions secrets:

- `MANZARA_DATABASE_URL`: the Aiven URL. The workflow overrides its TLS options
  with `sslmode=verify-full` and the supplied project CA.
- `MANZARA_AIVEN_CA_CERT_BASE64`: the base64-encoded PEM project CA.
- `MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID`: a dedicated B2 application key ID.
- `MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY`: that key's secret.

For example, set the CA without placing it in the repository:

```bash
base64 -w 0 ~/.manzara/private/credentials/database-migration/aiven-ca.pem \
  | gh secret set MANZARA_AIVEN_CA_CERT_BASE64
```

Configure these non-secret GitHub repository variables:

```text
MANZARA_LOGICAL_BACKUP_S3_ENDPOINT=https://s3.eu-central-003.backblazeb2.com
MANZARA_LOGICAL_BACKUP_S3_REGION=eu-central-003
MANZARA_LOGICAL_BACKUP_S3_BUCKET=ttbackups
```

Create a dedicated B2 application key restricted to bucket `ttbackups` and file
name prefix `logical/manzara/`. Give it only `listFiles`, `readFiles`, and
`writeFiles`; do not grant `deleteFiles` or bucket-management capabilities.
The read capability is required to verify uploads and preserve the first
monthly object.

The workflow explicitly requests SSE-B2 for each upload, so bucket-default
encryption is not required. SSE-B2 protects stored bytes at rest using a
Backblaze-managed key. Anyone with valid B2 read credentials can still download
the plaintext archive, so keep the application key scoped and secret.

### Retention policy

Retention belongs to Backblaze rather than the GitHub credential. In the B2
console, open the `ttbackups` bucket's **Lifecycle Settings**, select custom
rules, and configure exactly these prefix-scoped rules:

```json
[
  {
    "fileNamePrefix": "logical/manzara/daily/",
    "daysFromUploadingToHiding": 90,
    "daysFromHidingToDeleting": 1,
    "daysFromStartingToCancelingUnfinishedLargeFiles": 1
  },
  {
    "fileNamePrefix": "logical/manzara/monthly/",
    "daysFromUploadingToHiding": 730,
    "daysFromHidingToDeleting": 1,
    "daysFromStartingToCancelingUnfinishedLargeFiles": 1
  }
]
```

Do not use an empty or bucket-wide prefix: this bucket may later contain other
backup repositories. Backblaze lifecycle processing is asynchronous, so an
expired object can remain visible briefly before it is hidden and permanently
deleted.

Enable GitHub Actions failure notifications for the account that owns the
schedule. GitHub may delay or drop a scheduled run during heavy load; a missed
night does not affect the next full dump, and the next successful run creates
the month's long-term object if it is still absent.

### Validation and restore drill

After enabling the workflow, dispatch it manually once. Confirm the Action
summary reports the daily key, monthly key, size, SHA-256, and SSE-B2. Download
one object with an independently held read credential:

```bash
aws s3 cp \
  s3://ttbackups/logical/manzara/daily/YYYY/MM/manzara-TIMESTAMP.dump \
  /tmp/manzara-restore.dump \
  --endpoint-url https://s3.eu-central-003.backblazeb2.com

aws s3api head-object \
  --bucket ttbackups \
  --key logical/manzara/daily/YYYY/MM/manzara-TIMESTAMP.dump \
  --endpoint-url https://s3.eu-central-003.backblazeb2.com

sha256sum /tmp/manzara-restore.dump
docker run --rm \
  --volume=/tmp/manzara-restore.dump:/backup/manzara.dump:ro \
  postgres:18 pg_restore --list /backup/manzara.dump
```

Compare the local SHA-256 with the `sha256` value in the object's metadata.
Then restore into a new, empty, isolated PostgreSQL database using a private
libpq service file and `pg_restore --no-owner --no-privileges`. Never perform a
restore drill against production. Verify the Alembic revision, representative
table counts, and application reads before declaring the drill successful.

Update the workflow's PostgreSQL container major version before upgrading Aiven
past PostgreSQL 18; an older `pg_dump` cannot dump a newer server.

## Local PostgreSQL physical backups

Manzara's Full backup and Incremental backup tasks operate one pgBackRest
repository. The repository is a private Backblaze B2 bucket configured under
`backups.pgbackrest`; full and incremental backup sets must not be split across
buckets.

## Storage contract

```yaml
backups:
  pgbackrest:
    endpoint_url: https://s3.eu-central-003.backblazeb2.com
    region_name: eu-central-003
    bucket: ttbackups
    repository_path: /pgbackrest
    access_key_id: "<local-only>"
    secret_access_key: "<local-only>"
```

`credential_source: documents.primary_storage` may be used locally while both
roles intentionally share one Backblaze application key. Prefer a dedicated,
bucket-restricted backup key for long-term operation. Never commit either key.

Keep the bucket private. Server-side AES-256 encryption and versioning are
compatible with pgBackRest. Do not enable a default Object Lock retention
without a separate retention design: locked objects prevent pgBackRest expiry.

The live pgBackRest repository additionally uses client-side AES-256-CBC
encryption. Its generated cipher passphrase lives only in the root-owned
pgBackRest configuration and must be escrowed with other disaster-recovery
secrets. Losing that passphrase makes the repository unrecoverable.

## Yandex-to-Backblaze cutover

The migration helper is fail-safe at the configuration boundary. It retains a
timestamped copy of the Yandex configuration, initializes the Backblaze stanza,
atomically activates Backblaze, runs `check`, creates a new full backup, and
runs pgBackRest repository verification. If initialization, check, backup, or
verification fails, it restores the prior configuration.

Run from the repository root in an interactive terminal:

```bash
sudo env PYTHONPATH=. .venv/bin/python \
  scripts/migrate_pgbackrest_to_backblaze.py --apply \
  --config /home/tans1q/projects/manzara/config.yaml
```

The script does not delete or modify the Yandex repository. Keep its credentials
and the reported `pgbackrest.conf.pre-backblaze-*` file until the restore drill
and an agreed overlap period have completed.

After cutover, restart Manzara so its task process inherits the current local
configuration. Both dashboard backup tasks will then use the active Backblaze
pgBackRest repository, and their post-run verifier will inspect
`s3://ttbackups/pgbackrest/`.

## Validation

For the latest completed task run, confirm marker objects through Manzara:

```bash
PYTHONPATH=. .venv/bin/python \
  app/modules/maintenance/runtime/check_backup_s3.py \
  --task-id maintenance.pgbackrest_backup_full
```

Check pgBackRest's repository metadata and complete checksums as `postgres`:

```bash
sudo -u postgres pgbackrest --stanza=monocorpus info
sudo -u postgres pgbackrest --stanza=monocorpus --set=<label> --verbose verify
```

Marker checks are not a substitute for pgBackRest verification or a restore
drill.

## Recovery boundary

A production restore is destructive and must never be tested against the live
PostgreSQL data directory. Prove recovery in a separately owned data directory
and on a non-production port. The drill must:

1. Select the intended full or incremental label with `pgbackrest info`.
2. Restore it into an empty isolated PostgreSQL 18 data directory using an
   explicit `--pg1-path` override.
3. Start that cluster on a separate port and socket directory.
4. Confirm recovery completes, connect with `psql`, and inspect expected
   databases, schemas, and representative row counts.
5. Stop the isolated cluster before removing its drill directory.

The dedicated helper performs those steps on port `55432` and removes only its
own temporary directories after a successful or failed drill:

```bash
sudo env PYTHONPATH=. .venv/bin/python scripts/pgbackrest_restore_drill.py \
  --set 20260829-115401F
```

For an actual incident, stop Manzara and PostgreSQL first, preserve the damaged
data directory for forensics, select the desired backup/PITR target, and only
then run pgBackRest restore. A production restore requires an incident-specific
decision about latest-backup recovery versus point-in-time recovery.
