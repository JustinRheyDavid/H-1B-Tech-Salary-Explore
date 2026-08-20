"""The contract between the Bicep templates and the code that reads them.

Two artifacts have to agree on a set of strings and nothing connects them:
`infra/containerapps.bicep` writes environment variables onto the containers,
and `src/` reads them by name. Rename a constant and the template silently stops
configuring the container — which then **falls back to the hardcoded default and
works on this deployment**, so the break only surfaces for the stranger
redeploying into their own subscription, which is Step 12's whole criterion.

That is the shape of failure this project keeps finding: correct-looking, silent,
and outside what any existing test could observe. These tests are cheap and they
close it.

Text-matching rather than parsing: Bicep has no Python parser, and the thing
worth asserting is that the literal strings agree.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src import db
from src.db import azure_impl
from src.etl import blob

BICEP = Path(__file__).resolve().parent.parent / "infra" / "containerapps.bicep"


@pytest.fixture(scope="module")
def bicep() -> str:
    return BICEP.read_text()


def _env_names(source: str, variable: str) -> set[str]:
    """Every ``name:`` inside one ``var <variable> = [ ... ]`` block."""
    match = re.search(rf"var {variable} = \[(.*?)\n\]", source, re.DOTALL)
    assert match, f"{variable} not found in {BICEP.name}"
    return set(re.findall(r"name:\s*'([^']+)'", match.group(1)))


def test_the_dashboard_gets_the_variables_it_reads(bicep):
    """`app.py` picks its backend from one of these and finds the server with two."""
    assert _env_names(bicep, "webEnvironment") == {
        db.BACKEND_ENV,
        azure_impl._SERVER_ENV,
        azure_impl._DATABASE_ENV,
    }


def test_the_etl_job_gets_the_variables_it_reads(bicep):
    """It had none of these at first, and ran on names that exist only here.

    ``H1B_STORAGE_ACCOUNT`` is the one that matters most: without it the job
    reads ``blob._DEFAULT_ACCOUNT``, which carries this deployment's random
    suffix, so a redeploy elsewhere would have the job downloading somebody
    else's caches — or failing to.
    """
    assert _env_names(bicep, "etlEnvironment") == {
        azure_impl._SERVER_ENV,
        azure_impl._DATABASE_ENV,
        blob._ACCOUNT_ENV,
    }


def test_no_deployment_specific_name_is_hardcoded_in_the_template(bicep):
    """The FQDN and account name must come from module outputs, not literals.

    They carry a per-deployment random suffix. A literal here would work
    perfectly for this subscription and silently point somebody else's job at
    these resources.
    """
    for literal in (azure_impl._DEFAULT_SERVER, azure_impl._DEFAULT_DATABASE,
                    blob._DEFAULT_ACCOUNT):
        assert literal not in bicep, (
            f"{literal!r} is hardcoded in {BICEP.name}; pass it from a module output"
        )


MAIN = BICEP.parent / "main.bicep"
DEPLOY = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "deploy.yml"


def test_the_role_assignment_defaults_to_being_created():
    """`assignRoles` must default to true, or a stranger's deploy skips the grant.

    CI passes `false` because its principal is Contributor and cannot write role
    assignments. That is a concession to one caller, not the intended behaviour:
    somebody deploying into their own subscription as their own owner runs the
    template with no parameters and must get the ETL job's blob access. Drop the
    default and their job fails at runtime with AuthorizationPermissionMismatch,
    long after the deployment reported success.
    """
    main = MAIN.read_text()
    assert re.search(r"param assignRoles bool = true\b", main), (
        "assignRoles must exist and default to true in main.bicep"
    )
    assert "module roles 'roles.bicep' = if (assignRoles)" in main


def test_ci_skips_the_role_assignment_it_cannot_write():
    """The other half: without this, every push to main goes red.

    Observed once already — run 32311438822 deployed the container app and then
    failed on `Microsoft.Authorization/roleAssignments/write`.
    """
    assert "assignRoles=false" in DEPLOY.read_text()


def test_the_home_ip_is_not_committed():
    """A home IP address does not belong in a public repository.

    ``sql.bicep`` takes it as a parameter defaulting to empty and the live rule
    is created out-of-band. This asserts the default stayed empty rather than
    someone filling it in for convenience.
    """
    sql = (BICEP.parent / "sql.bicep").read_text()
    addresses = set(re.findall(r"\b(?:\d{1,3}\.){3}\d{1,3}\b", sql))
    # 0.0.0.0 is the documented special case meaning "Azure services".
    assert addresses <= {"0.0.0.0"}, f"real IP addresses in sql.bicep: {addresses}"
