"""Drex for Snowflake: the handler behind the drex_* vectorized Python UDFs that sql/setup.sql creates.

Each function's inline handler is two lines, `import drex_snowflake` and
`drex_noul = drex_snowflake.handler("drex_noul", concurrency=8)`; this module, staged beside the functions, does the
rest. It sends each row of a batch to Drex's `POST /v1/systemone` through nace-sdk, at most `concurrency` at once, and
follows drex-duckdb's rules: constant arguments are checked before any request, a bad one fails the query, and a row
Drex can't answer returns NULL with a warning.

Snowflake gives every batch 180 seconds, so a batch stops sending new rows, and gives up on retries, before then.
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import httpx
import pandas
from nace_sdk import (
    NaceAPIConnectionError,
    NaceAPIError,
    NaceAPIResponseValidationError,
    NaceAPITimeoutError,
    NaceClient,
    NaceError,
    RetryPolicy,
    image_input,
)
from pydantic import RootModel
from pydantic_core import to_json

__version__ = "0.2.0"
#: Sent as X-Nace-Client, so Drex records this integration with each request
CLIENT_INFO = f"drex-snowflake/{__version__}"

DEFAULT_MODEL = "drex-v2.0"
DEFAULT_IMAGE_MODEL = "drex-max"
DEFAULT_BASE_URL = "https://console.nace.ai"
DEFAULT_CONCURRENCY = 8

#: Drex's limits: questions per request, labels per choice or levels per score, images per request and their sizes
MAX_QUESTIONS = 512
MIN_LABELS = 2
MAX_LABELS = 255
MAX_IMAGES = 8
MAX_IMAGE_BYTES = 16 * 1024 * 1024
#: Drex refuses a larger body
MAX_BODY_BYTES = 4 * 1024 * 1024

#: The versions that read images. Drex's GET /v1/models has no image field, so this module keeps the list; a listed
#: model reads images when it is one of these or its alias_for names one. OpenRouter lists each model's modalities
IMAGE_MODELS = frozenset({"drex-max", "drex-max-v1.1"})

#: Seconds for one attempt; Drex waits up to 55 for the model before it answers 529
REQUEST_TIMEOUT_S = 60.0
#: Attempts per request, and the seconds from its first attempt after which no retry starts
MAX_ATTEMPTS = 5
REQUEST_BUDGET_S = 120.0
RETRY_STATUSES = frozenset({408, 429, 500, 502, 503, 504, 529})
#: Snowflake ends a batch's handler call after 180 seconds; this leaves room to answer
BATCH_BUDGET_S = 165.0
#: A row whose batch has less time left than this is not sent
MIN_SEND_S = 5.0

OPENROUTER_REFERER = "https://nace.ai"
OPENROUTER_TITLE = "Drex for Snowflake"
OPENROUTER_BILLING_URL = "https://openrouter.ai/settings/credits"

logger = logging.getLogger("drex_snowflake")


class DrexError(Exception):
    """An error that fails the query. Its message starts with the function's name."""


# ---------------------------------------------------------------------------------------------------------------------
# Questions
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Label:
    """One label of a noul, choice or score: a plain label, or a label with a description."""

    label: str
    #: None when the label has no description
    description: str | None = None


@dataclass
class Question:
    id: str
    type: str
    instructions: str = ""
    labels: list[Label] = field(default_factory=list)
    #: Whether the labels were written as Drex's object of label to description, where a noul may describe only one
    labels_from_object: bool = False


def _is_null(value: Any) -> bool:
    """Whether a value from a batch is SQL NULL, which pandas may give as None, NA or NaN."""
    return value is None or value is pandas.NA or (isinstance(value, float) and math.isnan(value))


def _parse_json(function: str, name: str, value: Any) -> Any:
    """A VARIANT argument, or a VARCHAR holding its JSON text."""
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except ValueError as error:
        raise DrexError(f"{function}: {name} is not valid JSON: {error}") from None


def _labels(function: str, name: str, value: Any, allow_object: bool) -> list[Label]:
    """The labels of criteria in drex-duckdb's JSON forms: an array of labels or of {label, description} objects, or,
    for a noul or a choice, Drex's own object of label to description."""
    shape = f"{function}: {name} must be a JSON array of labels or of {{label, description}} objects" + (
        ", or an object of label to description" if allow_object else ""
    )

    def text_or_null(field_value: Any) -> str | None:
        if field_value is None:
            return None
        if not isinstance(field_value, str):
            raise DrexError(shape)
        return field_value

    # A helper's argument is itself named labels or levels; a question's criteria are named for the question
    what = name if name in ("labels", "levels") else f"{name} labels"

    def make(label: str | None, description: str | None) -> Label:
        if label is None:
            raise DrexError(f"{function}: {what} must not be NULL")
        return Label(label, description)

    if isinstance(value, list):
        labels = []
        for element in value:
            if isinstance(element, Mapping):
                if not set(element) <= {"label", "description"}:
                    raise DrexError(shape)
                labels.append(make(text_or_null(element.get("label")), text_or_null(element.get("description"))))
            else:
                labels.append(make(text_or_null(element), None))
        return labels
    if isinstance(value, Mapping) and allow_object:
        return [make(key, text_or_null(description)) for key, description in value.items()]
    raise DrexError(shape)


def _check_labels(function: str, name: str, question: Question) -> None:
    """Drex's rules for a question's labels."""
    labels = question.labels
    if question.type == "noul":
        # No labels is a noul without criteria
        found = [label.label for label in labels]
        valid = all(label in ("true", "false") for label in found) and len(set(found)) == len(found)
        complete = not found or question.labels_from_object or set(found) == {"true", "false"}
        if not valid or not complete:
            raise DrexError(f"{function}: {name} labels must be exactly true and false, got [{', '.join(found)}]")
        return
    kind = "labels" if question.type == "choice" else "levels"
    what = name if name == kind else f"{name} {kind}"
    if not MIN_LABELS <= len(labels) <= MAX_LABELS:
        raise DrexError(f"{function}: {name} takes {MIN_LABELS} to {MAX_LABELS} {kind}, got {len(labels)}")
    seen = set()
    for label in labels:
        if not label.label:
            raise DrexError(f"{function}: {what} must not be empty")
        if label.label in seen:
            raise DrexError(f"{function}: {what} must be unique; '{label.label}' is a duplicate")
        seen.add(label.label)
        if question.type == "score" and label.description is not None:
            raise DrexError(f"{function}: score levels take no description; describe them in instructions")


def _check_noul_has_text(function: str, name: str, question: Question) -> None:
    """What Drex refuses in a noul: neither instructions nor a description of true or false."""
    if question.type != "noul" or question.instructions:
        return
    if not any(label.description for label in question.labels):
        raise DrexError(f"{function}: {name} needs instructions, or a description of true or false")


def _question_type(function: str, name: str, value: Any) -> str:
    if value in ("noul", "choice", "score"):
        return value
    if value in ("multiple_choice", "box"):
        raise DrexError(f"{function}: {name}: {value} questions are not supported, since Drex has no {value} question")
    if not isinstance(value, str):
        raise DrexError(f"{function}: {name}.type must be noul, choice or score")
    raise DrexError(f"{function}: {name}.type must be noul, choice or score, not '{value}'")


def helper_question(function: str, kind: str, instructions: Any, criteria: Any) -> Question:
    """The one question of drex_noul, drex_choice or drex_score."""
    if _is_null(instructions):
        instructions = ""
    elif not isinstance(instructions, str):
        raise DrexError(f"{function}: instructions must be a VARCHAR")
    name = "criteria" if kind == "noul" else ("labels" if kind == "choice" else "levels")
    question = Question(id=kind, type=kind, instructions=instructions)
    if _is_null(criteria):
        if kind != "noul":
            raise DrexError(f"{function}: {name} must not be NULL")
    else:
        criteria = _parse_json(function, name, criteria)
        question.labels = _labels(function, name, criteria, allow_object=kind != "score")
        question.labels_from_object = isinstance(criteria, Mapping)
    _check_questions(function, [question], named=False, labels_name=name)
    return question


def parse_questions(function: str, value: Any) -> list[Question]:
    """The questions of drex_decide and drex_decide_image: a JSON object of id to {type, instructions, criteria}."""
    value = _parse_json(function, "questions", value)
    if not isinstance(value, Mapping):
        raise DrexError(f"{function}: questions must be a JSON object of question id to question")
    questions = []
    for qid, entry in value.items():
        name = f"questions.{qid}"
        if not isinstance(entry, Mapping):
            raise DrexError(f"{function}: {name} must be an object with type, instructions and criteria")
        for key in entry:
            if key not in ("type", "instructions", "criteria"):
                raise DrexError(f"{function}: {name} has an unknown key '{key}'; use type, instructions and criteria")
        question = Question(id=qid, type=_question_type(function, name, entry.get("type")))
        instructions = entry.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str):
                raise DrexError(f"{function}: {name}.instructions must be a string")
            question.instructions = instructions
        criteria = entry.get("criteria")
        if criteria is not None:
            question.labels = _labels(function, f"{name}.criteria", criteria, allow_object=question.type != "score")
            question.labels_from_object = isinstance(criteria, Mapping)
        elif question.type != "noul":
            raise DrexError(f"{function}: {name} needs criteria")
        questions.append(question)
    _check_questions(function, questions, named=True)
    return questions


def _check_questions(function: str, questions: list[Question], named: bool, labels_name: str | None = None) -> None:
    """Drex's rules for questions. A helper's errors about its labels name its argument, `labels_name`."""
    if not 1 <= len(questions) <= MAX_QUESTIONS:
        raise DrexError(f"{function}: questions takes 1 to {MAX_QUESTIONS} questions, got {len(questions)}")
    ids = set()
    for question in questions:
        name = f"questions.{question.id}" if named else question.id
        if not question.id.strip():
            raise DrexError(f"{function}: question ids must not be empty")
        # Drex collects answers in a plain object, whose __proto__ is not a key, so it would bill the request and drop
        # the answer
        if question.id == "__proto__":
            raise DrexError(f"{function}: the question id '__proto__' is not allowed: Drex can't answer it")
        if question.id.lower() in ids:
            raise DrexError(f"{function}: question ids must be unique ignoring case; '{question.id}' is a duplicate")
        ids.add(question.id.lower())
        _check_labels(function, labels_name or name, question)
        _check_noul_has_text(function, name, question)


def questions_wire(questions: list[Question]) -> dict[str, Any]:
    """The `questions` object Drex takes, with `instructions` always a string, as OpenRouter requires."""
    wire: dict[str, Any] = {}
    for question in questions:
        entry: dict[str, Any] = {"type": question.type, "instructions": question.instructions}
        if question.type == "score":
            entry["criteria"] = [label.label for label in question.labels]
        elif question.type == "choice":
            entry["criteria"] = {label.label: label.description for label in question.labels}
        elif any(label.description is not None for label in question.labels):
            # A noul's labels alone are the same as no criteria
            entry["criteria"] = {label.label: label.description for label in question.labels}
        wire[question.id] = entry
    return wire


# ---------------------------------------------------------------------------------------------------------------------
# Answers
# ---------------------------------------------------------------------------------------------------------------------


def _number(function: str, question: Question, container: Any, key: str) -> float:
    value = container.get(key) if isinstance(container, Mapping) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DrexError(f"{function}: Drex's answer to question '{question.id}' has no number {key}")
    return float(value)


def answer_value(function: str, question: Question, body: Any) -> float | dict[str, Any]:
    """Drex's answer to one question: a noul's probability, a choice's probabilities sorted by label and a score's by
    index. Every value comes from the response."""
    answers = body.get("answers") if isinstance(body, Mapping) else None
    answer = answers.get(question.id) if isinstance(answers, Mapping) else None
    if not isinstance(answer, Mapping):
        raise DrexError(f"{function}: Drex's response has no answer to question '{question.id}'")
    if question.type == "noul":
        return _number(function, question, answer, "noul")
    probabilities = answer.get("probabilities")
    confidence = _number(function, question, answer, "confidence")
    if question.type == "choice":
        choice = answer.get("choice")
        if not isinstance(choice, str):
            raise DrexError(f"{function}: Drex's answer to question '{question.id}' has no choice")
        rows = [
            {"value": label, "probability": _number(function, question, probabilities, label)}
            for label in sorted(label.label for label in question.labels)
        ]
        return {"choice": choice, "probabilities": rows, "confidence": confidence}
    rows = [
        {"index": index, "value": label.label, "probability": _number(function, question, probabilities, str(index))}
        for index, label in enumerate(question.labels)
    ]
    return {"score": _number(function, question, answer, "score"), "probabilities": rows, "confidence": confidence}


# ---------------------------------------------------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------------------------------------------------


def is_open_router(base_url: str) -> bool:
    """Whether the base URL is OpenRouter's (https://openrouter.ai/api), which takes a different body and headers."""
    host = (httpx.URL(base_url).host or "").lower()
    return host == "openrouter.ai" or host.endswith(".openrouter.ai")


@dataclass(frozen=True)
class Credentials:
    api_key: str = field(repr=False)
    base_url: str
    #: The app identity OpenRouter attributes requests to; "" for Nace's
    referer: str = ""
    title: str = ""


def _optional_secret(snowflake_module, name: str) -> str:
    """A secret a function may have been created without (before it existed), "" then."""
    try:
        return (snowflake_module.get_generic_secret_string(name) or "").strip()
    except Exception:  # noqa: BLE001 - a function created without the secret uses the default
        return ""


def credentials() -> Credentials:
    """The key and base URL from the function's secrets `nace` and `nace_base_url`, and on OpenRouter the optional
    `nace_openrouter_referer` and `nace_openrouter_title`; outside Snowflake, from NACE_API_KEY, NACE_BASE_URL,
    NACE_OPENROUTER_REFERER and NACE_OPENROUTER_TITLE, and OPENROUTER_API_KEY before NACE_API_KEY on OpenRouter.
    A `nace_` key is never sent to OpenRouter."""
    try:
        import _snowflake  # noqa: PLC0415 - exists only inside Snowflake
    except ImportError:
        api_key, base_url = os.environ.get("NACE_API_KEY", ""), os.environ.get("NACE_BASE_URL", "")
        referer, title = os.environ.get("NACE_OPENROUTER_REFERER", ""), os.environ.get("NACE_OPENROUTER_TITLE", "")
        openrouter_key = os.environ.get("OPENROUTER_API_KEY", "")
    else:
        api_key = _snowflake.get_generic_secret_string("nace")
        base_url = _optional_secret(_snowflake, "nace_base_url")
        referer = _optional_secret(_snowflake, "nace_openrouter_referer")
        title = _optional_secret(_snowflake, "nace_openrouter_title")
        openrouter_key = ""
    base_url = (base_url or "").strip().rstrip("/") or DEFAULT_BASE_URL
    api_key = (api_key or "").strip()
    if is_open_router(base_url):
        api_key = (openrouter_key or "").strip() or api_key
        if api_key.startswith("nace_"):
            raise DrexError(
                "drex: a Nace API key (nace_...) is never sent to OpenRouter; store an OpenRouter key in the "
                "'nace' secret (OPENROUTER_API_KEY outside Snowflake)"
            )
    if not api_key:
        raise DrexError("drex: no API key; set the secret the function names as 'nace' to a nace_sk_ key")
    return Credentials(api_key, base_url, (referer or "").strip(), (title or "").strip())


@dataclass(frozen=True)
class ListedModel:
    #: The name a request sends
    name: str
    reads_images: bool


class Drex:
    """A nace-sdk client for one key and base URL, and the models its base URL lists."""

    def __init__(self, creds: Credentials) -> None:
        self.base_url = creds.base_url
        self.open_router = is_open_router(creds.base_url)
        self._api_key = creds.api_key
        self._headers = (
            {
                "HTTP-Referer": creds.referer or OPENROUTER_REFERER,
                "X-OpenRouter-Title": creds.title or OPENROUTER_TITLE,
            }
            if self.open_router
            else {}
        )
        self.http = httpx.Client(timeout=REQUEST_TIMEOUT_S)
        try:
            self.client = NaceClient(
                api_key=creds.api_key,
                base_url=creds.base_url,
                headers=self._headers,
                client_info=CLIENT_INFO,
                retry=retry_policy(REQUEST_BUDGET_S),
                http_client=self.http,
            )
        except NaceError as error:
            raise DrexError(f"drex: {error}") from None
        self._models: dict[str, ListedModel] | None = None
        self._models_lock = threading.Lock()

    def routed(self, model: str) -> str:
        """The name a request for `model` is sent as: on OpenRouter, a name without an owner is under nace-ai/."""
        return f"nace-ai/{model}" if self.open_router and "/" not in model else model

    def find_model(self, function: str, model: str) -> ListedModel:
        """The listed model `model` is sent as. Drex answers any unregistered drex-* name with a 422 that would fail
        every row, so the name is checked first. The list is cached, and fetched again for a model it lacks, so a model
        released since the last fetch is found."""
        with self._models_lock:
            name = self.routed(model)
            if self._models is None or name not in self._models:
                self._models = self._fetch_models(function)
            listed = self._models.get(name)
            if listed is None:
                raise DrexError(f"{function}: unknown model '{model}'; GET /v1/models lists {', '.join(self._models)}")
            return listed

    def _fetch_models(self, function: str) -> dict[str, ListedModel]:
        if self.open_router:
            return self._fetch_open_router_models(function)
        try:
            listed = self.client.models.list()
        except NaceError as error:
            raise fatal_or_none(function, error, self.base_url) or DrexError(
                f"{function}: could not list Drex's models: {describe(error)}"
            ) from None
        models = {}
        for model in listed.models:
            models[model.name] = ListedModel(model.name, (model.alias_for or model.name) in IMAGE_MODELS)
        return models

    def _fetch_open_router_models(self, function: str) -> dict[str, ListedModel]:
        """OpenRouter's list, of which only Nace's models are Drex. It lists them only when asked for decision models.
        One attempt: the list is fetched once per process."""
        try:
            response = self.http.get(
                f"{self.base_url}/v1/models",
                params={"output_modalities": "decisions"},
                headers={"Authorization": f"Bearer {self._api_key}", "Accept": "application/json", **self._headers},
            )
        except httpx.HTTPError as error:
            raise DrexError(f"{function}: could not list OpenRouter's models: {type(error).__name__}") from None
        if response.status_code != 200:
            raise DrexError(f"{function}: could not list OpenRouter's models: HTTP {response.status_code}")
        try:
            data = response.json()["data"]
            models = {}
            for entry in data:
                if entry["id"].startswith("nace-ai/"):
                    modalities = (entry.get("architecture") or {}).get("input_modalities") or []
                    models[entry["id"]] = ListedModel(entry["id"], "image" in modalities)
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DrexError(f"{function}: OpenRouter's GET /v1/models answered with an unexpected body") from None
        return models

    def close(self) -> None:
        self.client.close()


_clients: dict[Credentials, Drex] = {}
_clients_lock = threading.Lock()


def get_client() -> Drex:
    """The client for the current secrets, kept for the life of the handler process so its connections are reused."""
    creds = credentials()
    with _clients_lock:
        client = _clients.get(creds)
        if client is None:
            for stale in _clients.values():
                stale.close()
            _clients.clear()
            client = _clients[creds] = Drex(creds)
        return client


# ---------------------------------------------------------------------------------------------------------------------
# Retries and errors
# ---------------------------------------------------------------------------------------------------------------------


def _is_tls_failure(error: BaseException) -> bool:
    """Whether a connection failed in the TLS handshake or on the certificate, which no retry fixes."""
    if not isinstance(error, NaceAPIConnectionError) or isinstance(error, NaceAPITimeoutError):
        return False
    text = f"{error} {error.__cause__ or ''}".lower()
    return "ssl" in text or "certificate" in text


def _retry_connection_error(error: BaseException) -> bool:
    """No response: a connection error other than TLS. Timeouts are retried by the policy itself."""
    return (
        isinstance(error, NaceAPIConnectionError)
        and not isinstance(error, NaceAPITimeoutError)
        and not (_is_tls_failure(error))
    )


def retry_policy(budget_s: float) -> RetryPolicy:
    """drex-duckdb's retries: up to 5 attempts while no response, 408, 429, 500, 502, 503, 504 or 529, waiting for
    retry-after-ms (or retry-after), else 1, 2, 4 and 8 s less up to 25 % jitter; no retry starts `budget_s` after the
    first attempt."""
    if budget_s < 1:
        return RetryPolicy(max_retries=0, timeout=None)
    return RetryPolicy(
        max_retries=MAX_ATTEMPTS - 1,
        backoff_initial=1.0,
        backoff_max=8.0,
        backoff_jitter=0.25,
        http_statuses=set(RETRY_STATUSES),
        respect_retry_after=True,
        api_connection_error=False,
        api_timeout_error=True,
        predicate=_retry_connection_error,
        timeout=budget_s,
    )


@dataclass(frozen=True)
class _ErrorBody:
    message: str
    issues: list[tuple[str, str]]
    request_id: str


def _error_body(error: NaceAPIError) -> _ErrorBody:
    """Drex's error envelope: {"error": {"type", "message", "issues": [{"path", "message"}]}, "request_id"}. Only these
    fields are read, never the raw body, which may quote the row."""
    body = error.body if isinstance(error.body, Mapping) else {}
    envelope = body.get("error")
    message = envelope if isinstance(envelope, str) else ""
    issues = []
    if isinstance(envelope, Mapping):
        message = envelope.get("message") if isinstance(envelope.get("message"), str) else ""
        for issue in error.issues:
            path, text = issue.get("path"), issue.get("message")
            issues.append((path if isinstance(path, str) else "", text if isinstance(text, str) else ""))
    request_id = body.get("request_id") if isinstance(body.get("request_id"), str) else ""
    return _ErrorBody(message, issues, request_id)


def _issue_text(path: str, message: str) -> str:
    return f"{path}: {message}" if path else message


def _is_under(path: str, name: str) -> bool:
    return path == name or path.startswith(name + ".")


def _refuses_row(body: _ErrorBody) -> bool:
    """Whether a 422 is about the row rather than the query's constants (questions, model): a request over the token
    limit (Drex's documented `Request too long:`), or issues only under `state` and `images`."""
    if body.message.startswith("Request too long:"):
        return True
    return bool(body.issues) and all(_is_under(path, "state") or _is_under(path, "images") for path, _ in body.issues)


def _request_suffix(body: _ErrorBody) -> str:
    return f" (request {body.request_id})" if body.request_id else ""


def fatal_or_none(function: str, error: BaseException, base_url: str) -> DrexError | None:
    """The error that fails the query for an answer that does: 401, 402, a 422 that is not about the row alone, a TLS
    failure, or a 200 whose body is not Drex's. None for one that only loses its row."""
    if _is_tls_failure(error):
        return DrexError(f"{function}: could not reach Drex over TLS: {error}")
    if isinstance(error, NaceAPIResponseValidationError):
        return DrexError(f"{function}: Drex answered with a body that is not JSON")
    if not isinstance(error, NaceAPIError):
        # Refused by nace-sdk before sending, which the checks here should have caught
        return DrexError(f"{function}: {error}") if not isinstance(error, NaceAPIConnectionError) else None
    body = _error_body(error)
    request = _request_suffix(body)
    if error.status == 401:
        return DrexError(f"{function}: {body.message or 'the API key was refused (HTTP 401)'}{request}")
    if error.status == 402:
        billing = OPENROUTER_BILLING_URL if is_open_router(base_url) else f"{base_url}/dashboard/billing"
        message = body.message or "the account has no funds left (HTTP 402)."
        return DrexError(f"{function}: {message} Billing: {billing}{request}")
    if error.status == 422 and not _refuses_row(body):
        issues = "; ".join(_issue_text(path, text) for path, text in body.issues) or body.message or "HTTP 422"
        return DrexError(f"{function}: Drex refused the request: {issues}{request}")
    return None


def describe(error: BaseException) -> str:
    """Why a row has no answer, without the row's content: a path under `state` names the row's own JSON keys, so a
    422's issues keep only `state`."""
    if isinstance(error, NaceAPITimeoutError):
        return "no answer after its retries, the last timed out"
    if isinstance(error, NaceAPIConnectionError):
        return f"no answer after its retries, the last a connection error: {error}"
    if not isinstance(error, NaceAPIError):
        return str(error)
    body = _error_body(error)
    if error.status == 422:
        issues = [_issue_text("state" if _is_under(path, "state") else path, text) for path, text in body.issues]
        reason = "; ".join(issues)
    else:
        reason = body.message
    description = f"HTTP {error.status}" + (f": {reason}" if reason else "") + _request_suffix(body)
    return f"no answer after its retries, the last {description}" if error.status in RETRY_STATUSES else description


# ---------------------------------------------------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------------------------------------------------


class _Body(RootModel[dict[str, Any]]):
    """Drex's response body, as it came."""


def _media_type(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_staged_file(url: str) -> bytes:
    """A staged file's bytes, from a scoped file URL (BUILD_SCOPED_FILE_URL), which the caller's role must be able to
    read. At most one byte over Drex's limit per image is read."""
    from snowflake.snowpark.files import SnowflakeFile  # noqa: PLC0415 - exists only inside Snowflake

    with SnowflakeFile.open(url, "rb") as file:
        return file.read(MAX_IMAGE_BYTES + 1)


@dataclass
class Row:
    """One row's request. `images` are scoped file URLs, read when the row is sent."""

    state: Any
    questions: dict[str, Any]
    model: str
    images: list[str] = field(default_factory=list)


class _Batch:
    """Sends a batch's rows, at most `concurrency` at once, before the batch's deadline. The first error that fails the
    query stops every row not yet sent."""

    def __init__(self, function: str, drex: Drex, concurrency: int, deadline: float) -> None:
        self.function = function
        self.drex = drex
        self.concurrency = concurrency
        self.deadline = deadline
        self.failed = threading.Event()
        self.failure: DrexError | None = None
        self.lock = threading.Lock()

    def run(self, rows: list[Row | None]) -> list[dict[str, Any] | None]:
        results: list[dict[str, Any] | None] = [None] * len(rows)
        pending = [i for i, row in enumerate(rows) if row is not None]
        if not pending:
            return results

        def work(index: int) -> None:
            try:
                results[index] = self._send(rows[index])
            except DrexError as error:
                with self.lock:
                    self.failure = self.failure or error
                self.failed.set()

        with ThreadPoolExecutor(max_workers=min(self.concurrency, len(pending)), thread_name_prefix="drex") as pool:
            futures = [pool.submit(work, index) for index in pending]
        if self.failure is not None:
            raise self.failure
        for future in futures:
            # Anything else a worker raised, which would otherwise be lost
            future.result()
        return results

    def _lost(self, reason: str) -> None:
        logger.warning("%s: a row returned NULL: %s", self.function, reason)

    def _send(self, row: Row) -> dict[str, Any] | None:
        if self.failed.is_set():
            return None
        remaining = self.deadline - time.monotonic()
        if remaining < MIN_SEND_S:
            self._lost("the batch reached Snowflake's 180-second limit before this row was sent")
            return None

        images = []
        if len(row.images) > MAX_IMAGES:
            self._lost(f"the row has {len(row.images)} images; Drex takes at most {MAX_IMAGES}")
            return None
        for number, url in enumerate(row.images, 1):
            try:
                data = _read_staged_file(url)
            except Exception as error:  # noqa: BLE001 - Snowflake's file errors have no common type
                # A stage file URL fails here with Snowflake's opaque "open_stream returned a result with an exception
                # set", so the message says what the URL must be
                raise DrexError(
                    f"{self.function}: could not read image {number}: {error}. Images must be scoped file URLs from "
                    "BUILD_SCOPED_FILE_URL, of files the caller's role can read"
                ) from None
            if _media_type(data) is None:
                self._lost(f"image {number} is not a PNG, JPEG or WebP image")
                return None
            if len(data) > MAX_IMAGE_BYTES:
                self._lost(f"image {number} is over Drex's {MAX_IMAGE_BYTES} bytes per image")
                return None
            images.append(image_input(data))

        model = self.drex.routed(row.model)
        state = row.state
        if images and self.drex.open_router:
            # OpenRouter takes images as data URLs in state content parts, after the state as text
            text = state if isinstance(state, str) else json.dumps(state, separators=(",", ":"), ensure_ascii=False)
            parts = [{"type": "image_url", "image_url": {"url": url}} for url in images]
            state = [{"type": "text", "text": text}, *parts]
            images = []
        body: dict[str, Any] = {"state": state, "model": model, "questions": row.questions}
        if images:
            body["images"] = images
        size = len(to_json(body))
        if size > MAX_BODY_BYTES:
            self._lost(
                f"the request body is {size} bytes, over Drex's limit of {MAX_BODY_BYTES}; shorten the input or resize "
                "the images"
            )
            return None

        # Each attempt ends before the deadline, and so does the last retry
        timeout = min(REQUEST_TIMEOUT_S, remaining)
        try:
            response = self.drex.client.system_one(
                state,
                row.questions,
                model=model,
                images=images or None,
                retry=retry_policy(min(REQUEST_BUDGET_S, remaining - timeout)),
                timeout=timeout,
                response_model=_Body,
            )
        except NaceError as error:
            fatal = fatal_or_none(self.function, error, self.drex.base_url)
            if fatal is not None:
                raise fatal from None
            self._lost(describe(error))
            return None
        return response.root


# ---------------------------------------------------------------------------------------------------------------------
# The functions
# ---------------------------------------------------------------------------------------------------------------------


def _model(function: str, value: Any, default: str) -> str:
    """The `model` argument: NULL or '' is the default."""
    if _is_null(value) or value == "":
        return default
    if not isinstance(value, str):
        raise DrexError(f"{function}: model must be a VARCHAR")
    return value


def _key(value: Any) -> str:
    """A batch value as a dictionary key, so that each distinct constant is checked once per batch."""
    return "null" if _is_null(value) else json.dumps(value, sort_keys=True, default=str)


@dataclass(frozen=True)
class _Function:
    name: str
    #: "noul", "choice" or "score" for a helper, else None for drex_decide and drex_decide_image
    kind: str | None
    images: bool = False
    #: Rows per batch Snowflake is asked for: a batch must finish within 180 seconds
    max_batch_rows: int = 64


FUNCTIONS = {
    "drex_noul": _Function("drex_noul", "noul"),
    "drex_choice": _Function("drex_choice", "choice"),
    "drex_score": _Function("drex_score", "score"),
    "drex_decide": _Function("drex_decide", None),
    "drex_decide_image": _Function("drex_decide_image", None, images=True, max_batch_rows=16),
}


def _helper_batch(spec: _Function, frame: pandas.DataFrame, batch: _Batch) -> list[Any]:
    """drex_noul, drex_choice and drex_score: (input, instructions, criteria|labels|levels, model)."""
    columns = [frame.iloc[:, i].tolist() for i in range(4)]
    checked: dict[tuple[str, str, str], tuple[Question, str]] = {}
    rows: list[Row | None] = []
    questions: list[Question | None] = []
    for state, instructions, criteria, model in zip(*columns, strict=True):
        key = (_key(instructions), _key(criteria), _key(model))
        if key not in checked:
            question = helper_question(spec.name, spec.kind, instructions, criteria)
            name = _model(spec.name, model, DEFAULT_MODEL)
            batch.drex.find_model(spec.name, name)
            checked[key] = (question, name)
        question, name = checked[key]
        if _is_null(state):
            rows.append(None)
            questions.append(None)
        else:
            rows.append(Row(state, questions_wire([question]), name))
            questions.append(question)
    answers = batch.run(rows)
    return [
        None if body is None else answer_value(spec.name, question, body)
        for body, question in zip(answers, questions, strict=True)
    ]


def _images(function: str, value: Any) -> list[str] | None:
    """A scoped file URL or an array of them; None for a row that sends nothing: no images, or a NULL one."""
    urls = value if isinstance(value, list) else [value]
    if not urls or any(_is_null(url) for url in urls):
        return None
    if not all(isinstance(url, str) and url for url in urls):
        raise DrexError(f"{function}: images must be a scoped file URL (BUILD_SCOPED_FILE_URL) or an array of them")
    return urls


def _decide_batch(spec: _Function, frame: pandas.DataFrame, batch: _Batch) -> list[Any]:
    """drex_decide (state, questions, model) and drex_decide_image (state, images, questions, model): Drex's response
    body as it came."""
    count = 4 if spec.images else 3
    columns = [frame.iloc[:, i].tolist() for i in range(count)]
    default = DEFAULT_IMAGE_MODEL if spec.images else DEFAULT_MODEL
    checked: dict[tuple[str, str], tuple[dict[str, Any], str]] = {}
    rows: list[Row | None] = []
    for values in zip(*columns, strict=True):
        state, images, questions, model = values if spec.images else (values[0], None, *values[1:])
        if _is_null(questions):
            rows.append(None)
            continue
        key = (_key(questions), _key(model))
        if key not in checked:
            wire = questions_wire(parse_questions(spec.name, questions))
            name = _model(spec.name, model, default)
            listed = batch.drex.find_model(spec.name, name)
            if spec.images and not listed.reads_images:
                raise DrexError(f"{spec.name}: {name} does not read images; use an image model such as drex-max")
            checked[key] = (wire, name)
        wire, name = checked[key]
        urls = _images(spec.name, images) if spec.images else []
        rows.append(None if _is_null(state) or urls is None else Row(state, wire, name, urls))
    return batch.run(rows)


def run_batch(function: str, frame: pandas.DataFrame, concurrency: int = DEFAULT_CONCURRENCY) -> pandas.Series:
    """One batch of a drex_* function: a DataFrame of its arguments by position, answered one value per row."""
    spec = FUNCTIONS[function]
    started = time.monotonic()
    batch = _Batch(function, get_client(), concurrency, started + BATCH_BUDGET_S)
    values = _helper_batch(spec, frame, batch) if spec.kind is not None else _decide_batch(spec, frame, batch)
    if spec.kind == "noul":
        # Snowflake reads NaN in a FLOAT result as NULL
        return pandas.Series([math.nan if value is None else value for value in values], dtype="float64")
    return pandas.Series(values, dtype="object")


def handler(function: str, *, concurrency: int = DEFAULT_CONCURRENCY) -> Callable[[pandas.DataFrame], pandas.Series]:
    """The vectorized handler of a drex_* function, sending at most `concurrency` requests at once per batch."""
    if function not in FUNCTIONS:
        raise ValueError(f"unknown function {function!r}; one of {', '.join(FUNCTIONS)}")
    if isinstance(concurrency, bool) or not isinstance(concurrency, int) or concurrency < 1:
        raise DrexError(f"{function}: concurrency must be at least 1")

    def answer(frame: pandas.DataFrame) -> pandas.Series:
        return run_batch(function, frame, concurrency)

    answer.__name__ = function
    # The attributes the _snowflake.vectorized decorator sets
    answer._sf_vectorized_input = pandas.DataFrame  # type: ignore[attr-defined]
    answer._sf_max_batch_size = FUNCTIONS[function].max_batch_rows  # type: ignore[attr-defined]
    return answer
