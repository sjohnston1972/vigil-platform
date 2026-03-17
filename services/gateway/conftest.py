import sys
import os
import pytest

_SERVICE_DIR = os.path.dirname(__file__)

# Ensure our service dir is on sys.path at collection time
if _SERVICE_DIR not in sys.path:
    sys.path.insert(0, _SERVICE_DIR)


@pytest.fixture(autouse=True)
def _pin_gateway_main():
    """Ensure sys.modules["main"] is this service's main.py for every test."""
    saved_main = sys.modules.pop("main", None)
    try:
        sys.path.remove(_SERVICE_DIR)
    except ValueError:
        pass
    sys.path.insert(0, _SERVICE_DIR)
    import main as _m
    sys.modules["main"] = _m
    yield
    sys.modules.pop("main", None)
    if saved_main is not None:
        sys.modules["main"] = saved_main
