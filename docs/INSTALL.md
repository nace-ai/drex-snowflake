# Installing Drex for Snowflake

## What it needs

- **External access.** The functions call `https://console.nace.ai` through an external access integration, which
  needs `ACCOUNTADMIN` (or a role with `CREATE INTEGRATION`) to create. Snowflake doesn't enable external access on
  trial accounts ("By default, Snowflake does not enable external access for trial accounts"); ask Snowflake support
  to enable it, or use a paid account.
- **nace-sdk.** By default the functions install [`nace-sdk`](https://pypi.org/project/nace-sdk/) from PyPI through
  Snowflake's Artifact Repository, `snowflake.snowpark.pypi_shared_repository`. Every role that creates or calls the
  functions needs the database role `SNOWFLAKE.PYPI_REPOSITORY_USER`; `setup.sql` grants it to the calling role. If
  your account can't use the Artifact Repository, [install nace-sdk from a stage](#installing-nace-sdk-from-a-stage).
- **A Drex API key** from <https://console.nace.ai/dashboard/keys>. Use a key for this integration alone, so you can
  rotate it without touching anything else.

## Setup

1. Keep the real key out of the tracked SQL file. Copy the setup script into the ignored `dist/` directory,
   restrict its permissions, and edit that copy locally to replace `<your nace_sk_ key>` and `<role>`:

   ```sh
   mkdir -p dist
   (umask 077 && cp sql/setup.sql dist/setup.local.sql)
   ```

   Use your editor to enter the key, not a shell command containing it. Treat this local file as a secret;
   do not attach it to issues or logs. `<role>` must already exist: the script grants it the functions but doesn't
   create it, and the role also needs a warehouse to run queries.
2. From the repository's root, run it with the [Snowflake CLI](https://docs.snowflake.com/en/developer-guide/snowflake-cli/index):

   ```sh
   snow sql -f dist/setup.local.sql
   ```

   It creates the database `DREX`, a network rule for `console.nace.ai:443`, the secrets `drex_api_key` and
   `drex_base_url`, the integration `drex_access`, the stage `drex_stage` with `src/drex_snowflake.py` on it, and the
   five functions, with SQL overloads that take `VARCHAR` input (and, for `drex_decide_image`, a single image URL).

   In a Snowsight worksheet, which can't run `PUT`, create the stage, upload `src/drex_snowflake.py` to it (**+ Files**
   on the stage's page), then run the rest of the script without the `PUT`.
3. Delete the local credential-bearing copy after successful setup:

   ```sh
   rm dist/setup.local.sql
   ```

   Check the installation:

   ```sql
   SELECT drex.public.drex_noul('I was charged twice. Please refund one.', 'Is the customer asking for a refund?');
   ```

A row Drex can't answer returns NULL with a warning, which Snowflake records only once logging is on; to see why rows
returned NULL, run `ALTER DATABASE drex SET LOG_LEVEL = WARN` ([errors](API.md#errors)).

Running the script again upgrades the functions and the staged module. It doesn't replace the secrets, so the key
stays; change it with `ALTER SECRET drex.public.drex_api_key SET SECRET_STRING = '…'`.

## Installing nace-sdk from a stage

For an account without the Artifact Repository: nace-sdk is pure Python, and its dependencies (`httpx`, `pydantic`,
`typing-extensions`) are in Snowflake's Anaconda channel (on 2026-10-30, `httpx` 0.28.1, `pydantic` 2.13.5 and
`typing_extensions` 4.16.0 for Python 3.12; nace-sdk needs `httpx>=0.27.0` and `pydantic>=2.11.5`).

1. Download the wheel and stage it as a `.zip`, which Snowflake imports (a wheel is a zip archive):

   ```sh
   pip download nace-sdk==0.6.2 --no-deps --only-binary=:all: -d dist
   cp dist/nace_sdk-0.6.2-py3-none-any.whl dist/nace_sdk-0.6.2.zip
   snow stage copy dist/nace_sdk-0.6.2.zip @drex.public.drex_stage --overwrite
   ```

2. In each `LANGUAGE PYTHON` function of `sql/setup.sql`, drop the `ARTIFACT_REPOSITORY` line, and replace the `PACKAGES` and
   `IMPORTS` lines with:

   ```sql
     PACKAGES = ('pandas', 'httpx', 'pydantic', 'typing-extensions')
     IMPORTS = ('@drex_stage/drex_snowflake.py', '@drex_stage/nace_sdk-0.6.2.zip')
   ```

   keeping `'snowflake-snowpark-python'` in `drex_decide_image`'s `PACKAGES`, which reads the staged images with it.

   and drop the `SNOWFLAKE.PYPI_REPOSITORY_USER` grant. If your account requires it, accept the Anaconda terms first
   (**Admin > Billing & Terms > Anaconda**).

Check which versions your account offers with:

```sql
SELECT package_name, MAX(version) FROM information_schema.packages
WHERE language = 'python' AND package_name IN ('httpx', 'pydantic', 'typing-extensions', 'pandas')
GROUP BY package_name;
```

## Concurrency

Each function sends at most 8 requests at once per batch (`concurrency=8` in each handler in `setup.sql`). An X-Small
warehouse runs 8 batches at once, so a query can have about 8 × 8 = 64 requests in flight (55 measured), and a larger
warehouse more. Drex's account limits are 8 in flight and 120 requests a minute on a free account, and 16 and 600 on a
paid one; a request over them gets a 429, which the functions wait out and retry, and a row whose retries run out
returns NULL. On an X-Small, `concurrency=2` kept a query within 16 requests in flight in Nace's tests, a paid account's limit,
and `concurrency=1` within 8, a free account's. To change the cap, edit the number in all five handlers and run
the script again.

A batch is at most 64 rows (16 for `drex_decide_image`), so that it finishes within Snowflake's 180-second limit for a
batch. A row still unsent when a batch nears that limit returns NULL.

## OpenRouter

To call Drex through [OpenRouter](https://openrouter.ai) with an OpenRouter key: add `'openrouter.ai:443'` to the
network rule's `VALUE_LIST`, store the OpenRouter key in `drex_api_key`, and set `drex_base_url` to
`https://openrouter.ai/api`. Models are named as on Drex (`drex-v2.0`), and sent under `nace-ai/`.

A `nace_sk_` key in `drex_api_key` is refused before any request when `drex_base_url` is OpenRouter's, so the query
fails with an error rather than sending it. A Snowflake function takes one key secret, so there is no separate
OpenRouter key variable here: the `drex_api_key` secret holds the OpenRouter key.

OpenRouter attributes requests to an app through the `HTTP-Referer` and `X-OpenRouter-Title` headers, which the
functions send, only to OpenRouter, as `https://nace.ai` and `Drex for Snowflake`. To report your own app instead:

```sql
ALTER SECRET drex.public.drex_openrouter_referer SET SECRET_STRING = 'https://your.app';
ALTER SECRET drex.public.drex_openrouter_title SET SECRET_STRING = 'Your App';
```

Functions created before these secrets existed keep the defaults until you run `setup.sql` again, which adds the secrets
to the integration and to every function.

## Upgrading

Each release is a tag, `vMAJOR.MINOR.PATCH`, with its changes in [CHANGELOG.md](../CHANGELOG.md). To upgrade, check out
the new release, make the local copy of `setup.sql` again as in [Setup](#setup), run it, and delete it:

```sh
git fetch --tags
git checkout v0.2.0
```

The script replaces the functions and the staged module and keeps the secrets, so the key stays. If you lowered
`concurrency`, lower it again in the new copy.

## Changing the code

`src/drex_snowflake.py` is what runs. After changing it, run `setup.sql` again, which stages it and replaces the
functions. When `nace-sdk`'s version changes, change it in `pyproject.toml` and in every `PACKAGES` of `setup.sql`
together; `tests/test_setup_sql.py` checks they agree.
