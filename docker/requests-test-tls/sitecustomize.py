"""Trust the pinned local Requests test CA without disabling TLS verification."""

from __future__ import annotations

import os
import sys
from pathlib import Path

bundle = os.environ.get("TRACEFIX_TEST_CA_BUNDLE")
checkout = Path.cwd()
if bundle and (checkout / "requests" / "adapters.py").is_file():
    sys.path.insert(0, str(checkout))
    import requests.adapters
    import requests.utils

    requests.adapters.DEFAULT_CA_BUNDLE_PATH = bundle
    requests.utils.DEFAULT_CA_BUNDLE_PATH = bundle
