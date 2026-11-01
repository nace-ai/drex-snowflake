-- Drex for Snowflake: creates drex_noul, drex_choice, drex_score, drex_decide and drex_decide_image in DREX.PUBLIC.
--
-- Run a local copy of it from the repository's root with the Snowflake CLI, which uploads src/drex_snowflake.py with
-- PUT. The copy holds your key, so keep it in the ignored dist/ directory and delete it afterwards (docs/INSTALL.md):
--
--     snow sql -f dist/setup.local.sql
--
-- In the copy, before running it:
--   1. Replace <your nace_sk_ key> below with a Drex API key (https://console.nace.ai/dashboard/keys). The secret is
--      created only if it doesn't exist, so running the script again to upgrade the functions keeps the key.
--   2. Replace <role> at the end with the role that calls the functions.
--
-- In a Snowsight worksheet, which can't run PUT, upload src/drex_snowflake.py to DREX.PUBLIC.DREX_STAGE first
-- (Data > Databases > DREX > PUBLIC > Stages > DREX_STAGE > + Files) and skip the PUT statement.
--
-- Trial accounts: Snowflake doesn't enable external access on a trial, so CREATE EXTERNAL ACCESS INTEGRATION fails
-- there until Snowflake support enables it for the account.
--
-- nace-sdk comes from PyPI through Snowflake's Artifact Repository. To install it from a stage instead, see
-- docs/INSTALL.md, "Installing nace-sdk from a stage".
--
-- Concurrency: each function sends at most 8 requests at once per batch (`concurrency=8` in each handler below).
-- Snowflake runs several batches at once on each warehouse node, so a query can have several times that in flight.
-- See docs/INSTALL.md, "Concurrency", before changing it.

USE ROLE ACCOUNTADMIN;

CREATE DATABASE IF NOT EXISTS drex;
USE SCHEMA drex.public;

-- Where the functions may connect. For OpenRouter, add 'openrouter.ai:443' and set drex_base_url to
-- https://openrouter.ai/api
CREATE OR REPLACE NETWORK RULE drex_egress
  MODE = EGRESS
  TYPE = HOST_PORT
  VALUE_LIST = ('console.nace.ai:443');

CREATE SECRET IF NOT EXISTS drex_api_key
  TYPE = GENERIC_STRING
  SECRET_STRING = '<your nace_sk_ key>';

CREATE SECRET IF NOT EXISTS drex_base_url
  TYPE = GENERIC_STRING
  SECRET_STRING = 'https://console.nace.ai';

-- The app identity OpenRouter attributes requests to, read only when drex_base_url is OpenRouter's. Set them to
-- your own app's with ALTER SECRET; a key for OpenRouter goes in drex_api_key, never a nace_sk_ key
CREATE SECRET IF NOT EXISTS drex_openrouter_referer
  TYPE = GENERIC_STRING
  SECRET_STRING = 'https://nace.ai';

CREATE SECRET IF NOT EXISTS drex_openrouter_title
  TYPE = GENERIC_STRING
  SECRET_STRING = 'Drex for Snowflake';

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION drex_access
  ALLOWED_NETWORK_RULES = (drex_egress)
  ALLOWED_AUTHENTICATION_SECRETS = (drex_api_key, drex_base_url, drex_openrouter_referer, drex_openrouter_title)
  ENABLED = TRUE;

CREATE STAGE IF NOT EXISTS drex_stage;
PUT file://src/drex_snowflake.py @drex_stage AUTO_COMPRESS = FALSE OVERWRITE = TRUE;

-- The probability that the answer is yes
CREATE OR REPLACE FUNCTION drex_noul(
    input VARIANT,
    instructions VARCHAR,
    criteria VARIANT DEFAULT NULL,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS FLOAT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository
  PACKAGES = ('nace-sdk==0.6.2', 'pandas')
  IMPORTS = ('@drex_stage/drex_snowflake.py')
  EXTERNAL_ACCESS_INTEGRATIONS = (drex_access)
  SECRETS = ('nace' = drex_api_key, 'nace_base_url' = drex_base_url,
            'nace_openrouter_referer' = drex_openrouter_referer, 'nace_openrouter_title' = drex_openrouter_title)
  HANDLER = 'drex_noul'
  COMMENT = 'Drex: the probability that the answer to a yes-or-no question about input is yes'
AS $$
import drex_snowflake

drex_noul = drex_snowflake.handler("drex_noul", concurrency=8)
$$;

-- The same with VARCHAR input, which Snowflake doesn't convert to VARIANT to call a function
CREATE OR REPLACE FUNCTION drex_noul(
    input VARCHAR,
    instructions VARCHAR,
    criteria VARIANT DEFAULT NULL,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS FLOAT
AS $$ drex_noul(TO_VARIANT(input), instructions, criteria, model) $$;

-- One of 2 to 255 labels
CREATE OR REPLACE FUNCTION drex_choice(
    input VARIANT,
    instructions VARCHAR,
    labels VARIANT,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS OBJECT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository
  PACKAGES = ('nace-sdk==0.6.2', 'pandas')
  IMPORTS = ('@drex_stage/drex_snowflake.py')
  EXTERNAL_ACCESS_INTEGRATIONS = (drex_access)
  SECRETS = ('nace' = drex_api_key, 'nace_base_url' = drex_base_url,
            'nace_openrouter_referer' = drex_openrouter_referer, 'nace_openrouter_title' = drex_openrouter_title)
  HANDLER = 'drex_choice'
  COMMENT = 'Drex: which of the labels answers a question about input'
AS $$
import drex_snowflake

drex_choice = drex_snowflake.handler("drex_choice", concurrency=8)
$$;

-- The same with VARCHAR input, which Snowflake doesn't convert to VARIANT to call a function
CREATE OR REPLACE FUNCTION drex_choice(
    input VARCHAR,
    instructions VARCHAR,
    labels VARIANT,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS OBJECT
AS $$ drex_choice(TO_VARIANT(input), instructions, labels, model) $$;

-- A position on a scale of 2 to 255 ordered levels
CREATE OR REPLACE FUNCTION drex_score(
    input VARIANT,
    instructions VARCHAR,
    levels VARIANT,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS OBJECT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository
  PACKAGES = ('nace-sdk==0.6.2', 'pandas')
  IMPORTS = ('@drex_stage/drex_snowflake.py')
  EXTERNAL_ACCESS_INTEGRATIONS = (drex_access)
  SECRETS = ('nace' = drex_api_key, 'nace_base_url' = drex_base_url,
            'nace_openrouter_referer' = drex_openrouter_referer, 'nace_openrouter_title' = drex_openrouter_title)
  HANDLER = 'drex_score'
  COMMENT = 'Drex: where input falls on a scale of ordered levels'
AS $$
import drex_snowflake

drex_score = drex_snowflake.handler("drex_score", concurrency=8)
$$;

-- The same with VARCHAR input, which Snowflake doesn't convert to VARIANT to call a function
CREATE OR REPLACE FUNCTION drex_score(
    input VARCHAR,
    instructions VARCHAR,
    levels VARIANT,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS OBJECT
AS $$ drex_score(TO_VARIANT(input), instructions, levels, model) $$;

-- Several questions in one request; Drex's response body as it came
CREATE OR REPLACE FUNCTION drex_decide(
    state VARIANT,
    questions VARIANT,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS OBJECT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository
  PACKAGES = ('nace-sdk==0.6.2', 'pandas')
  IMPORTS = ('@drex_stage/drex_snowflake.py')
  EXTERNAL_ACCESS_INTEGRATIONS = (drex_access)
  SECRETS = ('nace' = drex_api_key, 'nace_base_url' = drex_base_url,
            'nace_openrouter_referer' = drex_openrouter_referer, 'nace_openrouter_title' = drex_openrouter_title)
  HANDLER = 'drex_decide'
  COMMENT = 'Drex: answers to several questions about state, as Drex''s response body'
AS $$
import drex_snowflake

drex_decide = drex_snowflake.handler("drex_decide", concurrency=8)
$$;

-- The same with VARCHAR state, which Snowflake doesn't convert to VARIANT to call a function
CREATE OR REPLACE FUNCTION drex_decide(
    state VARCHAR,
    questions VARIANT,
    model VARCHAR DEFAULT 'drex-v2.0'
  )
  RETURNS OBJECT
AS $$ drex_decide(TO_VARIANT(state), questions, model) $$;

-- Questions about up to 8 staged images, by scoped file URL (BUILD_SCOPED_FILE_URL); Drex Max by default
CREATE OR REPLACE FUNCTION drex_decide_image(
    state VARIANT,
    images VARIANT,
    questions VARIANT,
    model VARCHAR DEFAULT 'drex-max'
  )
  RETURNS OBJECT
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.12'
  ARTIFACT_REPOSITORY = snowflake.snowpark.pypi_shared_repository
  -- snowflake-snowpark-python for SnowflakeFile, which reads the staged images
  PACKAGES = ('nace-sdk==0.6.2', 'pandas', 'snowflake-snowpark-python')
  IMPORTS = ('@drex_stage/drex_snowflake.py')
  EXTERNAL_ACCESS_INTEGRATIONS = (drex_access)
  SECRETS = ('nace' = drex_api_key, 'nace_base_url' = drex_base_url,
            'nace_openrouter_referer' = drex_openrouter_referer, 'nace_openrouter_title' = drex_openrouter_title)
  HANDLER = 'drex_decide_image'
  COMMENT = 'Drex: answers to questions about staged images, as Drex''s response body'
AS $$
import drex_snowflake

drex_decide_image = drex_snowflake.handler("drex_decide_image", concurrency=8)
$$;

-- The same with VARCHAR state, which Snowflake doesn't convert to VARIANT to call a function
CREATE OR REPLACE FUNCTION drex_decide_image(
    state VARCHAR,
    images VARIANT,
    questions VARIANT,
    model VARCHAR DEFAULT 'drex-max'
  )
  RETURNS OBJECT
AS $$ drex_decide_image(TO_VARIANT(state), images, questions, model) $$;

-- The same with a VARCHAR image, such as BUILD_SCOPED_FILE_URL's, for each type of state
CREATE OR REPLACE FUNCTION drex_decide_image(
    state VARIANT,
    images VARCHAR,
    questions VARIANT,
    model VARCHAR DEFAULT 'drex-max'
  )
  RETURNS OBJECT
AS $$ drex_decide_image(state, TO_VARIANT(images), questions, model) $$;

CREATE OR REPLACE FUNCTION drex_decide_image(
    state VARCHAR,
    images VARCHAR,
    questions VARIANT,
    model VARCHAR DEFAULT 'drex-max'
  )
  RETURNS OBJECT
AS $$ drex_decide_image(TO_VARIANT(state), TO_VARIANT(images), questions, model) $$;

-- The role that calls the functions. It needs SNOWFLAKE.PYPI_REPOSITORY_USER too, because the functions install
-- nace-sdk from the Artifact Repository
GRANT USAGE ON DATABASE drex TO ROLE <role>;
GRANT USAGE ON SCHEMA drex.public TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_noul(VARIANT, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_noul(VARCHAR, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_choice(VARIANT, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_choice(VARCHAR, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_score(VARIANT, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_score(VARCHAR, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_decide(VARIANT, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_decide(VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_decide_image(VARIANT, VARIANT, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_decide_image(VARCHAR, VARIANT, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_decide_image(VARIANT, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT USAGE ON FUNCTION drex_decide_image(VARCHAR, VARCHAR, VARIANT, VARCHAR) TO ROLE <role>;
GRANT DATABASE ROLE SNOWFLAKE.PYPI_REPOSITORY_USER TO ROLE <role>;
