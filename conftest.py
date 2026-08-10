"""Makes ``pytest`` work from a clean checkout.

The tests import ``src.clean``, which requires the repository root on
``sys.path``. ``python -m pytest`` puts it there and bare ``pytest`` does not,
so without this file the suite passes one way and fails the other — the worst
possible outcome for someone running it for the first time.

Its presence at the root is enough for that: pytest adds the directory
containing it to ``sys.path``. The marker below is the only configuration.
"""


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "slow: needs the 850 MB source data and its Parquet cache. Skipped "
        'automatically when absent; run "pytest -m \'not slow\'" to skip it '
        "even when present.",
    )
