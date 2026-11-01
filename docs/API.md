# SQL API

[`sql/setup.sql`](../sql/setup.sql) creates these functions in `DREX.PUBLIC`; [INSTALL.md](INSTALL.md) covers setup.
They expose Drex decisions with Snowflake's types.

| Function | Result |
| --- | --- |
| `drex_noul(input, instructions [, criteria] [, model])` | `FLOAT`: the probability that the answer is yes |
| `drex_choice(input, instructions, labels [, model])` | `OBJECT`: `{choice, probabilities: [{value, probability}], confidence}`, probabilities sorted by label |
| `drex_score(input, instructions, levels [, model])` | `OBJECT`: `{score, probabilities: [{index, value, probability}], confidence}`, probabilities by index |
| `drex_decide(state, questions [, model])` | `OBJECT`: Drex's response body (`model`, `answers`, `usage`, `evaluation_time_ms`, `request_id`) as it came |
| `drex_decide_image(state, images, questions [, model])` | `OBJECT`: the same, for questions about up to 8 staged images |

```sql
SELECT drex_choice(body, 'What is this support ticket about?', ['billing', 'technical', 'account']) FROM tickets;

SELECT drex_noul(body, 'Is the customer asking for a refund?', NULL, 'drex-max') FROM tickets;

SELECT drex_score(body, 'How upset is the customer?', ['Calm', 'Mildly annoyed', 'Frustrated', 'Angry']):score
FROM tickets;

SELECT drex_decide(
    OBJECT_CONSTRUCT('subject', subject, 'body', body),
    PARSE_JSON('{
      "refund":   {"type": "noul",   "instructions": "Is a refund needed?"},
      "priority": {"type": "choice", "instructions": "Which priority?", "criteria": {"p0": "Outage", "p1": null, "p2": null}}
    }')
):answers FROM tickets;
```

Optional arguments can be left off from the right, or every argument named:
`drex_noul(input => body, instructions => 'Refund?', model => 'drex-max')`. Snowflake doesn't mix named and
positional arguments in one call.

The functions live in `DREX.PUBLIC`. From another schema, call them as `drex.public.drex_choice(...)`, or add the schema
to the session's search path once and call them unqualified:

```sql
ALTER SESSION SET SEARCH_PATH = '$current, $public, drex.public';
```

## Arguments

- **`input`, `state`**: a `VARCHAR` is sent as text. An `OBJECT` or `ARRAY` (or any other `VARIANT`) is sent as JSON
  state. A NULL returns NULL and sends nothing. Each function has a `VARCHAR` overload for this argument, since
  Snowflake doesn't convert a `VARCHAR` to `VARIANT` to call a function. Numbers, dates and timestamps are sent as their
  JSON value. An empty string, `{}` or `[]` is sent and billed like any other input, so turn the empty values of a
  column into NULL first, for example `NULLIF(TRIM(body), '')`; `OBJECT_CONSTRUCT` leaves out NULL values, so a row
  whose fields are all NULL is `{}`.
- **`instructions`**: NULL or `''` is no instructions. A `noul` needs instructions, or a described `true` or `false`.
- **`criteria`, `labels`, `levels`**: an `ARRAY` of labels (`['billing', 'tech']`), an `ARRAY` of
  `{'label', 'description'}` objects, or, except for `levels`, an `OBJECT` of label to description (a description may
  be NULL). Snowflake doesn't convert a `VARCHAR` to these `VARIANT` parameters, so pass JSON text through
  `PARSE_JSON`.
  - `noul` labels are exactly `true` and `false`; labels alone, with no description, are the same as no criteria.
  - `choice` and `score` take 2–255 unique, non-empty labels.
  - `levels` take no descriptions (describe the scale in `instructions`), and their order is the scale, so they must be
    an `ARRAY`: Snowflake doesn't keep the order of an `OBJECT`'s keys.
- **`questions`**: an `OBJECT` (JSON text through `PARSE_JSON`) of question id to `{type, instructions, criteria}`. 1–512 questions; ids
  are non-empty after trimming, unique ignoring case, and not `__proto__`; `type` is `noul`, `choice` or `score`
  (`multiple_choice` and `box` are refused by name); an unknown key in a question is refused. A NULL returns NULL.
- **`images`**: a scoped file URL from `BUILD_SCOPED_FILE_URL(@stage, path)` (a `VARCHAR`, which an overload takes), or
  an `ARRAY` of 1–8 of them, of PNG, JPEG or WebP files. The caller's role must be able to read the files. The function reads the bytes and sends them;
  Drex never fetches a URL. A NULL image, or an empty array, returns NULL and sends nothing. The typed helpers take no
  images, since a `VARIANT` can't hold bytes; ask about images with `drex_decide_image`.

  ```sql
  SELECT relative_path,
         drex_decide_image('', BUILD_SCOPED_FILE_URL(@receipts, relative_path),
                           PARSE_JSON('{"receipt": {"type": "noul", "instructions": "Is this a receipt?"}}')):answers
  FROM DIRECTORY(@receipts);
  ```
- **`model`**: `drex-v2.0` by default, or `drex-max` for `drex_decide_image`; NULL or `''` is the default. The name is
  checked, exactly as written, against `GET /v1/models`, which each handler process fetches once and again for a name it lacks. Images need
  a model that reads them (`drex-max`, `drex-max-v1.1`).

`instructions`, `criteria`, `labels`, `levels`, `questions` and `model` are usually constants, but may vary by row. Each
distinct value in a batch is checked once, and a value that fails a check fails the query, whichever row it is on.

## Errors

| Outcome | When |
| --- | --- |
| **The query fails** | 401 (Drex's message, or "the API key was refused"); 402 (Drex's message and the billing page, `<base URL>/dashboard/billing`, or OpenRouter's credits page); a 422 about anything but the row (each issue as `path: message`); an unknown model; a TLS failure; a staged image that can't be read; any of the checks above |
| **NULL, with a warning** | a 422 whose message starts `Request too long:` or whose issues are all under `state` or `images` (the warning names `state`, never the row's keys); a body over 4 MiB, checked before sending; an image that isn't PNG, JPEG or WebP, or is over 16 MiB; more than 8 images; retries run out; any other status (for example 403 or 404); a row not yet sent when the batch nears Snowflake's 180-second limit |
| **Retried** | no response, 408, 429, 500, 502, 503, 504 or 529: up to 5 attempts within 120 seconds of the first, waiting for `retry-after-ms` (or `retry-after`), else 1, 2, 4 and 8 seconds less up to 25 % jitter |

Treat NULL as no answer, not as a negative one. NULL is SQL NULL for every function, so `WHERE answer IS NULL` finds
the rows to look at again.

Warnings go through Python's `logging` to the account's event table (`SNOWFLAKE.TELEMETRY.EVENTS` unless you set
another), and only at a log level of `WARN` or finer, which is `OFF` by default. Turn them on for the functions with:

```sql
ALTER DATABASE drex SET LOG_LEVEL = WARN;
```

then read them with `SELECT timestamp, record:severity_text::STRING AS level, value FROM snowflake.telemetry.events WHERE record_type = 'LOG' AND
resource_attributes:"snow.executable.name"::STRING ILIKE 'DREX_%' ORDER BY timestamp DESC`. A warning names the
function and the reason, never the row's content.

## Billing

Drex bills only a `200`, and every attempt of a request sends the same body, so a retry is billed twice only when a
`200` was lost on its way back. Drex keeps no answer cache: running a query again, or Snowflake running part of one
again, is billed again.
