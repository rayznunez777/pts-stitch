"""
A stand-in for wxPython.

Ink/Stitch imports its GUI modules whenever any extension loads, even the
headless ones, so wx has to exist for the import to succeed. It is never used
on this path. Shipping the real wxPython and its GTK stack costs several
hundred megabytes for code that never runs.

Every attribute resolves to a permissive dummy class, so module-level
subclassing and constant lookups both succeed.
"""
import sys
import types
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec

__version__ = "4.2.1-stub"
__path__ = []


class _Stub:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, *args, **kwargs):
        return _Stub()

    def __getattr__(self, name):
        return _Stub()

    def __or__(self, other):
        return self

    def __ror__(self, other):
        return self

    def __int__(self):
        return 0

    def __bool__(self):
        return False


_made = {}


def __getattr__(name):
    if name not in _made:
        _made[name] = type(name, (_Stub,), {})
    return _made[name]


class _SubLoader(Loader):
    def create_module(self, spec):
        module = types.ModuleType(spec.name)
        module.__dict__["__getattr__"] = __getattr__
        module.__path__ = []
        return module

    def exec_module(self, module):
        pass


class _Finder(MetaPathFinder):
    """Satisfy any `import wx.anything` as well."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("wx."):
            return ModuleSpec(fullname, _SubLoader(), is_package=True)
        return None


sys.meta_path.append(_Finder())
