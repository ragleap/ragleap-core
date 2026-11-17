"""
Data source CRUD and sync orchestration for RagLeap Core integrations.
Talks directly to Postgres (plain psycopg2, matching core/ingest.py and
core/retrieval.py's pattern) rather than through an ORM.
"""
import os
import json
import logging
import uuid
from typing import Dict, Any, List, Optional

from core.integrations.base import DataSource, encrypt_secret, decrypt_value, protect_headers, reveal_headers
from core.integrations.factory import get_connector, CONNECTOR_MAP

logger = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://ragleap:ragleap@localhost:5433/ragleap_core")


def _get_connection():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def _row_to_data_source(row) -> DataSource:
    """Build a DataSource from a DB row, decrypting credentials."""
    (id_, name, source_type, connection_string, api_endpoint, api_key,
     api_headers, query_template, field_mappings, user_identifier_field,
     documents_table_name, csv_content, csv_filename) = row
    return DataSource(
        id=str(id_),
        name=name,
        source_type=source_type,
        connection_string=decrypt_value(connection_string),
        api_endpoint=api_endpoint,
        api_key=decrypt_value(api_key),
        api_headers=reveal_headers(api_headers),
        query_template=query_template,
        field_mappings=field_mappings or {},
        user_identifier_field=user_identifier_field,
        documents_table_name=documents_table_name,
        csv_content=csv_content,
        csv_filename=csv_filename,
    )


def create_data_source(
    name: str,
    source_type: str,
    connection_string: Optional[str] = None,
    api_endpoint: Optional[str] = None,
    api_key: Optional[str] = None,
    api_headers: Optional[Dict] = None,
    query_template: Optional[str] = None,
    field_mappings: Optional[Dict] = None,
    user_identifier_field: str = "user_id",
) -> Dict[str, Any]:
    """Create a new data source. Sensitive fields are encrypted before storage."""
    if source_type not in CONNECTOR_MAP:
        raise ValueError(
            f"Unsupported source type: {source_type}. "
            f"Supported types: {', '.join(sorted(CONNECTOR_MAP.keys()))}"
        )

    # Credentials are never stored in plain text: with no key this raises (the route answers 400).
    enc_connection_string = encrypt_secret(connection_string)
    enc_api_key = encrypt_secret(api_key)
    stored_headers = protect_headers(api_headers)

    conn = _get_connection()
    try:
        cur = conn.cursor()
        data_source_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO data_sources
                (id, name, source_type, connection_string, api_endpoint, api_key,
                 api_headers, query_template, field_mappings, user_identifier_field)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                data_source_id, name, source_type,
                enc_connection_string, api_endpoint, enc_api_key,
                json.dumps(stored_headers), query_template,
                json.dumps(field_mappings or {}), user_identifier_field,
            ),
        )
        conn.commit()
        cur.close()
        logger.info(f"Created data source '{name}' ({source_type})")
        return {"id": data_source_id, "name": name, "source_type": source_type}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_csv_data_source(
    name: str,
    csv_content: str,
    csv_filename: str,
    user_identifier_field: str = "user_id",
) -> Dict[str, Any]:
    """
    Create a CSV data source. Unlike create_data_source(), this takes raw
    CSV text directly rather than connection parameters — content is
    stored in Postgres (no persistent disk volume exists in
    docker-compose.yml for the app container).
    """
    conn = _get_connection()
    try:
        cur = conn.cursor()
        data_source_id = str(uuid.uuid4())
        cur.execute(
            """
            INSERT INTO data_sources
                (id, name, source_type, user_identifier_field, csv_content, csv_filename)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (data_source_id, name, "csv", user_identifier_field, csv_content, csv_filename),
        )
        conn.commit()
        cur.close()
        logger.info(f"Created CSV data source '{name}' ({len(csv_content)} chars)")
        return {"id": data_source_id, "name": name, "source_type": "csv"}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_data_source(data_source_id: str) -> Optional[DataSource]:
    """Fetch a single data source by ID, with credentials decrypted."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, source_type, connection_string, api_endpoint, api_key,
                   api_headers, query_template, field_mappings, user_identifier_field,
                   documents_table_name, csv_content, csv_filename
            FROM data_sources WHERE id = %s
            """,
            (data_source_id,),
        )
        row = cur.fetchone()
        cur.close()
        return _row_to_data_source(row) if row else None
    finally:
        conn.close()


def list_data_sources() -> List[Dict[str, Any]]:
    """List all data sources, without exposing decrypted credentials."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT id, name, source_type, is_active, last_sync_at,
                   last_sync_status, last_sync_record_count, sync_interval_minutes
            FROM data_sources ORDER BY created_at DESC
            """
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id": str(r[0]), "name": r[1], "source_type": r[2],
                "is_active": r[3], "last_sync_at": r[4].isoformat() if r[4] else None,
                "last_sync_status": r[5], "last_sync_record_count": r[6],
                "sync_interval_minutes": r[7],
            }
            for r in rows
        ]
    finally:
        conn.close()


def test_data_source(data_source_id: str) -> Dict[str, Any]:
    """Test a data source's connection without syncing any data."""
    data_source = get_data_source(data_source_id)
    if not data_source:
        return {"success": False, "error": "Data source not found"}

    connector = get_connector(data_source)
    success, message = connector.test_connection()
    return {"success": success, "message": message}


def sync_data_source(data_source_id: str) -> Dict[str, Any]:
    """
    Fetch data from the external source and store it as synced context,
    keyed by each record's user_identifier_field. Updates last_sync_*
    tracking fields on the data source regardless of outcome.
    """
    data_source = get_data_source(data_source_id)
    if not data_source:
        return {"success": False, "error": "Data source not found"}

    conn = _get_connection()
    try:
        connector = get_connector(data_source)
        records = connector.fetch_data()

        cur = conn.cursor()
        synced_count = 0
        id_field = data_source.user_identifier_field

        for record in records:
            user_identifier = record.get(id_field)
            if not user_identifier:
                continue

            cur.execute(
                """
                INSERT INTO synced_context_data (data_source_id, user_identifier, context_data)
                VALUES (%s, %s, %s)
                ON CONFLICT (data_source_id, user_identifier)
                DO UPDATE SET context_data = EXCLUDED.context_data, last_updated = now()
                """,
                (data_source_id, str(user_identifier), json.dumps(record, default=str)),
            )
            synced_count += 1

        cur.execute(
            """
            UPDATE data_sources
            SET last_sync_at = now(), last_sync_status = %s,
                last_sync_error = NULL, last_sync_record_count = %s
            WHERE id = %s
            """,
            ("success", synced_count, data_source_id),
        )
        conn.commit()
        cur.close()

        logger.info(f"Synced data source '{data_source.name}': {synced_count} records")
        return {"success": True, "records_synced": synced_count}

    except Exception as e:
        conn.rollback()
        cur = conn.cursor()
        cur.execute(
            "UPDATE data_sources SET last_sync_at = now(), last_sync_status = %s, last_sync_error = %s WHERE id = %s",
            ("failed", str(e), data_source_id),
        )
        conn.commit()
        cur.close()
        logger.error(f"Sync failed for data source {data_source_id}: {e}")
        return {"success": False, "error": str(e)}
    finally:
        conn.close()


def get_synced_context(data_source_id: str, user_identifier: str) -> Optional[Dict[str, Any]]:
    """Look up synced context data for a specific user from a specific data source."""
    conn = _get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT context_data FROM synced_context_data WHERE data_source_id = %s AND user_identifier = %s",
            (data_source_id, user_identifier),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    finally:
        conn.close()
