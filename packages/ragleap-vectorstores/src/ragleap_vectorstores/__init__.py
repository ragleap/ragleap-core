"""
ragleap-vectorstores: pluggable vector backends beyond ragleap-rag core's 6.

Each backend import is wrapped in try/except ImportError, matching
ragleap-rag's own vectorstores/__init__.py pattern - a missing optional
extra simply makes that one backend unavailable rather than breaking
the package.
"""
from ragleap.vectorstores.base import VectorBackend

__version__ = "0.7.0"

__all__ = ["VectorBackend", "__version__"]

try:
    from ragleap_vectorstores.chroma_backend import ChromaBackend
    __all__.append("ChromaBackend")
except ImportError:
    pass  # chroma extra not installed - ChromaBackend simply unavailable, not an error

try:
    from ragleap_vectorstores.lancedb_backend import LanceDBBackend
    __all__.append("LanceDBBackend")
except ImportError:
    pass  # lancedb extra not installed - LanceDBBackend simply unavailable, not an error

try:
    from ragleap_vectorstores.redis_backend import RedisBackend
    __all__.append("RedisBackend")
except ImportError:
    pass  # redis extra not installed - RedisBackend simply unavailable, not an error

try:
    from ragleap_vectorstores.upstash_backend import UpstashBackend
    __all__.append("UpstashBackend")
except ImportError:
    pass  # upstash extra not installed - UpstashBackend simply unavailable, not an error

try:
    from ragleap_vectorstores.opensearch_backend import OpenSearchBackend
    __all__.append("OpenSearchBackend")
except ImportError:
    pass  # opensearch extra not installed - OpenSearchBackend simply unavailable, not an error
