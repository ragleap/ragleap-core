"""Engine registry (docs/design/api.md sections 5 and 6). Standard library only.

Built-in engines are registered by name. A third-party engine runs arbitrary code from an installed package, so it is never a
candidate for automatic selection: it runs only when the caller names it. Ranking by a routing table comes in a later change; for
now candidates are ordered by name.
"""
import re

from .engine import KINDS, Capabilities, EngineInfo
from .errors import EngineNotInstalledError

_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_METHODS = ("load", "run", "close")


class Registry:
    def __init__(self):
        self._items = {}

    def register(self, engine, *, third_party=False):
        name = getattr(engine, "name", None)
        if not isinstance(name, str) or not _NAME.match(name):
            raise ValueError("engine name must be lower case letters, digits, '-' or '_', got %r" % (name,))
        entries = getattr(engine, "manifest_entries", None)
        if isinstance(entries, str) or not isinstance(entries, tuple) or not all(isinstance(e, str) for e in entries):
            raise TypeError("manifest_entries must be a tuple of str")
        if not isinstance(getattr(engine, "capabilities", None), Capabilities):
            raise TypeError("capabilities must be a Capabilities object")
        for method in _METHODS:
            if not callable(getattr(engine, method, None)):
                raise TypeError("engine must have a callable %s()" % method)
        if name in self._items:
            raise ValueError("an engine named %r is already registered" % name)
        self._items[name] = (engine, bool(third_party))

    def get(self, name):
        try:
            return self._items[name][0]
        except KeyError:
            known = ", ".join(sorted(self._items)) or "none"
            raise EngineNotInstalledError("no engine named %r is registered (registered: %s)." % (name, known),
                                          hint="engines arrive as optional extras; install the one you need, or check the name.")

    def info(self, kind=None):
        if kind is not None and kind not in KINDS:
            raise ValueError("kind must be one of %s or None, got %r" % (", ".join(KINDS), kind))
        out = []
        for name in sorted(self._items):
            engine, third = self._items[name]
            if kind is None or engine.capabilities.kind == kind:
                out.append(EngineInfo(name, engine.capabilities.kind, engine.capabilities, engine.manifest_entries, third))
        return out

    def auto_candidates(self, kind, language):
        """Built-in engines of this kind that declare the language, ordered by name. Third-party engines are never returned."""
        if kind not in KINDS:
            raise ValueError("kind must be one of %s, got %r" % (", ".join(KINDS), kind))
        return [e for name, (e, third) in sorted(self._items.items())
                if not third and e.capabilities.kind == kind and language in e.capabilities.languages]


_DEFAULT = Registry()


def register_engine(engine, *, third_party=False):
    _DEFAULT.register(engine, third_party=third_party)


def engines(kind=None):
    """What is registered, with capabilities."""
    return _DEFAULT.info(kind)
