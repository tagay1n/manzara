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
