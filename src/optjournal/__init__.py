"""optjournal -- options and equity journal backed by IBKR Flex.

Importing this package installs the py_ibkr compatibility shim, so any entry
point (CLI, tests, notebooks) is protected against unknown IBKR trade codes
aborting a parse. See compat.py for why this is necessary.
"""

from importlib.metadata import version as _version

#: From the installed metadata, so `pyproject.toml` is the one place a release
#: bumps. A second literal here is a second number to forget, and the updater
#: compares against the file.
__version__ = _version("optjournal")

from optjournal.compat import install_code_fallback

install_code_fallback()

__all__ = ["__version__", "install_code_fallback"]
