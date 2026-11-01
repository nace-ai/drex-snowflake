<p align="center">
  <a href="https://nace.ai"><picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/nace-logo-white.svg"><img src="docs/assets/nace-logo-black.svg" alt="Nace" height="40"></picture></a>
</p>

<h1 align="center">Drex for Snowflake</h1>

<p align="center"><b>Classify text, score rows and read staged images from Snowflake SQL.</b></p>

<p align="center">
  <a href="https://github.com/nace-ai/drex-snowflake/actions/workflows/MockTests.yml"><img src="https://img.shields.io/badge/tests-GitHub_Actions-2088FF" alt="Tests"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="MIT license"></a>
</p>

Ask [Drex](https://console.nace.ai/docs) typed questions about your rows from Snowflake SQL, and get calibrated
probabilities back as SQL values:

| Function | Result |
| --- | --- |
| `drex_noul(input, instructions [, criteria] [, model])` | `FLOAT` probability of yes |
| `drex_choice(input, instructions, labels [, model])` | `OBJECT` `{choice, probabilities, confidence}` |
| `drex_score(input, instructions, levels [, model])` | `OBJECT` `{score, probabilities, confidence}` |
| `drex_decide(state, questions [, model])` | `OBJECT`: Drex's response body |
| `drex_decide_image(state, images, questions [, model])` | `OBJECT`: Drex's response body, about staged images, on Drex Max |

[Getting started](#getting-started) · [Examples](#examples) · [SQL API](docs/API.md) · [Development](#development)

## Getting started

You need a Snowflake account with external access enabled, a warehouse, the Snowflake CLI, and a
[Nace API key](https://console.nace.ai/dashboard/keys). Setup uses `ACCOUNTADMIN`; callers use the role you grant
in the script. Trial accounts may need Snowflake support to enable external access.

```sh
git clone --branch v0.2.0 https://github.com/nace-ai/drex-snowflake.git
cd drex-snowflake
```

`v0.2.0` is the latest release; [CHANGELOG.md](CHANGELOG.md) lists each release's changes.

Follow [the installation guide](docs/INSTALL.md#setup) to create the Snowflake secret and run `sql/setup.sql`.
It uploads the Python module to a stage and creates the five functions in `DREX.PUBLIC`; there is no
`drex-snowflake` package to install from PyPI. The functions use Python 3.12 and install `nace-sdk` through
Snowflake's Artifact Repository; the guide also covers installing that dependency from a stage.

After setup, run this self-contained query:

```sql
SELECT drex.public.drex_noul(
    'I was charged twice. Please refund the duplicate payment.',
    'Is the customer asking for a refund?'
) AS refund_probability;
```

The result is a probability from 0 to 1, or NULL if no answer is available. Model answers can vary.

## Examples

Use this synthetic fixture in one Snowflake session:

```sql
USE SCHEMA drex.public;
CREATE TEMP TABLE tickets (id INTEGER, body VARCHAR);
INSERT INTO tickets VALUES
    (1, 'I was charged twice. Please refund one.'),
    (2, 'Our checkout is down.');
```

### Route and score tickets

```sql
SELECT id,
       drex_choice(body, 'Which team?', ['billing', 'technical', 'account']):choice::STRING AS team,
       drex_score(body, 'How urgent is this ticket?', ['low', 'medium', 'high']):score::FLOAT AS urgency
FROM tickets;
```

### Ask several questions per row

```sql
SELECT id, drex_decide(
    OBJECT_CONSTRUCT('ticket', body),
    PARSE_JSON('{"refund": {"type": "noul", "instructions": "Is a refund requested?"}, "team": {"type": "choice", "instructions": "Which team?", "criteria": {"billing": "Payments", "technical": "Bugs"}}}')
):answers AS answers
FROM tickets;
```

For images, pass scoped file URLs from a stage the caller can read to
[`drex_decide_image`](docs/API.md#arguments). Upload your PNG, JPEG or WebP fixtures first.

## Configuration and behavior

Text and JSON calls default to `drex-v2.0`; image calls default to `drex-max`. Optional arguments may be omitted
from the right, or all arguments named; Snowflake does not mix named and positional arguments.

Each handler allows eight concurrent requests per batch; the warehouse runs multiple batches at once.
Review [concurrency](docs/INSTALL.md#concurrency) before a large query. Invalid questions, authentication failures
and unknown models fail the query. Row-specific failures, exhausted retries and unsent rows at the batch deadline
return NULL. See [errors](docs/API.md#errors) for retry limits and logging.

## How it works

`sql/setup.sql` creates an external access integration for `console.nace.ai`, a secret holding the API key, and five
[vectorized Python UDFs](https://docs.snowflake.com/en/developer-guide/udf/python/udf-python-batch). Each one imports
[`src/drex_snowflake.py`](src/drex_snowflake.py) from a stage, which sends each row of a batch to Drex's
`POST /v1/systemone` through [`nace-sdk`](https://pypi.org/project/nace-sdk/), up to 8 at once, retrying 429s and
5xxs. Each request carries `X-Nace-Client: drex-snowflake/<version>`.

## Requests, privacy and cost

Each evaluated non-NULL row sends its content and questions to the configured API; images are read from the stage
and sent as bytes. Use only data you may send to that service. A custom endpoint receives the key as well as data.
Only answered requests (HTTP 200) are billed by Drex. Repeated queries and warehouse retries can send rows again;
there is no answer cache. Save results in a table and filter input rows before calling the functions. Snowflake
compute charges are separate. Treat NULL as unanswered, never as a negative answer.

## Development

```sh
uv sync
uv run pytest           # the handler against the mock Drex API in tests/mock
uv run ruff check . && uv run ruff format --check .
```

The tests call each handler as Snowflake does, with a DataFrame of its arguments by position, against
`tests/mock/drex_mock.py`, drex-duckdb's mock of Drex's `/v1/systemone` and `/v1/models` (copied from
`nace-ai/drex-duckdb` at `9139ced`; it is the same file, so take fixes there first). `tests/test_setup_sql.py` checks
`sql/setup.sql` against the module: each function's signature, inline handler, packages and grants.

## Support and contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for local checks and pull requests, and the
[API reference](docs/API.md) for the complete contract. Report bugs through
[GitHub Issues](https://github.com/nace-ai/drex-snowflake/issues), with your runtime version and a minimal example
using synthetic data. Remove keys, signed URLs and private data from reports.

## License and trademarks

[MIT](LICENSE). Nace artwork belongs to Nace AI. The software license does not grant trademark rights.
See [asset sources and branding guidance](docs/assets/README.md).

This integration is maintained by Nace AI. References to the platform describe compatibility,
not sponsorship or endorsement by its owner.

Snowflake is a trademark of Snowflake Inc.
