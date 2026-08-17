"""Azure-side ETL helpers.

Phase 2 only. Nothing in :mod:`src.etl` is imported by the Phase 1 pipeline or
by ``app.py``, so a clone with no Azure account and no Azure SDK installed still
runs the full test suite and the dashboard.
"""
