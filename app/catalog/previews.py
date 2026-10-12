"""One-message preview commands on the lock-owning PostgreSQL session."""

import json
from pathlib import Path

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import TRANSACTION_STATUS_IDLE
from psycopg2.extras import Json

from app.catalog.contracts import CatalogConflict, CatalogNotFound, integer, nonblank
from app.document_operation_lock import DocumentOperationBusy, check_document_operation
from app.runtime_config import config_integer

_BATCH = Path(__file__).with_suffix('.sql').read_text(encoding='utf-8')


def validate_pages(pages, source_page_count):
    integer(source_page_count, 'source_page_count', minimum=0)
    if not isinstance(pages, list):
        raise ValueError('preview pages must be an array')
    roles, numbers = set(), set()
    for page in pages:
        if not isinstance(page, dict) or set(page) != {'role', 'page_number', 'small_key', 'large_key'}:
            raise ValueError('unsupported preview page')
        if page['role'] not in {'first', 'second', 'last'} or page['role'] in roles:
            raise ValueError('preview roles must be distinct')
        number = integer(page['page_number'], 'page_number')
        if number > source_page_count or number in numbers:
            raise ValueError('preview pages must be distinct and within the document')
        roles.add(page['role'])
        numbers.add(number)
        nonblank(page['small_key'], 'small_key')
        nonblank(page['large_key'], 'large_key')


def preview_command(engine, schema, payload, *, source_query, pending_query, operation=None):
    """Implicit transaction: guards, mutations, result and commit in one message.

    Success/renewal/claim use the reserved session that owns the document lock.
    Failure finalization, after remote work has unwound, takes a transaction lock.
    Failed/ambiguous writes are never automatically replayed.
    """
    if operation is not None:
        check_document_operation(operation, probe=False, md5=payload['md5'])
        if operation.engine is not engine:
            raise CatalogConflict('Preview command requires the document lock-owning engine')
        pooled = None
        connection = operation.connection.driver_connection
        invalidate = operation.invalidate
    else:
        if payload['action'] != 'fail':
            raise CatalogConflict('Preview publication requires a document operation lock')
        pooled = engine.raw_connection()
        connection = pooled.driver_connection
        invalidate = pooled.invalidate
    previous_autocommit = connection.autocommit
    statement = sql.SQL('SET TRANSACTION READ WRITE; SET LOCAL search_path TO {}, public;\n').format(sql.Identifier(schema))
    statement += sql.SQL(_BATCH.replace('{{source_query}}', source_query).replace('{{pending_query}}', pending_query))
    try:
        if connection.get_transaction_status() != TRANSACTION_STATUS_IDLE:
            raise RuntimeError('Preview batch requires an idle PostgreSQL session')
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute(statement, {
                'payload': Json(payload, dumps=lambda value: json.dumps(value, default=lambda item: item.isoformat())),
                'lock_timeout': f"{config_integer('postgres', 'lock_timeout_seconds') * 1000}ms",
            })
            return cursor.fetchone()[0]
    except psycopg2.Error as exc:
        errors = {'MZ001': CatalogConflict, 'MZ002': DocumentOperationBusy, 'MZ003': CatalogNotFound}
        if exc.pgcode in errors:
            raise errors[exc.pgcode](exc.diag.message_primary) from None
        invalidate()
        raise
    except BaseException:
        invalidate()
        raise
    finally:
        try:
            if not connection.closed:
                if connection.get_transaction_status() != TRANSACTION_STATUS_IDLE:
                    connection.rollback()
                connection.autocommit = previous_autocommit
        except BaseException:
            invalidate()
            raise
        finally:
            if pooled is not None:
                pooled.close()
