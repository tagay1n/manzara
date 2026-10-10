# PostgreSQL backup and recovery

## Backup contract

[nightly-postgres-backup.yml](../.github/workflows/nightly-postgres-backup.yml) runs at 01:27 UTC or manual dispatch. `scripts/backup_postgres_to_b2.py` creates independent custom-format dumps with YAML `backup.postgres_image`, covering `database_schema`, `public`, and `pg_trgm`, without ownership/privileges. Validate via `pg_restore --list`, record SHA-256, upload sequentially with SSE-B2 AES-256, and verify remote size/checksum/encryption.

| Tier | Object key | Lifecycle |
| --- | --- | --- |
| Daily | `logical/manzara/daily/YYYY/MM/manzara-<UTC timestamp>.dump` | Hide after 90 days; delete 1 day later |
| Monthly | `logical/manzara/monthly/YYYY/manzara-YYYY-MM.dump` | Hide after 730 days; delete 1 day later |

Monthly preserves the first successful full dump of each UTC month; no incremental chain.

## Configuration

[Operations](operations.md#runtime-configuration) lists credentials/profile setup. Workflow requires CA and `sslmode=verify-full`; local backup requires explicit YAML plus CA or URL certificate path. Store secrets in artifact `private/credentials/`. Scope the B2 key to `ttbackups`, `logical/manzara/`, and `listFiles`/`readFiles`/`writeFiles`; read verifies uploads/monthly preservation. No delete/bucket-management rights are needed.

Apply lifecycle rules only to the daily/monthly prefixes above; cancel unfinished large files after one day. Never use bucket-wide rules. Processing is asynchronous. SSE-B2 protects at rest; authorized downloads are plaintext.

## Restore drill

Dispatch once after setup and inspect keys/size/SHA-256/encryption. Download with independently held read credentials:

```bash
aws s3 cp s3://ttbackups/logical/manzara/daily/YYYY/MM/manzara-TIMESTAMP.dump /tmp/manzara-restore.dump \
  --endpoint-url https://s3.eu-central-003.backblazeb2.com
aws s3api head-object --bucket ttbackups --key logical/manzara/daily/YYYY/MM/manzara-TIMESTAMP.dump \
  --endpoint-url https://s3.eu-central-003.backblazeb2.com
sha256sum /tmp/manzara-restore.dump
docker run --rm --volume=/tmp/manzara-restore.dump:/backup/manzara.dump:ro \
  postgres:18 pg_restore --list /backup/manzara.dump
```

Compare SHA-256 with metadata. Restore directly into an empty isolated PostgreSQL database using private libpq service settings and `pg_restore --no-owner --no-privileges`; never production or a precreated baseline. Verify Alembic revision, catalog/checkpoint counts, constraints/indexes, representative reads. Older-than-`20261008_0062` dumps require historical code/reviewed upgrades under [database tools](operations.md#database-tools), never restamping. Worker readiness remains separate.

Enable Actions failure notifications, periodically verify recovery points, and update client major before server upgrades beyond PostgreSQL 18. A scheduled/listed archive does not prove successful backup or restore.
