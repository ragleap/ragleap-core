"""
Base classes for RagLeap Core's data source integrations.
Ported from production's api/addon_models.py + api/addon_services.py —
Django model fields replaced with a plain dataclass, Fernet encryption
kept (same library, no Django dependency), workspace scoping removed
(single-tenant).
"""
import os
import re
import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, Tuple, List, Optional

logger = logging.getLogger(__name__)

try:
    from cryptography.fernet import Fernet
    _FERNET_AVAILABLE = True
except ImportError:
    _FERNET_AVAILABLE = False


def _get_fernet():
    """Return a Fernet instance from ADDON_ENCRYPTION_KEY, or None if unavailable/unset."""
    if not _FERNET_AVAILABLE:
        return None
    key = os.environ.get("ADDON_ENCRYPTION_KEY")
    if not key:
        return None
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt_value(value: Optional[str]) -> Optional[str]:
    """Encrypt a value for storage. Returns the plaintext unchanged if no key is configured."""
    if value is None:
        return value
    fernet = _get_fernet()
    if fernet:
        return fernet.encrypt(value.encode()).decode()
    return value


class MissingEncryptionKey(ValueError):
    """Raised instead of storing a credential in plain text."""


def encrypt_secret(value: Optional[str]) -> Optional[str]:
    """Encrypt a credential for storage. Unlike encrypt_value it never falls back to plain text:
    without ADDON_ENCRYPTION_KEY (or the cryptography library) it refuses to store the secret."""
    if not value:
        return value
    fernet = _get_fernet()
    if not fernet:
        raise MissingEncryptionKey(
            "ADDON_ENCRYPTION_KEY is not set, so credentials cannot be stored. Set the key and try again."
        )
    return fernet.encrypt(value.encode()).decode()


_SENSITIVE_HEADER = re.compile(r"auth|cookie|token|secret|password|key|credential", re.IGNORECASE)


def protect_headers(headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Headers as stored. If any header name looks like a credential, all headers are stored encrypted
    (refused when there is no key); otherwise they are stored as given."""
    headers = headers or {}
    if any(_SENSITIVE_HEADER.search(str(name)) for name in headers):
        return {"__enc__": encrypt_secret(json.dumps(headers))}
    return headers


def reveal_headers(stored) -> Dict[str, str]:
    """Headers as the connector uses them: decrypts the form protect_headers writes, passes older rows through."""
    if isinstance(stored, dict) and set(stored) == {"__enc__"}:
        text = decrypt_value(stored["__enc__"])
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            logger.error("Stored connector headers could not be decrypted (key mismatch or damaged data).")
            return {}
    return stored or {}


def decrypt_value(value: Optional[str]) -> Optional[str]:
    """Decrypt a stored value. Returns the value unchanged if no key is configured."""
    if value is None:
        return value
    fernet = _get_fernet()
    if fernet:
        try:
            return fernet.decrypt(value.encode()).decode()
        except Exception:
            logger.error(
                "Decryption failed for a data source credential — possible "
                "ADDON_ENCRYPTION_KEY mismatch or corrupted data."
            )
            return "[DECRYPTION_FAILED]"
    return value


@dataclass
class DataSource:
    """
    Plain data holder replacing the Django ExternalDataSource model.
    Field names match production exactly so ported connector logic
    (self.data_source.connection_string, etc.) works unmodified.
    """
    id: str
    name: str
    source_type: str
    connection_string: Optional[str] = None
    api_endpoint: Optional[str] = None
    api_key: Optional[str] = None
    api_headers: Dict[str, str] = field(default_factory=dict)
    query_template: Optional[str] = None
    field_mappings: Dict[str, str] = field(default_factory=dict)
    user_identifier_field: str = "user_id"
    documents_table_name: str = "documents"
    csv_content: Optional[str] = None
    csv_filename: Optional[str] = None


class BaseDatabaseConnector(ABC):
    """Abstract base class for all data source connectors."""

    # Tables/collections that must never be exposed, regardless of source type
    BLACKLISTED_TABLE_PATTERNS = [
        'auth_', 'django_', 'api_key', 'secret', 'password', 'token',
        'session', 'payment', 'credit_card', 'billing_alert', 'admin_',
        'oauth', 'social_auth', 'celery_',
    ]

    SENSITIVE_COLUMN_PATTERNS = [
        'password', 'secret', 'token', 'api_key', 'private_key',
        'credit_card', 'ssn', 'social_security', 'cvv', 'pin',
        'encryption_key', 'salt', 'hash',
    ]

    def __init__(self, data_source: DataSource):
        self.data_source = data_source

    @abstractmethod
    def test_connection(self) -> Tuple[bool, str]:
        """Test the connection. Returns (success, message)."""
        pass

    @abstractmethod
    def fetch_data(self, user_identifier: str = None) -> List[Dict]:
        """Fetch data from the external source."""
        pass

    def introspect_schema(self) -> Dict[str, Any]:
        """Discover tables/collections with blacklist filtering. Override in subclass."""
        return {'tables': [], 'filtered_count': 0, 'message': 'Schema introspection not supported for this source type'}

    def check_required_schema(self) -> Dict[str, Any]:
        """Check if RagLeap's required tables exist in the user's database."""
        return {'supported': False, 'message': 'Schema check not supported for this source type'}

    def create_required_schema(self) -> Dict[str, Any]:
        """Create RagLeap's required tables in the user's database."""
        return {'supported': False, 'message': 'Schema creation not supported for this source type'}

    def insert_document(self, table_name: str, title: str, content: str,
                        metadata: Optional[Dict] = None, source_type: str = 'upload') -> Dict[str, Any]:
        """Insert an uploaded document into the user's external database."""
        return {'success': False, 'error': 'Insert not supported for this source type'}

    REQUIRED_TABLES = {
        'ragleap_documents': {
            'description': 'Source documents ingested into the RAG pipeline',
            'columns': [
                ('id', 'UUID / Primary Key'),
                ('source_id', 'Text — external reference ID'),
                ('title', 'Text — document title'),
                ('content', 'Text — full document body'),
                ('metadata', 'JSON — extra attributes'),
                ('created_at', 'Timestamp'),
            ],
        },
        'ragleap_chunks': {
            'description': 'Text chunks split from documents for embedding',
            'columns': [
                ('id', 'UUID / Primary Key'),
                ('document_id', 'UUID — FK -> ragleap_documents'),
                ('chunk_text', 'Text — chunk content'),
                ('chunk_index', 'Integer — position in document'),
                ('metadata', 'JSON — chunk attributes'),
                ('created_at', 'Timestamp'),
            ],
        },
        'ragleap_embeddings': {
            'description': 'Vector embeddings for similarity search',
            'columns': [
                ('id', 'UUID / Primary Key'),
                ('chunk_id', 'UUID — FK -> ragleap_chunks'),
                ('embedding', 'vector(3072) — embedding array'),
                ('created_at', 'Timestamp'),
            ],
        },
    }

    def _is_table_blacklisted(self, table_name: str) -> bool:
        lower_name = table_name.lower()
        for pattern in self.BLACKLISTED_TABLE_PATTERNS:
            if lower_name.startswith(pattern) or pattern in lower_name:
                return True
        return False

    def _is_column_sensitive(self, column_name: str) -> bool:
        lower_name = column_name.lower()
        for pattern in self.SENSITIVE_COLUMN_PATTERNS:
            if pattern in lower_name:
                return True
        return False
