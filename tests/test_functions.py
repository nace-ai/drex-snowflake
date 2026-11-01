"""The drex_* handlers against the mock Drex API: the shared SQL surface's arguments, results and error rules."""

import dataclasses
import json
import logging
import math
import sys

import httpx
import pytest
from conftest import KEY, OPEN_ROUTER_KEY, call
from nace_sdk import NaceInsufficientCreditError

import drex_snowflake
from drex_snowflake import DrexError

TICKET = "I was charged twice for my March invoice. Please refund one of the charges."
TOPICS = {
    "billing": "Payments, invoices, refunds, or charges",
    "technical": "Bugs, errors, or outages",
    "account": "Login, profile, or permissions",
    "other": None,
}
UPSET = ["Calm", "Mildly annoyed", "Frustrated", "Angry"]


# ---------------------------------------------------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("model", ["drex-v2.0", "drex-max"])
def test_helpers_answer_on_each_model(drex, model):
    noul = call("drex_noul", [(TICKET, "Is the customer asking for a refund?", None, model)])
    choice = call("drex_choice", [(TICKET, "What is this support ticket about?", TOPICS, model)])
    score = call("drex_score", [(TICKET, "How upset is the customer?", UPSET, model)])

    assert noul == [0.9868]
    # Sorted by label, with Drex's own confidence
    assert choice == [
        {
            "choice": "billing",
            "probabilities": [
                {"value": "account", "probability": 0.0006},
                {"value": "billing", "probability": 0.9895},
                {"value": "other", "probability": 0.0049},
                {"value": "technical", "probability": 0.005},
            ],
            "confidence": 0.986,
        }
    ]
    # By index, each row's value naming its level
    assert score == [
        {
            "score": 1.5306,
            "probabilities": [
                {"index": 0, "value": "Calm", "probability": 0.1572},
                {"index": 1, "value": "Mildly annoyed", "probability": 0.3516},
                {"index": 2, "value": "Frustrated", "probability": 0.2946},
                {"index": 3, "value": "Angry", "probability": 0.1966},
            ],
            "confidence": 0.7183,
        }
    ]
    assert {r["served_model"] for r in drex.log()} == {model}


def test_requests_name_the_client_and_the_default_model(drex):
    call("drex_noul", [(TICKET, "Is this about billing?", None, None), (TICKET, "Is this about billing?", None, "")])

    for record in drex.log():
        assert record["x_nace_client"] == f"drex-snowflake/{drex_snowflake.__version__}"
        assert record["model"] == "drex-v2.0"
        assert record["state_type"] == "string"
        assert record["instructions_present"]


def test_structured_input_is_sent_as_json_state(drex):
    call("drex_noul", [({"subject": "Refund", "body": TICKET}, "Is this about billing?", None, None)])

    [record] = drex.log()
    assert record["state_type"] == "object"
    assert json.loads(record["state"]) == {"subject": "Refund", "body": TICKET}


def test_a_null_input_returns_null_and_sends_nothing(drex):
    rows = [(None, "Is this about billing?", None, None), (TICKET, "Is this about billing?", None, None)]
    noul = call("drex_noul", rows)
    choice = call("drex_choice", [(None, "Which?", ["a", "b"], None)])

    assert math.isnan(noul[0]) and noul[1] == 0.75
    assert choice == [None]
    assert len(drex.log()) == 1


def test_a_mixed_batch_of_questions_is_one_request(drex):
    questions = {
        "category": {"type": "choice", "instructions": "What is this support ticket about?", "criteria": TOPICS},
        "sentiment": {"type": "score", "instructions": "How upset is the customer?", "criteria": UPSET},
        "wants_refund": {"type": "noul", "instructions": "Is the customer asking for a refund?"},
    }

    [body] = call("drex_decide", [(TICKET, questions, None)])

    [record] = drex.log()
    assert record["question_count"] == 3
    # The whole response body, unchanged
    assert set(body) == {"model", "answers", "usage", "evaluation_time_ms", "request_id"}
    assert body["answers"]["wants_refund"]["noul"] == 0.9868
    assert body["answers"]["category"]["choice"] == "billing"
    assert body["model"] == "drex-v2.0"


def test_questions_may_be_json_text(drex):
    [body] = call("drex_decide", [(TICKET, '{"refund": {"type": "noul", "instructions": "Refund?"}}', "drex-v1.5")])

    assert body["answers"]["refund"]["noul"] == 0.75
    assert drex.log()[0]["served_model"] == "drex-v1.5"


def test_noul_with_criteria_and_no_instructions(drex):
    criteria = {"true": "Many users cannot use the product", "false": "A single user or a question"}

    assert call("drex_noul", [(TICKET, None, criteria, None)]) == [0.75]
    [record] = drex.log()
    assert json.loads(record["questions"]) == [{"id": "noul", "type": "noul", "instructions": "", "criteria": criteria}]


def test_a_choice_option_with_a_null_description(drex):
    labels = [{"label": "billing", "description": "Charges"}, {"label": "other", "description": None}]

    [choice] = call("drex_choice", [(TICKET, "Which?", labels, None)])

    assert choice["choice"] == "billing"
    assert json.loads(drex.log()[0]["questions"])[0]["criteria"] == {"billing": "Charges", "other": None}


def test_labels_alone_are_a_noul_without_criteria(drex):
    call("drex_noul", [(TICKET, "Is this about billing?", ["true", "false"], None)])

    assert "criteria" not in json.loads(drex.log()[0]["questions"])[0]


# ---------------------------------------------------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------------------------------------------------


def which(image_number):
    return {
        "which": {
            "type": "choice",
            "instructions": f"Which colour is Image {image_number}?",
            "criteria": {"red": None, "blue": None},
        }
    }


def test_drex_max_reads_one_then_two_images_in_order(drex, staged_images):
    one = call("drex_decide_image", [("", "scoped://red.png", which(1), None)])
    two = call(
        "drex_decide_image",
        [
            ("", ["scoped://red.png", "scoped://blue.png"], which(1), None),
            ("", ["scoped://red.png", "scoped://blue.png"], which(2), None),
            ("", ["scoped://blue.png", "scoped://red.png"], which(1), None),
        ],
    )

    assert one[0]["answers"]["which"]["choice"] == "red"
    assert [body["answers"]["which"]["choice"] for body in two] == ["red", "blue", "blue"]
    records = drex.log()
    # With no model, drex_decide_image uses drex-max
    assert {r["model"] for r in records} == {"drex-max"}
    assert sorted(r["image_colours"] for r in records) == [
        ["blue", "red"],
        ["red"],
        ["red", "blue"],
        ["red", "blue"],
    ]


def test_images_with_a_model_that_does_not_read_them_fail_the_query(drex, staged_images):
    with pytest.raises(DrexError, match=r"^drex_decide_image: drex-v2.0 does not read images"):
        call("drex_decide_image", [("", "scoped://red.png", which(1), "drex-v2.0")])
    assert drex.log() == []


def test_a_null_image_or_no_images_returns_null(drex, staged_images):
    rows = [("", None, which(1), None), ("", [], which(1), None), ("", ["scoped://red.png", None], which(1), None)]

    assert call("drex_decide_image", rows) == [None, None, None]
    assert drex.log() == []


def test_a_file_that_is_not_an_image_returns_null(drex, staged_images, caplog):
    rows = [("", "scoped://notes.txt", which(1), None), ("", "scoped://red.png", which(1), None)]

    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        results = call("drex_decide_image", rows)

    assert results[0] is None and results[1]["answers"]["which"]["choice"] == "red"
    assert "image 1 is not a PNG, JPEG or WebP image" in caplog.text


def test_more_than_eight_images_returns_null(drex, staged_images, caplog):
    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        assert call("drex_decide_image", [("", ["scoped://red.png"] * 9, which(1), None)]) == [None]
    assert "at most 8" in caplog.text
    assert drex.log() == []


def test_images_must_be_file_urls(drex, staged_images):
    with pytest.raises(DrexError, match="images must be a scoped file URL"):
        call("drex_decide_image", [("", [{"url": "scoped://red.png"}], which(1), None)])


def test_an_image_that_cannot_be_read_fails_the_query_naming_scoped_urls(drex, monkeypatch):
    # What Snowflake raises for a stage file URL (BUILD_STAGE_FILE_URL), which SnowflakeFile doesn't open
    def refuse(url):
        raise SystemError("<built-in function open_stream> returned a result with an exception set")

    monkeypatch.setattr(drex_snowflake, "_read_staged_file", refuse)
    stage_url = "https://example.snowflakecomputing.com/api/files/DREX/PUBLIC/DREX_IMAGES/red.png"

    with pytest.raises(DrexError, match=r"could not read image 1: .*scoped file URLs from BUILD_SCOPED_FILE_URL"):
        call("drex_decide_image", [("", stage_url, which(1), None)])
    assert drex.log() == []


# ---------------------------------------------------------------------------------------------------------------------
# Errors that fail the query
# ---------------------------------------------------------------------------------------------------------------------


def test_401_fails_the_query(drex):
    drex.use("401")
    with pytest.raises(DrexError, match=r"^drex_noul: Invalid API key\..* \(request req_"):
        call("drex_noul", [(TICKET, "Refund?", None, None)])


def test_402_fails_the_query_with_the_billing_page(drex):
    drex.use("402")
    with pytest.raises(DrexError) as error:
        call("drex_noul", [(TICKET, "Refund?", None, None)])
    assert "insufficient available funds" in str(error.value)
    assert f"Billing: {drex.mock_url}/ns/{drex.ns}/402/dashboard/billing" in str(error.value)


def test_a_422_about_the_query_surfaces_each_issue(drex):
    drex.use("422")
    with pytest.raises(DrexError) as error:
        call("drex_noul", [(TICKET, "Refund?", None, None)])
    assert str(error.value).startswith(
        "drex_noul: Drex refused the request: questions.a.instructions: must be a string; "
        "questions.a.instructions: noul needs instructions or criteria (request req_"
    )


def test_an_unknown_model_fails_the_query_before_any_request(drex):
    with pytest.raises(DrexError, match=r"^drex_choice: unknown model 'drex-v9'; GET /v1/models lists drex-v1.5, "):
        call("drex_choice", [(TICKET, "Which?", ["a", "b"], "drex-v9")])
    assert drex.log() == []


def test_a_model_missing_from_the_cached_list_is_looked_up_again(drex):
    call("drex_noul", [(TICKET, "Refund?", None, None)])
    with pytest.raises(DrexError, match="unknown model 'drex-v9'"):
        call("drex_noul", [(TICKET, "Refund?", None, "drex-v9")])

    assert len(drex.log("/v1/models")) == 2


def test_the_model_list_is_cached(drex):
    call("drex_noul", [(TICKET, "Refund?", None, None)])
    call("drex_noul", [(TICKET, "Refund?", None, "drex-v2.0")])

    assert len(drex.log("/v1/models")) == 1


def test_proto_is_refused_as_a_question_id(drex):
    with pytest.raises(DrexError, match="the question id '__proto__' is not allowed"):
        call("drex_decide", [(TICKET, {"__proto__": {"type": "noul", "instructions": "Refund?"}}, None)])
    assert drex.log() == []


@pytest.mark.parametrize(
    ("function", "row", "message"),
    [
        ("drex_noul", (TICKET, "", None, None), "^drex_noul: noul needs instructions, or a description of true or"),
        (
            "drex_noul",
            (TICKET, "Refund?", ["true"], None),
            r"^drex_noul: criteria labels must be exactly true and false, got \[true\]",
        ),
        ("drex_noul", (TICKET, "Refund?", ["yes", "no"], None), "must be exactly true and false"),
        ("drex_choice", (TICKET, "Which?", ["a"], None), "labels takes 2 to 255 labels, got 1"),
        (
            "drex_choice",
            (TICKET, "Which?", ["a", "a"], None),
            "^drex_choice: labels must be unique; 'a' is a duplicate",
        ),
        ("drex_choice", (TICKET, "Which?", ["a", ""], None), "^drex_choice: labels must not be empty"),
        ("drex_choice", (TICKET, "Which?", None, None), "labels must not be NULL"),
        ("drex_choice", (TICKET, "Which?", ["a", None], None), "^drex_choice: labels must not be NULL"),
        ("drex_score", (TICKET, "How?", ["low", ""], None), "^drex_score: levels must not be empty"),
        ("drex_choice", (TICKET, "Which?", "not json", None), "labels is not valid JSON"),
        ("drex_choice", (TICKET, "Which?", [str(i) for i in range(256)], None), "got 256"),
        ("drex_score", (TICKET, "How?", {"low": None, "high": None}, None), "levels must be a JSON array of labels"),
        ("drex_score", (TICKET, "How?", [{"label": "low", "description": "x"}, "high"], None), "take no description"),
        ("drex_choice", (TICKET, 5, ["a", "b"], None), "instructions must be a VARCHAR"),
    ],
)
def test_invalid_constants_fail_the_query_before_any_request(drex, function, row, message):
    with pytest.raises(DrexError, match=message):
        call(function, [row])
    assert drex.log() == []


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ({}, "questions takes 1 to 512 questions, got 0"),
        ({f"q{i}": {"type": "noul", "instructions": "?"} for i in range(513)}, "got 513"),
        ({" ": {"type": "noul", "instructions": "?"}}, "question ids must not be empty"),
        ({"a": {"type": "noul", "instructions": "?"}, "A": {"type": "noul", "instructions": "?"}}, "ignoring case"),
        ({"a": {"type": "multiple_choice"}}, "multiple_choice questions are not supported"),
        ({"a": {"type": "box"}}, "box questions are not supported"),
        ({"a": {"type": "rank"}}, "questions.a.type must be noul, choice or score, not 'rank'"),
        ({"a": {"type": "noul", "instructions": "?", "weight": 1}}, "unknown key 'weight'"),
        ({"a": {"type": "choice", "instructions": "?"}}, "questions.a needs criteria"),
        ({"a": {"type": "noul", "instructions": 3}}, "questions.a.instructions must be a string"),
        (["a"], "questions must be a JSON object"),
    ],
)
def test_invalid_questions_fail_the_query(drex, questions, message):
    with pytest.raises(DrexError, match=message):
        call("drex_decide", [(TICKET, questions, None)])
    assert drex.log() == []


def test_a_bad_constant_on_any_row_fails_the_query(drex):
    rows = [(TICKET, "Which?", ["a", "b"], None), (TICKET, "Which?", ["a"], None)]
    with pytest.raises(DrexError, match="got 1"):
        call("drex_choice", rows)


def test_concurrency_must_be_at_least_one():
    with pytest.raises(DrexError, match="concurrency must be at least 1"):
        drex_snowflake.handler("drex_noul", concurrency=0)


def test_a_missing_key_fails_the_query(drex, monkeypatch):
    monkeypatch.setenv("NACE_API_KEY", "  ")
    with pytest.raises(DrexError, match="no API key"):
        call("drex_noul", [(TICKET, "Refund?", None, None)])


# ---------------------------------------------------------------------------------------------------------------------
# Retries and rows that return NULL
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("scenario", "wait_ms"), [("429-once", 1000), ("529-once", 2000)])
def test_429_and_529_are_retried_after_retry_after_ms(drex, scenario, wait_ms):
    drex.use(scenario)

    assert call("drex_noul", [(TICKET, "Refund?", None, None)]) == [0.75]

    first, retry = drex.log()
    assert (first["status"], retry["status"]) == (int(scenario[:3]), 200)
    assert retry["body_sha256"] == first["body_sha256"]
    assert retry["waited_ms"] >= wait_ms


def test_a_row_out_of_retries_returns_null(drex, caplog):
    drex.use("529-always")

    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        assert call("drex_choice", [(TICKET, "Which?", ["a", "b"], None)]) == [None]

    assert len(drex.log()) == drex_snowflake.MAX_ATTEMPTS
    assert "no answer after its retries, the last HTTP 529: Drex is at capacity" in caplog.text


def test_an_oversized_row_returns_null_and_the_query_continues(drex, caplog):
    rows = [("x" * (4 * 1024 * 1024), "Refund?", None, None), (TICKET, "Refund?", None, None)]

    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        results = call("drex_noul", rows)

    assert math.isnan(results[0]) and results[1] == 0.75
    assert len(drex.log()) == 1
    assert "over Drex's limit of 4194304" in caplog.text


def test_a_422_about_the_row_returns_null_without_its_content(drex, caplog):
    drex.use("too-long")
    rows = [({"secret_key_name": "x" * 8000}, "Refund?", None, None), (TICKET, "Refund?", None, None)]

    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        results = call("drex_noul", rows)

    assert math.isnan(results[0]) and results[1] == 0.75
    assert "HTTP 422" in caplog.text
    assert "secret_key_name" not in caplog.text


def test_rows_past_the_batch_deadline_return_null(drex, monkeypatch, caplog):
    monkeypatch.setattr(drex_snowflake, "BATCH_BUDGET_S", 1.0)

    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        assert call("drex_choice", [(TICKET, "Which?", ["a", "b"], None)]) == [None]
    assert "Snowflake's 180-second limit" in caplog.text
    assert drex.log() == []


def test_the_concurrency_cap_holds(drex):
    drex.use("slow-200")
    rows = [(f"{TICKET} {i}", "Refund?", None, None) for i in range(24)]

    assert call("drex_noul", rows, concurrency=3) == [0.75] * 24

    peak = max(r["in_flight"] for r in drex.log())
    assert peak == 3


def test_the_default_concurrency_is_eight(drex):
    drex.use("slow-200")
    call("drex_noul", [(f"{TICKET} {i}", "Refund?", None, None) for i in range(20)])

    assert max(r["in_flight"] for r in drex.log()) == 8


def test_handlers_ask_for_batches_that_fit_the_time_limit():
    assert drex_snowflake.handler("drex_noul")._sf_max_batch_size == 64
    assert drex_snowflake.handler("drex_decide_image")._sf_max_batch_size == 16


# ---------------------------------------------------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------------------------------------------------


def test_open_router_takes_models_under_nace_ai_and_images_in_state(drex, staged_images):
    drex.use_open_router()

    [noul] = call("drex_noul", [(TICKET, None, {"true": "Asks for a refund", "false": None}, None)])
    [body] = call("drex_decide_image", [("Two images", ["scoped://blue.png", "scoped://red.png"], which(1), None)])

    assert noul == 0.75
    assert body["answers"]["which"]["choice"] == "blue"
    assert body["model"] == "nace-ai/drex-max"
    text, image = drex.log()
    assert text["open_router"] and image["open_router"]
    assert (text["model"], image["model"]) == ("nace-ai/drex-v2.0", "nace-ai/drex-max")
    assert image["image_colours"] == ["blue", "red"]
    assert json.loads(image["body"])["state"][0] == {"type": "text", "text": "Two images"}
    for record in (text, image):
        assert record["http_referer"] == "https://nace.ai"
        assert record["x_openrouter_title"] == "Drex for Snowflake"


def test_open_router_attribution_is_set_by_the_caller(drex, monkeypatch):
    drex.use_open_router()
    monkeypatch.setenv("NACE_OPENROUTER_REFERER", "https://app.example")
    monkeypatch.setenv("NACE_OPENROUTER_TITLE", "Example")

    assert call("drex_noul", [(TICKET, "Refund?", None, None)]) == [0.75]

    sent = drex.log("/v1/systemone") + drex.log("/v1/models")
    assert len(sent) == 2
    assert {(r["http_referer"], r["x_openrouter_title"]) for r in sent} == {("https://app.example", "Example")}


def test_a_nace_host_gets_no_attribution_headers(drex, monkeypatch):
    monkeypatch.setenv("NACE_OPENROUTER_REFERER", "https://app.example")
    monkeypatch.setenv("NACE_OPENROUTER_TITLE", "Example")

    assert call("drex_noul", [(TICKET, "Refund?", None, None)]) == [0.75]

    assert {(r["http_referer"], r["x_openrouter_title"]) for r in drex.log()} == {(None, None)}


def test_a_nace_key_is_never_sent_to_open_router(drex, monkeypatch):
    drex.use_open_router()
    monkeypatch.setenv("NACE_API_KEY", KEY)

    with pytest.raises(DrexError, match="never sent to OpenRouter"):
        call("drex_noul", [(TICKET, "Refund?", None, None)])

    assert drex.log("/v1/systemone") == drex.log("/v1/models") == []


def test_open_router_prefers_its_own_key(drex, monkeypatch):
    drex.use_open_router()
    monkeypatch.setenv("NACE_API_KEY", KEY)
    monkeypatch.setenv("OPENROUTER_API_KEY", OPEN_ROUTER_KEY)

    assert call("drex_noul", [(TICKET, "Refund?", None, None)]) == [0.75]

    assert {r["authorization_header"] for r in drex.log()} == {f"Bearer {OPEN_ROUTER_KEY}"}


def test_an_open_router_key_in_nace_api_key_still_works(drex):
    drex.use_open_router()

    assert call("drex_noul", [(TICKET, "Refund?", None, None)]) == [0.75]

    assert {r["authorization_header"] for r in drex.log()} == {f"Bearer {OPEN_ROUTER_KEY}"}


class FakeSnowflake:
    """`_snowflake` as a function created with some secrets sees it: a secret it was not given raises."""

    def __init__(self, **secrets):
        self.secrets = secrets

    def get_generic_secret_string(self, name):
        return self.secrets[name]


def test_the_functions_secrets_set_the_attribution_and_a_function_without_them_uses_the_defaults(monkeypatch):
    base = {"nace": OPEN_ROUTER_KEY, "nace_base_url": "https://openrouter.ai/api"}
    app = {"nace_openrouter_referer": "https://app.example", "nace_openrouter_title": "Example"}

    monkeypatch.setitem(sys.modules, "_snowflake", FakeSnowflake(**base, **app))
    creds = drex_snowflake.credentials()
    assert (creds.referer, creds.title) == ("https://app.example", "Example")
    assert drex_snowflake.Drex(creds)._headers == {
        "HTTP-Referer": "https://app.example",
        "X-OpenRouter-Title": "Example",
    }

    monkeypatch.setitem(sys.modules, "_snowflake", FakeSnowflake(**base))
    creds = drex_snowflake.credentials()
    assert drex_snowflake.Drex(creds)._headers == {
        "HTTP-Referer": "https://nace.ai",
        "X-OpenRouter-Title": "Drex for Snowflake",
    }


def test_a_nace_key_in_the_secret_is_refused_for_open_router(monkeypatch):
    secrets = {"nace": KEY, "nace_base_url": "https://openrouter.ai/api"}
    monkeypatch.setitem(sys.modules, "_snowflake", FakeSnowflake(**secrets))

    with pytest.raises(DrexError, match="never sent to OpenRouter"):
        drex_snowflake.credentials()


def test_open_router_402_points_at_its_credits():
    refusal = NaceInsufficientCreditError(402, {"error": {"message": "No credits."}}, httpx.Headers())
    error = drex_snowflake.fatal_or_none("drex_noul", refusal, "https://openrouter.ai/api")
    assert str(error) == "drex_noul: No credits. Billing: https://openrouter.ai/settings/credits"


def test_a_tls_failure_fails_the_query_without_retrying(drex, monkeypatch):
    # The mock speaks plain HTTP, so a TLS handshake with it fails
    monkeypatch.setenv("NACE_BASE_URL", drex.mock_url.replace("http://", "https://"))

    with pytest.raises(DrexError, match=r"^drex_noul: could not reach Drex over TLS: .*SSL"):
        call("drex_noul", [(TICKET, "Refund?", None, None)])


def test_no_response_is_retried_then_returns_null(drex, monkeypatch, caplog):
    call("drex_noul", [(TICKET, "Refund?", None, None)])  # lists the models while the mock answers
    client = drex_snowflake.get_client()
    attempts = []

    def refuse(request, **kwargs):
        attempts.append(request.url.path)
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(client.http, "send", refuse)
    policy = drex_snowflake.retry_policy
    # Without the backoff, so the test doesn't wait
    monkeypatch.setattr(
        drex_snowflake, "retry_policy", lambda budget: dataclasses.replace(policy(budget), backoff_initial=0.0)
    )

    with caplog.at_level(logging.WARNING, logger="drex_snowflake"):
        assert math.isnan(call("drex_noul", [(TICKET, "Refund?", None, None)])[0])
    assert len(attempts) == drex_snowflake.MAX_ATTEMPTS
    assert "no answer after its retries, the last a connection error" in caplog.text
