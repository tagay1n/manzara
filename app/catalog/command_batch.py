"""One-message catalog commands on the process's shared PostgreSQL pool."""

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE


def execute_catalog_batch(engine, schema, batch, parameters, *, expected_errors=()):
    """Return the final SELECT only after the implicit transaction is acknowledged."""
    # PostgreSQL keeps the entire simple-query message in one implicit transaction;
    # psycopg2 execute() waits for its completion, including the commit acknowledgement.
    statement = sql.SQL('SET TRANSACTION READ WRITE; SET LOCAL search_path TO {}, public;\n').format(sql.Identifier(schema))
    statement += sql.SQL(batch)
    pooled = engine.raw_connection()
    connection = pooled.driver_connection
    previous_autocommit = connection.autocommit
    try:
        if connection.get_transaction_status() != TRANSACTION_STATUS_IDLE:
            raise RuntimeError('Catalog command requires an idle pooled PostgreSQL connection')
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(statement, parameters)
            return cursor.fetchone()[0]
    except psycopg2.Error as exc:
        if exc.pgcode not in expected_errors:
            # An uncertain commit is never replayed. Discovery reconciles it later.
            pooled.invalidate()
        raise
    finally:
        try:
            if not connection.closed:
                if connection.get_transaction_status() != TRANSACTION_STATUS_IDLE:
                    connection.rollback()
                connection.autocommit = previous_autocommit
        except BaseException:
            pooled.invalidate()
            raise
        finally:
            pooled.close()
