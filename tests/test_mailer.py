import json

import httpx
import pytest

from papertrail.digest import Digest
from papertrail.mailer import API_URL, DEFAULT_SENDER, Mailer, MailerNotConfigured

DIGEST = Digest(
    subject="paper-trail 01 Jun: A result",
    html="<!DOCTYPE html><html><body>hi</body></html>",
    text="paper-trail\n\n[8] A result",
    stories=[],
)


def client_recording(
    requests: list[httpx.Request], status: int = 200, body: dict | str | None = None
) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body if body is not None else {"id": "msg_123"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_a_digest_is_posted_to_resend():
    seen: list[httpx.Request] = []
    mailer = Mailer(api_key="re_test", recipient="me@example.com", client=client_recording(seen))

    result = mailer.send(DIGEST)

    assert result.ok is True
    assert result.message_id == "msg_123"
    (request,) = seen
    assert str(request.url) == API_URL


def test_the_request_carries_subject_html_and_text():
    seen: list[httpx.Request] = []
    Mailer(api_key="re_test", recipient="me@example.com", client=client_recording(seen)).send(
        DIGEST
    )

    payload = json.loads(seen[0].content)
    assert payload["subject"] == DIGEST.subject
    assert payload["html"] == DIGEST.html
    assert payload["text"] == DIGEST.text
    assert payload["to"] == ["me@example.com"]


def test_the_key_is_sent_as_a_bearer_token():
    seen: list[httpx.Request] = []
    Mailer(api_key="re_secret", recipient="me@example.com", client=client_recording(seen)).send(
        DIGEST
    )
    assert seen[0].headers["authorization"] == "Bearer re_secret"


def test_the_sender_defaults_to_the_sandbox_address():
    seen: list[httpx.Request] = []
    Mailer(api_key="re_test", recipient="me@example.com", client=client_recording(seen)).send(
        DIGEST
    )
    assert json.loads(seen[0].content)["from"] == DEFAULT_SENDER


def test_the_sender_can_be_set():
    seen: list[httpx.Request] = []
    Mailer(
        api_key="re_test",
        sender="digest@mine.dev",
        recipient="me@example.com",
        client=client_recording(seen),
    ).send(DIGEST)
    assert json.loads(seen[0].content)["from"] == "digest@mine.dev"


# --- configuration ----------------------------------------------------------


def test_sending_without_a_key_is_a_setup_error_not_a_silent_failure():
    with pytest.raises(MailerNotConfigured, match="RESEND_API_KEY"):
        Mailer(api_key="", recipient="me@example.com").send(DIGEST)


def test_sending_without_a_recipient_names_the_missing_variable():
    with pytest.raises(MailerNotConfigured, match="PAPERTRAIL_TO"):
        Mailer(api_key="re_test", recipient="").send(DIGEST)


def test_both_missing_are_named_together():
    with pytest.raises(MailerNotConfigured, match="RESEND_API_KEY and PAPERTRAIL_TO"):
        Mailer(api_key="", recipient="").send(DIGEST)


def test_configuration_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_env")
    monkeypatch.setenv("PAPERTRAIL_TO", "env@example.com")
    monkeypatch.setenv("PAPERTRAIL_FROM", "from@example.com")

    mailer = Mailer()
    assert mailer.configured is True
    assert mailer.recipient == "env@example.com"
    assert mailer.sender == "from@example.com"


def test_explicit_arguments_beat_the_environment(monkeypatch):
    monkeypatch.setenv("PAPERTRAIL_TO", "env@example.com")
    assert Mailer(api_key="k", recipient="explicit@example.com").recipient == "explicit@example.com"


def test_missing_reports_nothing_when_configured():
    assert Mailer(api_key="k", recipient="me@example.com").missing() == []


# --- failure ----------------------------------------------------------------


def test_a_rejected_send_reports_the_providers_reason():
    seen: list[httpx.Request] = []
    mailer = Mailer(
        api_key="re_test",
        recipient="me@example.com",
        client=client_recording(seen, status=422, body={"message": "domain not verified"}),
    )

    result = mailer.send(DIGEST)
    assert result.ok is False
    assert "422" in result.error
    assert "domain not verified" in result.error


def test_a_network_failure_is_reported_not_raised():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Mailer(api_key="k", recipient="me@example.com", client=client).send(DIGEST)

    assert result.ok is False
    assert "ConnectTimeout" in result.error


def test_a_success_without_a_parseable_body_still_counts_as_sent():
    seen: list[httpx.Request] = []
    mailer = Mailer(
        api_key="k",
        recipient="me@example.com",
        client=client_recording(seen, status=200, body="accepted"),
    )
    result = mailer.send(DIGEST)

    assert result.ok is True
    assert result.message_id is None


def test_the_key_never_appears_in_an_error_message():
    seen: list[httpx.Request] = []
    mailer = Mailer(
        api_key="re_supersecret",
        recipient="me@example.com",
        client=client_recording(seen, status=401, body={"message": "invalid"}),
    )
    assert "re_supersecret" not in (mailer.send(DIGEST).error or "")
