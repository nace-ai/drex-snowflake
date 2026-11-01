"""sql/setup.sql against the handler module: what can be checked without a Snowflake account."""

import re
import tomllib
from pathlib import Path

import pandas
import pytest

import drex_snowflake

ROOT = Path(__file__).resolve().parents[1]
SETUP = (ROOT / "sql" / "setup.sql").read_text()
FUNCTION = re.compile(
    r"CREATE OR REPLACE FUNCTION (?P<name>\w+)\((?P<args>[^)]*)\)\s+RETURNS (?P<returns>\w+)\s+LANGUAGE PYTHON"
    r"(?P<props>.*?)AS \$\$\n(?P<body>.*?)\$\$;",
    re.DOTALL,
)
FUNCTIONS = {m["name"]: m for m in FUNCTION.finditer(SETUP)}
#: The SQL overloads taking a VARCHAR input or state, which call the Python function with it as a VARIANT
OVERLOAD = re.compile(
    r"CREATE OR REPLACE FUNCTION (?P<name>\w+)\((?P<args>[^)]*)\)\s+RETURNS (?P<returns>\w+)\s+"
    r"AS \$\$ (?P<body>[^$]*) \$\$;"
)
OVERLOADS = list(OVERLOAD.finditer(SETUP))
#: The arguments a VARCHAR is passed to, which Snowflake doesn't convert to their VARIANT parameter
TEXT_ARGUMENTS = {"drex_decide_image": ("state", "images")}
#: The functions' Snowflake signatures, the SQL surface Drex's other SQL integrations share. Drex's response body is
#: an OBJECT, not a VARIANT: Snowflake returns a None in a VARIANT result as a JSON null, which IS NULL doesn't match
SIGNATURES = {
    "drex_noul": (
        "FLOAT",
        "input VARIANT, instructions VARCHAR, criteria VARIANT DEFAULT NULL, model VARCHAR DEFAULT 'drex-v2.0'",
    ),
    "drex_choice": ("OBJECT", "input VARIANT, instructions VARCHAR, labels VARIANT, model VARCHAR DEFAULT 'drex-v2.0'"),
    "drex_score": ("OBJECT", "input VARIANT, instructions VARCHAR, levels VARIANT, model VARCHAR DEFAULT 'drex-v2.0'"),
    "drex_decide": ("OBJECT", "state VARIANT, questions VARIANT, model VARCHAR DEFAULT 'drex-v2.0'"),
    "drex_decide_image": (
        "OBJECT",
        "state VARIANT, images VARIANT, questions VARIANT, model VARCHAR DEFAULT 'drex-max'",
    ),
}


def test_every_function_is_created_with_its_signature():
    assert set(FUNCTIONS) == set(drex_snowflake.FUNCTIONS) == set(SIGNATURES)
    for name, (returns, args) in SIGNATURES.items():
        assert " ".join(FUNCTIONS[name]["args"].split()) == args
        assert FUNCTIONS[name]["returns"] == returns


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_each_inline_handler_is_a_vectorized_drex_handler(name):
    match = FUNCTIONS[name]
    assert re.search(rf"HANDLER = '{name}'", match["props"])
    scope = {}
    exec(match["body"], scope)  # what Snowflake runs, with src/drex_snowflake.py imported from the stage
    handler = scope[name]
    assert handler._sf_vectorized_input is pandas.DataFrame
    assert handler._sf_max_batch_size == drex_snowflake.FUNCTIONS[name].max_batch_rows
    assert "concurrency=8" in match["body"]


@pytest.mark.parametrize("name", sorted(SIGNATURES))
def test_each_function_gets_the_key_the_sdk_and_the_module(name):
    props = " ".join(FUNCTIONS[name]["props"].split())
    sdk = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"][0]
    # drex_decide_image reads staged images with SnowflakeFile, from snowflake-snowpark-python
    snowpark = ", 'snowflake-snowpark-python'" if drex_snowflake.FUNCTIONS[name].images else ""
    assert f"PACKAGES = ('{sdk}', 'pandas'{snowpark})" in props
    assert "ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository" in props
    assert "IMPORTS = ('@drex_stage/drex_snowflake.py')" in props
    assert "EXTERNAL_ACCESS_INTEGRATIONS = (drex_access)" in props
    assert (
        "SECRETS = ('nace' = drex_api_key, 'nace_base_url' = drex_base_url, "
        "'nace_openrouter_referer' = drex_openrouter_referer, 'nace_openrouter_title' = drex_openrouter_title)"
    ) in props


def _overloads(name: str, args: str) -> set[tuple[str, str]]:
    """Each VARCHAR overload of a function: its arguments, and the call of the Python function its body makes."""
    params = args.split(", ")
    names = [param.split()[0] for param in params]
    text = TEXT_ARGUMENTS.get(name, names[:1])
    overloads = set()
    for mask in range(1, 2 ** len(text)):
        varchar = {arg for i, arg in enumerate(text) if mask >> i & 1}
        signature = [p.replace(" VARIANT", " VARCHAR", 1) if p.split()[0] in varchar else p for p in params]
        call = [f"TO_VARIANT({n})" if n in varchar else n for n in names]
        overloads.add((", ".join(signature), f"{name}({', '.join(call)})"))
    return overloads


def test_every_function_has_an_overload_for_varchar_input():
    # Snowflake doesn't convert a VARCHAR argument to a VARIANT parameter, so drex_noul(body, ...) on a text column,
    # or drex_decide_image with one BUILD_SCOPED_FILE_URL, needs a VARCHAR overload
    found = {(m["name"], " ".join(m["args"].split()), m["body"]) for m in OVERLOADS}
    expected = {(name, *overload) for name, (_, args) in SIGNATURES.items() for overload in _overloads(name, args)}
    assert found == expected
    assert len(found) == 7
    for overload in OVERLOADS:
        assert overload["returns"] == SIGNATURES[overload["name"]][0]


def test_every_function_is_granted_by_its_argument_types():
    for name, (_, args) in SIGNATURES.items():
        for signature in [args, *(overload for overload, _ in _overloads(name, args))]:
            types = ", ".join(arg.split()[1] for arg in signature.split(", "))
            assert f"GRANT USAGE ON FUNCTION {name}({types}) TO ROLE <role>;" in SETUP


def test_the_client_version_matches_the_project():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert drex_snowflake.__version__ == project["version"]
    assert f"drex-snowflake/{project['version']}" == drex_snowflake.CLIENT_INFO
