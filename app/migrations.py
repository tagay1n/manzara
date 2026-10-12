"""Apply the retained Alembic chain before interactive task startup."""

from pathlib import Path
from typing import Callable

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from app.postgres_engine import get_postgres_engine
from app.settings import Settings, configured_schema
from app.task_runtime.logging import format_log, redact

VERSION_TABLE = "alembic_version_manzara"
_LOCK_SCOPE = "manzara.catalog-migrations"


class MigrationReadOnlyError(RuntimeError):
    """Pending upgrades cannot start under the current database safeguard."""


def apply_catalog_migrations(settings: Settings, *, write: Callable[[str], None]) -> None:
    """Wait for other CLI upgrades, then commit all pending revisions or fail."""
    root = Path(__file__).resolve().parent.parent
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    config.attributes["configure_logging"] = False
    scripts = ScriptDirectory.from_config(config)
    targets = set(scripts.get_heads())
    version_schema = configured_schema("migration_version_schema")
    engine = get_postgres_engine(settings.database_url, schema=settings.database_schema,
                                 pool_size=settings.database_pool_size)

    def log(message):
        write(format_log(message, context="startup"))

    def heads(connection):
        context = MigrationContext.configure(connection, opts={
            "version_table": VERSION_TABLE, "version_table_schema": version_schema,
        })
        return set(context.get_current_heads())

    lock = {"scope": _LOCK_SCOPE, "schema": version_schema}
    log("Checking PostgreSQL migrations; waiting for any other CLI upgrade to finish.")
    try:
        with engine.connect() as connection:
            try:
                connection.execute(text("SELECT pg_advisory_lock(hashtext(:scope), hashtext(:schema))"), lock)
                connection.commit()
                current = heads(connection)
                access = connection.execute(text("""SELECT
                    current_setting('transaction_read_only') AS read_only,
                    pg_is_in_recovery() AS recovery""")).mappings().one()
                connection.commit()
                if current == targets:
                    log("PostgreSQL migrations are current: " + ", ".join(sorted(targets)))
                else:
                    if access["read_only"] == "on" or access["recovery"]:
                        raise MigrationReadOnlyError(
                            "Database is read-only; pending migrations were not started "
                            f"({', '.join(sorted(current)) or 'empty'} → {', '.join(sorted(targets))}). "
                            "Check Aiven disk usage and read-only alerts. Restore adequate free space "
                            "and normal database writes before restarting the CLI; startup does not "
                            "override the disk safeguard."
                        )
                    log("Applying PostgreSQL migrations: " + (", ".join(sorted(current)) or "empty")
                        + " → " + ", ".join(sorted(targets)))
                    config.attributes["connection"] = connection
                    command.upgrade(config, "heads")
                    deployed = heads(connection)
                    connection.commit()
                    if deployed != targets:
                        raise RuntimeError("Database revisions do not match the migration heads")
                    log("PostgreSQL migrations complete: " + ", ".join(sorted(deployed)))
                connection.execute(text("SELECT pg_advisory_unlock(hashtext(:scope), hashtext(:schema))"), lock)
                connection.commit()
            except BaseException:
                connection.invalidate()
                raise
    except MigrationReadOnlyError:
        raise
    except Exception as exc:
        original = getattr(exc, "orig", exc)
        detail = getattr(getattr(original, "diag", None), "message_primary", None) or str(original)
        if getattr(original, "pgcode", None) == "25006":
            detail = (
                "Database refused migration writes in a read-only transaction. "
                "Check Aiven disk usage and read-only alerts; restore adequate free space "
                "and normal database writes before restarting. " + detail
            )
        raise RuntimeError(
            "PostgreSQL migrations did not complete; tasks are unavailable. "
            "Inspect the database revision before restarting, especially after a lost connection. "
            + redact(detail)
        ) from exc
