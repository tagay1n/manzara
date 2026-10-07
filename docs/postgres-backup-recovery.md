# PostgreSQL backup and recovery

## Backup contract

`.github/workflows/nightly-postgres-backup.yml` runs at **01:27 UTC** and supports manual dispatch. `scripts/backup_postgres_to_b2.py` creates independent custom-format logical dumps using PostgreSQL 18 clients, covering `monocorpus`, `public`, and `pg_trgm`, without ownership/privileges.

It validates with `pg_restore --list`, records SHA-256 metadata, requests SSE-B2 AES-256, and verifies remote size/checksum/encryption. This documents workflow behavior; it does not confirm current remote configuration or successful runs.

| Recovery tier | Object key | Retention |
| --- | --- | --- |
| Daily | `logical/manzara/daily/YYYY/MM/manzara-<UTC timestamp>.dump` | Hide after 90 days; delete 1 day later |
| Monthly | `logical/manzara/monthly/YYYY/manzara-YYYY-MM.dump` | Hide after 730 days; delete 1 day later |

Monthly retains the first successful complete dump of the UTC month; there is no incremental chain.

## Configuration

Actions secrets:

- `MANZARA_DATABASE_URL`
- `MANZARA_AIVEN_CA_CERT_BASE64`: project PEM CA; workflow enforces `sslmode=verify-full`
- `MANZARA_LOGICAL_BACKUP_S3_ACCESS_KEY_ID`
- `MANZARA_LOGICAL_BACKUP_S3_SECRET_ACCESS_KEY`

Repository variables:

- `MANZARA_LOGICAL_BACKUP_S3_ENDPOINT`: `https://s3.eu-central-003.backblazeb2.com`
- `MANZARA_LOGICAL_BACKUP_S3_REGION`: `eu-central-003`
- `MANZARA_LOGICAL_BACKUP_S3_BUCKET`: `ttbackups`

Keep the CA and credentials outside git under the artifacts root's `private/credentials/`. Scope the B2 key to `ttbackups`, prefix `logical/manzara/`, and `listFiles`/`readFiles`/`writeFiles`. Read access verifies uploads and preserves monthly objects; no delete/bucket-management permission is needed.

Configure B2 lifecycle rules **only** for `logical/manzara/daily/` and `logical/manzara/monthly/`, using the retention values above and canceling unfinished large files after 1 day. Never use a bucket-wide prefix. Processing is asynchronous. SSE-B2 is encryption at rest; valid read credentials still yield plaintext archives.

## Restore drill

After setup, dispatch once and inspect the Action summary: daily/monthly keys, size, SHA-256, encryption. Download a dump with independently held read credentials:

```bash
aws s3 cp s3://ttbackups/logical/manzara/daily/YYYY/MM/manzara-TIMESTAMP.dump /tmp/manzara-restore.dump \
  --endpoint-url https://s3.eu-central-003.backblazeb2.com
aws s3api head-object --bucket ttbackups --key logical/manzara/daily/YYYY/MM/manzara-TIMESTAMP.dump \
  --endpoint-url https://s3.eu-central-003.backblazeb2.com
sha256sum /tmp/manzara-restore.dump
docker run --rm --volume=/tmp/manzara-restore.dump:/backup/manzara.dump:ro \
  postgres:18 pg_restore --list /backup/manzara.dump
```

Compare local SHA-256 with object metadata. Restore into a new, empty, isolated PostgreSQL database via a private libpq service file using `pg_restore --no-owner --no-privileges`. Never use production for drills. Verify Alembic revision, normalized catalog/checkpoint counts, constraints/indexes, and representative reads. Backend readiness remains a separate unresolved issue.

Enable Actions failure notifications and verify recovery points periodically; schedules may be missed. Update client major version before a server upgrade beyond PostgreSQL 18.
