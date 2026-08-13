"""What tech jobs in the US actually pay, from H-1B wage filings.

Layout and rendering only. Every number on the page comes from ``src.queries``
and no SQL appears here — that separation is the point of the file split, and
it is the first thing a reviewer checks.

Two rules this page is built around:

*A filter always renders something.* Any combination of title, city, state and
year can select nothing at all, and a dashboard that answers with a traceback
is worse than one that answers "no filings match". Every section checks its
own frame before drawing.

*A job title is always selected.* Unfiltered, the queries rank all 850,321
rows and take over a second; with a title they answer in tens of milliseconds.
The picker therefore starts on :data:`queries.DEFAULT_JOB_TITLE` and has no
empty state.
"""

from __future__ import annotations

import functools
import sqlite3

import pandas as pd
import plotly.express as px
import streamlit as st

from src import load, queries

# Column formatting is declared to Streamlit rather than applied with pandas'
# Styler: .style needs jinja2, which is not a dependency of this project, and
# formatting the values into strings would break sorting in the table.
_MONEY_COLUMN = st.column_config.NumberColumn(format="dollar")
_COUNT_COLUMN = st.column_config.NumberColumn(format="localized")

DOWNLOADED = "6 August 2026"
SOURCE_URL = "https://www.dol.gov/agencies/eta/foreign-labor/performance"

st.set_page_config(page_title="H-1B Tech Salary Explorer", page_icon="📊", layout="wide")

# Streamlit reruns this file top to bottom on every widget change, so without
# caching each keystroke in the title box would re-query. The database is
# read-only and committed, so a cached answer can never go stale.
_cache = st.cache_data(show_spinner=False)

percentiles = _cache(queries.salary_percentiles)
distribution = _cache(queries.wage_distribution)
employers = _cache(queries.top_employers)
by_city = _cache(queries.salary_by_city)
trend = _cache(queries.salary_trend)
titles = _cache(queries.title_search)


fiscal_years = _cache(queries.fiscal_years)


def _card(section):
    """Draw a section inside a bordered container.

    A decorator rather than a `with` block inside each function, so that the
    card is declared once next to the section's name instead of indenting
    every body by four spaces and inviting one of them to be forgotten.
    """

    @functools.wraps(section)
    def draw(*args, **kwargs):
        with st.container(border=True):
            return section(*args, **kwargs)

    return draw


def style(figure):
    """Strip a Plotly figure back to the data.

    The default template draws a filled plot area, a box around it and a grid
    in both directions. On a page made of white cards that is three competing
    rectangles per chart. Horizontal gridlines survive because reading a value
    off a bar needs them; nothing else does.
    """
    figure.update_layout(
        margin=dict(t=8, b=8, l=8, r=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        hoverlabel=dict(bgcolor="white", font_size=13),
        showlegend=False,
    )
    figure.update_xaxes(showgrid=False, showline=False, zeroline=False)
    figure.update_yaxes(
        showgrid=True, gridcolor="#EDEFF2", showline=False, zeroline=False
    )
    return figure


def money(value: float | None) -> str:
    """``$130,000``, or an em dash when there is nothing to show."""
    if value is None or pd.isna(value):
        return "—"
    return f"${value:,.0f}"


def sidebar() -> dict:
    """Collect every filter. Returns the arguments the query layer expects."""
    st.sidebar.header("Filters")

    typed = st.sidebar.text_input(
        "Search job titles", placeholder="data, software, analyst…"
    )
    # Ordered by how often each title is filed, so the first entry is the one
    # most people want. An unmatched search leaves the previous title in place
    # rather than emptying the page.
    options = titles(typed, 50) or [queries.DEFAULT_JOB_TITLE]
    if typed and not titles(typed, 50):
        st.sidebar.caption(f"No titles start with “{typed}”. Showing the default.")

    default = (
        options.index(queries.DEFAULT_JOB_TITLE)
        if queries.DEFAULT_JOB_TITLE in options
        else 0
    )
    job_title = st.sidebar.selectbox("Job title", options, index=default)

    city = st.sidebar.text_input("City", placeholder="Austin").strip() or None
    state = st.sidebar.text_input("State", max_chars=2, placeholder="TX")
    state = state.strip() or None

    year = st.sidebar.selectbox("Fiscal year", ["All years", *fiscal_years()])
    min_filings = st.sidebar.slider(
        "Minimum filings per city", 1, 100, 10,
        help="Cities with fewer filings are hidden: a median over three "
             "salaries swings by tens of thousands on one row.",
    )
    include_outliers = not st.sidebar.checkbox(
        "Exclude flagged outliers", value=True,
        help="37 filings annualize below $10,000 or above $2,000,000. They are "
             "kept in the database and excluded from these figures by default.",
    )

    return {
        "job_title": job_title,
        "city": city,
        "state": state,
        "fiscal_year": None if year == "All years" else int(year),
        "include_outliers": include_outliers,
        "min_filings": min_filings,
    }


def headline(filters: dict) -> int:
    """The three metric cards. Returns the filing count the rest of the page uses."""
    row = percentiles(
        filters["job_title"], filters["city"], filters["state"],
        filters["fiscal_year"], filters["include_outliers"],
    ).iloc[0]

    count = int(row["n_filings"])
    cards = [
        ("Median offered wage", money(row["p50"])),
        ("25th percentile", money(row["p25"])),
        ("75th percentile", money(row["p75"])),
        ("Filings", f"{count:,}"),
    ]
    for column, (label, value) in zip(st.columns(4), cards):
        with column.container(border=True):
            st.metric(label, value)
    return count


@_card
def distribution_chart(filters: dict) -> None:
    st.subheader("Distribution")
    frame = distribution(
        filters["job_title"], filters["city"], filters["state"],
        filters["fiscal_year"], filters["include_outliers"],
    )
    if frame.empty:
        st.info("No filings match these filters.")
        return
    figure = px.bar(
        frame, x="bin_floor", y="n_filings",
        labels={"bin_floor": "Offered wage (USD/year)", "n_filings": "Filings"},
    )
    figure.update_layout(bargap=0.05)
    style(figure)

    # Every bar is in the figure; only the initial view is narrowed. The top
    # 0.5% of Software Engineer filings reach $1.9M, which stretches the axis
    # fivefold and squashes the 60k-300k range everyone came to look at into a
    # sliver. Zooming out in the chart still shows the tail.
    cap, beyond = _visible_range(frame)
    figure.update_xaxes(range=[0, cap])
    st.plotly_chart(figure, width="stretch")
    if beyond:
        st.caption(
            f"{beyond:,} filings above ${cap:,.0f} are outside this view — "
            "double-click the chart to see the full range."
        )


def _visible_range(frame: pd.DataFrame, share: float = 0.995) -> tuple[int, int]:
    """Where to cut the x-axis, and how many filings that leaves off screen."""
    counts = frame["n_filings"]
    reached = counts.cumsum() / counts.sum()
    inside = frame.loc[reached >= share, "bin_floor"]
    if inside.empty:
        return int(frame["bin_floor"].max()), 0
    cap = int(inside.iloc[0])
    return cap, int(frame.loc[frame["bin_floor"] > cap, "n_filings"].sum())


@_card
def employers_table(filters: dict) -> None:
    st.subheader("Employers filing most often")
    frame = employers(filters["job_title"], filters["city"], 20)
    if frame.empty:
        st.info("No employers match these filters.")
        return
    st.dataframe(
        frame.rename(columns={
            "employer_name": "Employer",
            "n_filings": "Filings",
            "median_wage": "Median wage",
        }),
        column_config={"Median wage": _MONEY_COLUMN, "Filings": _COUNT_COLUMN},
        width="stretch", hide_index=True,
    )


@_card
def cities_table(filters: dict) -> None:
    st.subheader("Where it pays most")
    frame = by_city(filters["job_title"], filters["min_filings"])
    if frame.empty:
        st.info(
            f"No city has at least {filters['min_filings']} filings for this "
            "title. Lower the threshold in the sidebar."
        )
        return
    frame = frame.assign(
        Location=frame["worksite_city"] + ", " + frame["worksite_state"]
    )
    st.dataframe(
        frame[["Location", "median_wage", "n_filings"]]
        .rename(columns={"median_wage": "Median wage", "n_filings": "Filings"})
        .head(25),
        column_config={"Median wage": _MONEY_COLUMN, "Filings": _COUNT_COLUMN},
        width="stretch", hide_index=True,
    )


@_card
def trend_chart(filters: dict) -> None:
    st.subheader("By fiscal year")
    frame = trend(filters["job_title"], filters["city"])
    if frame.empty:
        st.info("No filings match these filters.")
        return

    figure = px.line(
        frame, x="fiscal_year", y="median_wage", markers=True,
        labels={"fiscal_year": "Fiscal year", "median_wage": "Median wage (USD/year)"},
    )
    style(figure)
    figure.update_xaxes(tickmode="array", tickvals=frame["fiscal_year"])
    st.plotly_chart(figure, width="stretch")

    changes = frame.dropna(subset=["yoy_pct_change"])
    if not changes.empty:
        st.caption(
            "Year on year: "
            + ", ".join(
                f"FY{int(row.fiscal_year)} {row.yoy_pct_change:+.1f}%"
                for row in changes.itertuples()
            )
            + ". FY2026 covers two quarters where the others cover four, so its "
            "filing count is not comparable — the median is."
        )


def footer() -> None:
    st.divider()
    st.caption(
        f"Source: US Department of Labor, Office of Foreign Labor Certification "
        f"— LCA disclosure data, nine quarterly files covering October 2023 to "
        f"March 2026, downloaded {DOWNLOADED}. [Source page]({SOURCE_URL})."
    )
    st.caption(
        "These are wages employers committed to on a federal form, for roles "
        "they sought to fill with H-1B, E-3 or H-1B1 workers. They are not a "
        "salary survey: they exclude everyone not sponsored, skew toward "
        "employers large enough to run an immigration process, and record what "
        "was offered rather than what was ultimately paid."
    )


# Every way the database can fail a reader. `load.connect` raises all of these
# with a message that already says what to do, so they are shown as written
# rather than translated. pandas wraps a failed query in its own DatabaseError
# and pastes the SQL into the message, which is the one case worth replacing.
DATABASE_TROUBLE = (FileNotFoundError, IsADirectoryError, sqlite3.DatabaseError)


def page() -> None:
    """The whole dashboard. Raises if the database cannot answer."""
    filters = sidebar()
    count = headline(filters)
    if count == 0:
        st.warning(
            "No filings match these filters. Try a different city, or clear the "
            "city and state to see the whole country."
        )
        footer()
        return

    left, right = st.columns(2)
    with left:
        distribution_chart(filters)
    with right:
        trend_chart(filters)

    employers_table(filters)
    cities_table(filters)
    footer()


def main() -> None:
    st.title("H-1B Tech Salary Explorer")
    st.caption(
        "850,321 tech filings from US Department of Labor wage disclosures, "
        "cleaned and normalized to annual USD."
    )

    try:
        page()
    except DATABASE_TROUBLE as trouble:
        # Around the whole page, not just the first query: a visitor cannot act
        # on a traceback, and this is the failure they are most likely to meet
        # — a clone that transferred the 78 MB database incompletely.
        st.error(f"The database cannot be read. {trouble}")
    except pd.errors.DatabaseError:
        st.error(
            "The database could not answer that query. It may be incomplete — "
            "rebuild it with `python -m src.load`."
        )


if __name__ == "__main__":
    main()
