"""
MongoDB connector for RagLeap Core integrations.
Ported from production's api/addon_services.py — logic unchanged,
only the makerag_* required-collection names renamed to ragleap_*.
"""
import json
import logging
from datetime import datetime
from typing import Dict, Any, Tuple, List

from core.integrations.base import BaseDatabaseConnector

logger = logging.getLogger(__name__)


_PLACEHOLDERS = ("{{user_id}}", "{{user_identifier}}")


def _fill_placeholders(node, identifier: str):
    """Put the identifier into the parsed filter as a plain value. The filter is data, never text that is
    parsed again, so the identifier cannot add keys, operators or quotes to it."""
    if isinstance(node, str):
        for p in _PLACEHOLDERS:
            node = node.replace(p, identifier)
        return node
    if isinstance(node, list):
        return [_fill_placeholders(x, identifier) for x in node]
    if isinstance(node, dict):
        return {k: _fill_placeholders(v, identifier) for k, v in node.items()}
    return node


class MongoDBConnector(BaseDatabaseConnector):
    """MongoDB database connector"""

    def test_connection(self) -> Tuple[bool, str]:
        try:
            from pymongo import MongoClient

            conn_string = self.data_source.connection_string
            client = MongoClient(conn_string, serverSelectionTimeoutMS=10000)
            client.admin.command('ping')
            client.close()

            return True, "Connection successful"
        except ImportError:
            return False, "pymongo not installed. Run: pip install pymongo"
        except Exception as e:
            return False, f"Connection failed: {str(e)}"

    def fetch_data(self, user_identifier: str = None) -> List[Dict]:
        try:
            from pymongo import MongoClient
            import bson

            client = MongoClient(self.data_source.connection_string)

            try:
                query_config = json.loads(self.data_source.query_template)
                database = query_config.get('database')
                collection = query_config.get('collection')
                filter_query = query_config.get('filter', {})
                projection = query_config.get('projection')

                if user_identifier:
                    filter_query = _fill_placeholders(filter_query, str(user_identifier))

                db = client[database]
                coll = db[collection]

                cursor = coll.find(filter_query, projection)
                results = []

                for doc in cursor:
                    if '_id' in doc and isinstance(doc['_id'], bson.ObjectId):
                        doc['_id'] = str(doc['_id'])
                    results.append(doc)

                return results
            finally:
                client.close()

        except Exception as e:
            logger.error(f"MongoDB fetch error: {e}")
            raise

    def introspect_schema(self) -> Dict[str, Any]:
        try:
            from pymongo import MongoClient

            client = MongoClient(self.data_source.connection_string, serverSelectionTimeoutMS=10000)

            try:
                db_name = None
                try:
                    query_config = json.loads(self.data_source.query_template)
                    db_name = query_config.get('database')
                except Exception:
                    pass

                if not db_name:
                    import re as _re
                    m = _re.search(r'/([^/?]+)(\?|$)', self.data_source.connection_string)
                    if m:
                        db_name = m.group(1)

                if not db_name:
                    return {'tables': [], 'error': 'No database specified in connection string or query template'}

                db = client[db_name]
                all_collections = db.list_collection_names()

                allowed_collections = []
                filtered_count = 0

                for coll_name in sorted(all_collections):
                    if self._is_table_blacklisted(coll_name):
                        filtered_count += 1
                        continue

                    coll = db[coll_name]
                    sample = coll.find_one()
                    doc_count = coll.estimated_document_count()

                    fields = []
                    if sample:
                        for key, value in sample.items():
                            fields.append({
                                'name': key,
                                'type': type(value).__name__,
                                'is_sensitive': self._is_column_sensitive(key),
                            })

                    allowed_collections.append({
                        'name': coll_name,
                        'columns': fields,
                        'column_count': len(fields),
                        'document_count': doc_count,
                    })

                return {
                    'tables': allowed_collections,
                    'total_tables': len(all_collections),
                    'allowed_tables': len(allowed_collections),
                    'filtered_count': filtered_count,
                    'database': db_name,
                    'db_type': 'mongodb',
                }
            finally:
                client.close()
        except Exception as e:
            logger.error(f"MongoDB schema introspection error: {e}")
            return {'tables': [], 'error': str(e)}

    def _get_mongo_db(self):
        db_name = None
        try:
            query_config = json.loads(self.data_source.query_template)
            db_name = query_config.get('database')
        except Exception:
            pass
        if not db_name:
            import re as _re
            m = _re.search(r'/([^/?]+)(\?|$)', self.data_source.connection_string)
            if m:
                db_name = m.group(1)
        return db_name

    def check_required_schema(self) -> Dict[str, Any]:
        try:
            from pymongo import MongoClient

            client = MongoClient(self.data_source.connection_string, serverSelectionTimeoutMS=10000)
            try:
                db_name = self._get_mongo_db()
                if not db_name:
                    return {'supported': True, 'error': 'No database specified'}

                db = client[db_name]
                existing = set(db.list_collection_names())

                required_collections = {
                    'ragleap_documents': self.REQUIRED_TABLES['ragleap_documents'],
                    'ragleap_chunks': self.REQUIRED_TABLES['ragleap_chunks'],
                    'ragleap_embeddings': self.REQUIRED_TABLES['ragleap_embeddings'],
                }

                tables = {}
                for coll_name, info in required_collections.items():
                    tables[coll_name] = {
                        'exists': coll_name in existing,
                        'status': 'ready' if coll_name in existing else 'missing',
                        'description': info['description'],
                        'columns': info['columns'],
                    }

                return {
                    'supported': True,
                    'tables': tables,
                    'all_ready': all(t['exists'] for t in tables.values()),
                    'missing_count': sum(1 for t in tables.values() if not t['exists']),
                    'has_pgvector': False,
                    'db_type': 'mongodb',
                }
            finally:
                client.close()
        except ImportError:
            return {'supported': False, 'message': 'pymongo not installed'}
        except Exception as e:
            logger.error(f"MongoDB schema check error: {e}")
            return {'supported': True, 'tables': {}, 'error': str(e)}

    def create_required_schema(self) -> Dict[str, Any]:
        try:
            from pymongo import MongoClient, ASCENDING

            client = MongoClient(self.data_source.connection_string, serverSelectionTimeoutMS=10000)
            created = []
            skipped = []
            errors = []

            try:
                db_name = self._get_mongo_db()
                if not db_name:
                    return {'supported': True, 'success': False, 'error': 'No database specified'}

                db = client[db_name]
                existing = set(db.list_collection_names())

                if 'ragleap_documents' in existing:
                    skipped.append('ragleap_documents')
                else:
                    try:
                        db.create_collection('ragleap_documents')
                        db['ragleap_documents'].create_index([('source_id', ASCENDING)])
                        db['ragleap_documents'].create_index([('created_at', ASCENDING)])
                        created.append('ragleap_documents')
                    except Exception as e:
                        errors.append({'table': 'ragleap_documents', 'error': str(e)})

                if 'ragleap_chunks' in existing:
                    skipped.append('ragleap_chunks')
                else:
                    try:
                        db.create_collection('ragleap_chunks')
                        db['ragleap_chunks'].create_index([('document_id', ASCENDING)])
                        db['ragleap_chunks'].create_index([('chunk_index', ASCENDING)])
                        created.append('ragleap_chunks')
                    except Exception as e:
                        errors.append({'table': 'ragleap_chunks', 'error': str(e)})

                if 'ragleap_embeddings' in existing:
                    skipped.append('ragleap_embeddings')
                else:
                    try:
                        db.create_collection('ragleap_embeddings')
                        db['ragleap_embeddings'].create_index([('chunk_id', ASCENDING)])
                        created.append('ragleap_embeddings')
                    except Exception as e:
                        errors.append({'table': 'ragleap_embeddings', 'error': str(e)})

                return {
                    'supported': True,
                    'created': created,
                    'skipped': skipped,
                    'errors': errors,
                    'success': len(errors) == 0,
                    'has_pgvector': False,
                    'db_type': 'mongodb',
                }
            finally:
                client.close()
        except ImportError:
            return {'supported': False, 'message': 'pymongo not installed'}
        except Exception as e:
            logger.error(f"MongoDB schema creation error: {e}")
            return {'supported': True, 'success': False, 'error': str(e)}

    def insert_document(self, table_name: str, title: str, content: str,
                        metadata=None, source_type: str = 'upload') -> Dict[str, Any]:
        try:
            from pymongo import MongoClient

            client = MongoClient(self.data_source.connection_string, serverSelectionTimeoutMS=10000)
            try:
                db_name = self._get_mongo_db()
                if not db_name:
                    return {'success': False, 'error': 'No database specified'}

                db = client[db_name]
                result = db[table_name].insert_one({
                    'title': title,
                    'content': content,
                    'metadata': metadata or {},
                    'source_type': source_type,
                    'created_at': datetime.utcnow(),
                })
                return {'success': True, 'id': str(result.inserted_id)}
            finally:
                client.close()
        except Exception as e:
            logger.error(f"MongoDB insert_document error: {e}")
            return {'success': False, 'error': str(e)}
