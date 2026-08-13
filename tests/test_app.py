"""Tests for ``app.py``, run headlessly through Streamlit's own AppTest.

Step 8's acceptance is that *no* filter combination raises — a dashboard that
answers a filter with a traceback is worse than one that answers "no filings
match". That is a claim about behaviour under every combination, so it gets
swept rather than spot-checked.

The app is pointed at a small database built here, not at the committed
``data/h1b.db``. That keeps the suite fast, keeps it working before the loader
has ever been run, and lets a test choose data that produces an empty result
on purpose.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pandas as pd
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from src import clean, load, queries
from tests.test_load import cleaned
from tests.test_queries import DEFAULTS

# Absolute: AppTest.from_file resolves a relative path against the file that
# calls it, so "app.py" would mean tests/app.py.
APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture(autouse=True)
def _empty_cache():
    """Clear Streamlit's cache around every test.

    ``st.cache_data`` is global to the process, so without this one test's
    answers are served to the next one's database — and the test that points
    the app at a *missing* file passes because it is still holding results
    from a database that existed.
    """
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def app(tmp_path, monkeypatch):
    """The app, wired to a database with two titles across two cities.

    Twelve filings per city, because the sidebar's minimum-filings slider
    defaults to 10 — a smaller fixture makes the cities table correctly
    report that nothing qualifies, which reads as a broken app in a test
    named for the opposite.
    """
    # Laid out so each test has something to bite on:
    #   * Software Engineer (the default) has 12 filings, all in Austin, which
    #     clears the sidebar's minimum of 10 so the cities table draws.
    #   * Data Analyst has 14, so the *most filed* title is not the default —
    #     a picker that takes the first option would show the wrong one.
    #   * One Software Engineer filing is a $9M outlier, so the exclude toggle
    #     has something to exclude in the default view.
    #   * Both years appear, so the trend line has two points.
    engineers, analysts = 12, 14
    rows = engineers + analysts
    wages = [float(90_000 + i * 5_000) for i in range(rows)]
    wages[engineers - 1] = 9_000_000.0
    frame = pd.DataFrame(
        {
            **{k: [v] * rows for k, v in DEFAULTS.items()},
            "CASE_NUMBER": [f"I-200-25001-{i:06d}" for i in range(1, rows + 1)],
            "JOB_TITLE": (["Software Engineer"] * engineers
                          + ["Data Analyst"] * analysts),
            "WORKSITE_CITY": ["austin"] * engineers + ["portland"] * analysts,
            "WORKSITE_STATE": ["tx"] * engineers + ["or"] * analysts,
            "DECISION_DATE": ["2024-05-01", "2025-05-01"] * (rows // 2),
            "EMPLOYER_NAME": (["ZETA LABS"] * 9 + ["ALPHA WORKS"] * 7
                              + ["BETA GROUP"] * 10),
            "WAGE_RATE_OF_PAY_FROM": wages,
        }
    )
    path, _ = load.build(clean.clean(frame), tmp_path / "h1b.db")
    monkeypatch.setattr(queries, "DB_PATH", path)

    def run(**widgets):
        test = AppTest.from_file(APP, default_timeout=90)
        test.run()
        for name, value in widgets.items():
            _set(test, name, value)
        if widgets:
            test.run()
        return test

    return run


def _set(test: AppTest, name: str, value) -> None:
    """Drive one sidebar widget by its label."""
    for group in (test.text_input, test.selectbox, test.slider, test.checkbox):
        for widget in group:
            if widget.label == name:
                widget.set_value(value)
                return
    raise AssertionError(f"no widget labelled {name!r}")


def messages(test: AppTest) -> list[str]:
    """Everything the page said in an info, warning or error box."""
    return [element.value for element in (*test.info, *test.warning, *test.error)]


# --------------------------------------------------------------------------
# It renders at all
# --------------------------------------------------------------------------


def test_the_page_renders_with_no_filters_touched(app):
    test = app()
    assert not test.exception
    assert test.title[0].value == "H-1B Tech Salary Explorer"
    assert [m.label for m in test.metric] == [
        "Median offered wage", "25th percentile", "75th percentile", "Filings",
    ]


def test_it_opens_on_a_job_title_rather_than_the_whole_dataset(app):
    """Unfiltered queries take over a second; the picker must never start empty."""
    test = app()
    picker = next(s for s in test.selectbox if s.label == "Job title")
    assert picker.value == queries.DEFAULT_JOB_TITLE
    assert test.metric[3].value != "0"


def test_every_section_draws_when_there_is_data(app):
    test = app()
    assert [s.value for s in test.subheader] == [
        "Distribution", "By fiscal year", "Employers filing most often",
        "Where it pays most",
    ]
    assert len(test.dataframe) == 2
    assert len(test.get("plotly_chart")) == 2


def test_the_year_picker_offers_every_year_in_the_data(tmp_path, monkeypatch):
    """Not just the years the default job title happens to have filings in."""
    frame = cleaned(
        CASE_NUMBER=[f"I-200-25001-{i:06d}" for i in range(1, 4)],
        JOB_TITLE=["Bioinformatics Scientist"] * 3,
        DECISION_DATE=["2024-05-01", "2025-05-01", "2026-01-15"],
        WAGE_RATE_OF_PAY_FROM=[100_000.0, 110_000.0, 120_000.0],
    )
    path, _ = load.build(frame, tmp_path / "h1b.db")
    monkeypatch.setattr(queries, "DB_PATH", path)

    test = AppTest.from_file(APP, default_timeout=90)
    test.run()
    assert not test.exception
    years = next(s for s in test.selectbox if s.label == "Fiscal year")
    # AppTest reports options as the strings Streamlit displays, not the
    # values the app passed in.
    assert list(years.options) == ["All years", "2026", "2025", "2024"]


def test_the_footer_says_what_the_data_is_and_is_not(app):
    captions = " ".join(c.value for c in app().caption)
    assert "Department of Labor" in captions
    assert "not a salary survey" in captions


# --------------------------------------------------------------------------
# It never raises, whatever the filters say
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("widget", "value"),
    [
        ("City", "Nowhere"),
        ("City", "austin"),
        ("City", "'; DROP TABLE filings; --"),
        ("City", "%"),
        ("State", "ZZ"),
        ("State", "tx"),
        ("Search job titles", "zzzz"),
        ("Search job titles", "%"),
        ("Search job titles", "data"),
        ("Minimum filings per city", 100),
        ("Minimum filings per city", 1),
        ("Exclude flagged outliers", False),
        ("Fiscal year", 2025),
        ("Fiscal year", "All years"),
    ],
)
def test_no_single_filter_raises(app, widget, value):
    test = app(**{widget: value})
    assert not test.exception, test.exception


def test_a_filter_matching_nothing_explains_itself(app):
    test = app(City="Nowhere")
    assert not test.exception
    assert test.metric[3].value == "0"
    assert any("No filings match" in m for m in messages(test))


def test_an_empty_slice_stops_before_the_charts(app):
    """No point drawing four empty sections under a warning that says why."""
    test = app(City="Nowhere")
    assert test.subheader == []
    assert len(test.get("plotly_chart")) == 0


def test_a_threshold_no_city_can_meet_says_how_to_fix_it(app):
    test = app(**{"Minimum filings per city": 100})
    assert not test.exception
    assert any("Lower the threshold" in m for m in messages(test))


def test_a_title_search_matching_nothing_keeps_the_page_up(app):
    test = app(**{"Search job titles": "zzzz"})
    assert not test.exception
    assert any("No titles start with" in c.value for c in test.caption)
    assert test.metric[3].value != "0"


def test_the_outlier_toggle_changes_the_numbers_it_claims_to(app):
    """Default is on. The fixture holds one $9M filing, so unchecking adds it."""
    strict = int(app().metric[3].value.replace(",", ""))
    loose = int(
        app(**{"Exclude flagged outliers": False}).metric[3].value.replace(",", "")
    )
    assert loose == strict + 1


# --------------------------------------------------------------------------
# The empty-state guards, which main() short-circuits past
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section", ["distribution_chart", "employers_table", "trend_chart", "cities_table"]
)
def test_each_section_says_so_rather_than_drawing_nothing(app, section, monkeypatch):
    """Called directly, because main() returns before reaching them.

    The count check in main() means these guards are unreachable through the
    page — but they are the difference between a readable message and a chart
    of nothing if that check is ever moved or removed, which is exactly the
    kind of edit that gets made later.
    """
    import app as dashboard

    app()  # binds queries.DB_PATH to the fixture database
    shown: list[str] = []
    monkeypatch.setattr(dashboard.st, "info", lambda text: shown.append(text))
    monkeypatch.setattr(dashboard.st, "subheader", lambda text: None)

    getattr(dashboard, section)(
        {
            "job_title": "No Such Job",
            "city": None,
            "state": None,
            "fiscal_year": None,
            "include_outliers": False,
            "min_filings": 10,
        }
    )
    assert shown, f"{section} drew nothing and said nothing"


# --------------------------------------------------------------------------
# The failure a visitor cannot diagnose
# --------------------------------------------------------------------------


def _broken(kind: str, folder: Path) -> Path:
    """One of the four ways this database reaches a reader unusable."""
    path = folder / "h1b.db"
    if kind == "missing":
        return path
    if kind == "a directory":
        path.mkdir()
    elif kind == "not a database":
        path.write_text("hello")
    elif kind == "valid but empty":
        # What an interrupted build leaves: structurally perfect, no tables.
        connection = sqlite3.connect(path)
        connection.execute("VACUUM")
        connection.close()
    elif kind == "truncated":
        load.build(cleaned(CASE_NUMBER=["I-200-25001-000001"]), path)
        whole = path.read_bytes()
        path.write_bytes(whole[: len(whole) // 2])
    return path


@pytest.mark.parametrize(
    "kind", ["missing", "a directory", "not a database", "valid but empty", "truncated"]
)
def test_an_unusable_database_is_a_message_not_a_traceback(tmp_path, monkeypatch, kind):
    """All four failures, not just the missing file.

    "valid but empty" is the one that actually happened on a real machine, and
    "truncated" is what an incomplete clone of the committed 78 MB file looks
    like. Both used to reach the browser as a pandas traceback with the SQL in
    it — the exact thing Step 8 says must not happen.
    """
    monkeypatch.setattr(queries, "DB_PATH", _broken(kind, tmp_path))
    test = AppTest.from_file(APP, default_timeout=90)
    test.run()

    assert not test.exception, test.exception
    said = " ".join(messages(test)).lower()
    assert "database" in said
    assert "select" not in said, "the SQL leaked into the message"
