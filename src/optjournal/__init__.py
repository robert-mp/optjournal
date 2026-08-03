"""optjournal -- options and equity journal backed by IBKR Flex.

Importing this package installs the py_ibkr compatibility shim, so any entry
point (CLI, tests, notebooks) is protected against unknown IBKR trade codes
aborting a parse. See compat.py for why this is necessary.
"""

__version__ = "0.1.0"

from optjournal.compat import install_code_fallback

install_code_fallback()

__all__ = ["__version__", "install_code_fallback"]
