"""aix — Universal AI Agent Control Plane."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("aix")
except PackageNotFoundError:  # pragma: no cover - running from an uninstalled source tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
