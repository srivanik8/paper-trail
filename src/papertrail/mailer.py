"""Sending the digest.

Resend rather than SMTP: an HTTP call with a bearer token beats Gmail app
passwords, TLS negotiation and a local relay, and the free tier covers a daily
digest to one address many times over.

Nothing here decides *whether* to send. It is given a digest and an address and
it makes one HTTP request, so the interesting questions -- has this already gone
out today, is there anything worth sending -- stay in the pipeline where the
store can answer them.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import httpx

API_URL = "https://api.resend.com/emails"

DEFAULT_TIMEOUT = 15.0

#: Resend's sandbox sender, which works before a domain is verified.
DEFAULT_SENDER = "paper-trail <onboarding@resend.dev>"


class MailerNotConfigured(RuntimeError):
    """Raised when a send is attempted without the credentials to do it."""


@dataclass(frozen=True, slots=True)
class Delivery:
    """The outcome of one send attempt."""

    sent: bool
    message_id: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True if the provider accepted the message."""
        return self.sent and self.error is None


class Mailer:
    """Sends a digest through Resend.

    Args:
        api_key: Resend key. Defaults to ``RESEND_API_KEY`` in the environment.
        sender: ``From`` header. Defaults to ``PAPERTRAIL_FROM`` or Resend's
            sandbox sender.
        recipient: Where the digest goes. Defaults to ``PAPERTRAIL_TO``.
        client: Reusable HTTP client. One is created per send if omitted.
        timeout: Per-request timeout in seconds.
    """

    def __init__(
        self,
        api_key: str | None = None,
        sender: str | None = None,
        recipient: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.environ.get("RESEND_API_KEY")
        self.sender = sender or os.environ.get("PAPERTRAIL_FROM") or DEFAULT_SENDER
        self.recipient = recipient or os.environ.get("PAPERTRAIL_TO")
        self._client = client
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        """True if there is both a key and somewhere to send."""
        return bool(self._api_key and self.recipient)

    def missing(self) -> list[str]:
        """Name what is missing, so the error can say which variable to set."""
        gaps = []
        if not self._api_key:
            gaps.append("RESEND_API_KEY")
        if not self.recipient:
            gaps.append("PAPERTRAIL_TO")
        return gaps

    def send(self, digest) -> Delivery:
        """Send ``digest``. Returns the outcome rather than raising on failure.

        Raises:
            MailerNotConfigured: if there is no key or no recipient. That is a
                setup mistake rather than a runtime failure, so it is loud.
        """
        if not self.configured:
            raise MailerNotConfigured("cannot send: set " + " and ".join(self.missing()))

        payload = {
            "from": self.sender,
            "to": [self.recipient],
            "subject": digest.subject,
            "html": digest.html,
            "text": digest.text,
        }

        if self._client is not None:
            return self._post(self._client, payload)
        with httpx.Client(timeout=self._timeout) as client:
            return self._post(client, payload)

    def _post(self, client: httpx.Client, payload: dict) -> Delivery:
        """Make the request, turning every failure into a :class:`Delivery`."""
        try:
            response = client.post(
                API_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                content=json.dumps(payload),
            )
        except httpx.HTTPError as exc:
            return Delivery(sent=False, error=f"{type(exc).__name__}: {exc}")

        if response.status_code >= 400:
            # Resend puts the reason in the body; the status alone is not useful.
            detail = response.text[:200]
            return Delivery(sent=False, error=f"HTTP {response.status_code}: {detail}")

        try:
            message_id = response.json().get("id")
        except ValueError:
            message_id = None

        return Delivery(sent=True, message_id=message_id)
