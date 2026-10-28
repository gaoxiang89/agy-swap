try:  # keep the reported version in lock-step with the installed distribution
    from importlib.metadata import PackageNotFoundError, version as _dist_version

    try:
        __version__ = _dist_version("agy-swap")
    except PackageNotFoundError:  # running from a source checkout
        __version__ = "0.2.0"
except Exception:  # pragma: no cover - very old interpreters
    __version__ = "0.2.0"

__all__ = ["__version__"]
