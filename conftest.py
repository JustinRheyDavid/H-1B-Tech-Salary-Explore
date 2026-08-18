"""Makes ``pytest`` work from a clean checkout, and defines the Azure skip policy.

The tests import ``src.clean``, which requires the repository root on
``sys.path``. ``python -m pytest`` puts it there and bare ``pytest`` does not,
so without this file the suite passes one way and fails the other — the worst
possible outcome for someone running it for the first time.

Its presence at the root is enough for that: pytest adds the directory
containing it to ``sys.path``, which is also what lets the test modules do
``from conftest import unavailable``.
"""

from __future__ import annotations

import os

import pytest

#: Set ``REQUIRE_AZURE=1`` to turn every Azure skip into a failure.
#:
#: The skips exist so a clone with no Azure account still runs green, and that
#: is right for a contributor. It is wrong for the one CI job whose purpose is
#: to exercise Azure: a paused database, an expired federated credential or a
#: missing ODBC driver would each report success while testing nothing.
#:
#: **It lives here, not in one test module, because it has to cover both.** An
#: earlier version defined it inside ``tests/test_db.py``, which left the four
#: ``tests/test_blob.py`` cases free to skip silently under a flag documented as
#: preventing exactly that — measured as ``1 failed, 16 passed, 3 skipped``
#: against an unreachable storage account. One definition, both suites.
REQUIRE_AZURE = "REQUIRE_AZURE"


def azure_is_mandatory() -> bool:
    """True when the environment says the Azure tests must actually run."""
    return os.environ.get(REQUIRE_AZURE, "").strip().lower() in {"1", "true", "yes"}


def unavailable(reason: str):
    """Skip, or fail if the environment said these tests are mandatory.

    Always raises, so callers can treat it as terminal.
    """
    if azure_is_mandatory():
        pytest.fail(f"{REQUIRE_AZURE} is set, but the Azure tests cannot run: {reason}")
    pytest.skip(reason)


def require_module(name: str, reason: str):
    """``importorskip``, but honouring :data:`REQUIRE_AZURE`.

    ``pytest.importorskip`` at module scope runs at *collection*, before any
    fixture can intervene, so a missing SDK skips a whole file no matter what
    the flag says. This is the collection-time equivalent.
    """
    try:
        __import__(name)
    except ImportError as exc:
        unavailable(f"{reason}: {exc}")


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: needs the 850 MB source data and its Parquet cache. Skipped "
        'automatically when absent; run "pytest -m \'not slow\'" to skip it '
        "even when present.",
    )
    config.addinivalue_line(
        "markers",
        "azure: talks to the real storage account or the real Azure SQL "
        "database. Skipped automatically when the SDK is missing, nobody is "
        "logged in, or the resource is unreachable, so a clone with no Azure "
        "account still runs green. Set REQUIRE_AZURE=1 to make those skips "
        "failures instead — see the note above.",
    )
