# PostgreSQL backup and recovery

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
