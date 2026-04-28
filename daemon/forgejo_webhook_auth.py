"""HMAC validation helpers for Forgejo webhook ingress.

Forgejo sends the raw request body signed with HMAC-SHA256. This module
parses the required headers, normalizes the delivery UUID, and compares
the supplied digest using constant-time comparison.

Example:
    Verify a request body inside a route handler::

        headers = parse_headers(
            event_header=request.headers.get("X-Forgejo-Event"),
            gitea_event_header=request.headers.get("X-Gitea-Event"),
            delivery_header=request.headers.get("X-Forgejo-Delivery"),
            signature_header=request.headers.get("X-Forgejo-Signature"),
        )
        verify_signature(secret=secret, body=body_bytes, headers=headers)
"""

from __future__ import annotations

import hmac
import uuid
from dataclasses import dataclass
from hashlib import sha256

HEADER_EVENT = "X-Forgejo-Event"
HEADER_GITEA_EVENT = "X-Gitea-Event"
HEADER_DELIVERY = "X-Forgejo-Delivery"
HEADER_SIGNATURE = "X-Forgejo-Signature"

SIGNATURE_PREFIX = "sha256="
SUPPORTED_EVENTS = frozenset({"pull_request"})


class ForgejoWebhookAuthError(Exception):
    """Base class for Forgejo webhook authentication failures.

    Example:
        Catch all Forgejo webhook validation errors::

            try:
                verify_signature(secret=secret, body=body, headers=headers)
            except ForgejoWebhookAuthError:
                raise
    """


class WebhookHeaderError(ForgejoWebhookAuthError):
    """Raised when a required Forgejo webhook header is missing or invalid.

    Example:
        Surface malformed headers as a 422 response in the route layer::

            raise WebhookHeaderError("missing required header")
    """


class WebhookSignatureError(ForgejoWebhookAuthError):
    """Raised when the Forgejo webhook HMAC digest does not match.

    Example:
        Surface HMAC failures as a 401 response in the route layer::

            raise WebhookSignatureError("invalid signature")
    """


@dataclass(frozen=True)
class ForgejoWebhookHeaders:
    """Parsed Forgejo webhook headers.

    Attributes:
        event_type: Normalized event type, such as ``pull_request``.
        delivery_id: Canonical UUID string from ``X-Forgejo-Delivery``.
        signature_hex: Lower-case hex digest from
            ``X-Forgejo-Signature``.

    Example:
        Build a header object through :func:`parse_headers`::

            headers = parse_headers(
                event_header="pull_request",
                gitea_event_header=None,
                delivery_header="00000000-0000-0000-0000-000000000001",
                signature_header="sha256=" + "0" * 64,
            )
    """

    event_type: str
    delivery_id: str
    signature_hex: str


def _require(value: str | None, header_name: str) -> str:
    if value is None or not value.strip():
        raise WebhookHeaderError(f"missing required header: {header_name}")
    return value.strip()


def _parse_event(
    event_header: str | None,
    gitea_event_header: str | None,
) -> str:
    forgejo_value = event_header.strip() if event_header else ""
    gitea_value = gitea_event_header.strip() if gitea_event_header else ""
    if forgejo_value and gitea_value and forgejo_value != gitea_value:
        raise WebhookHeaderError(
            "X-Forgejo-Event and X-Gitea-Event must match when both are present"
        )
    event = forgejo_value or gitea_value
    if not event:
        raise WebhookHeaderError("missing required header: X-Forgejo-Event")
    return event


def _parse_delivery_id(raw: str) -> str:
    try:
        parsed = uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise WebhookHeaderError("X-Forgejo-Delivery must be a UUID") from exc
    return str(parsed)


def _parse_signature(raw: str) -> str:
    if not raw.lower().startswith(SIGNATURE_PREFIX):
        raise WebhookHeaderError(
            "X-Forgejo-Signature must use 'sha256=<hex>' format"
        )
    digest = raw[len(SIGNATURE_PREFIX) :].strip().lower()
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise WebhookHeaderError(
            "X-Forgejo-Signature digest must be a 64-character hex string"
        )
    return digest


def parse_headers(
    *,
    event_header: str | None,
    delivery_header: str | None,
    signature_header: str | None,
    gitea_event_header: str | None = None,
) -> ForgejoWebhookHeaders:
    """Parse and normalize Forgejo webhook headers.

    Args:
        event_header: Value of ``X-Forgejo-Event``.
        delivery_header: Value of ``X-Forgejo-Delivery``.
        signature_header: Value of ``X-Forgejo-Signature``.
        gitea_event_header: Optional compatibility value of
            ``X-Gitea-Event``.

    Returns:
        Parsed headers with canonical UUID and lower-case signature
        digest. The signature has not yet been checked against the body.

    Raises:
        WebhookHeaderError: If a required header is missing or malformed.

    Example:
        Parse headers before verifying the HMAC::

            headers = parse_headers(
                event_header="pull_request",
                delivery_header="00000000-0000-0000-0000-000000000001",
                signature_header="sha256=" + "0" * 64,
            )
    """

    event = _parse_event(event_header, gitea_event_header)
    delivery_id = _parse_delivery_id(_require(delivery_header, HEADER_DELIVERY))
    signature_hex = _parse_signature(_require(signature_header, HEADER_SIGNATURE))
    return ForgejoWebhookHeaders(
        event_type=event,
        delivery_id=delivery_id,
        signature_hex=signature_hex,
    )


def compute_signature(secret: bytes, body: bytes) -> str:
    """Compute the Forgejo HMAC-SHA256 digest for a request body.

    Args:
        secret: Shared webhook secret bytes.
        body: Raw HTTP request body bytes.

    Returns:
        Lower-case hexadecimal SHA-256 HMAC digest.

    Example:
        Sign a JSON payload for a test request::

            digest = compute_signature(b"secret", b'{"action":"opened"}')
    """

    return hmac.new(secret, body, sha256).hexdigest()


def verify_signature(
    *,
    secret: bytes,
    body: bytes,
    headers: ForgejoWebhookHeaders,
) -> None:
    """Verify the Forgejo request body signature.

    Args:
        secret: Shared webhook secret bytes.
        body: Raw HTTP request body bytes.
        headers: Parsed headers from :func:`parse_headers`.

    Raises:
        WebhookSignatureError: If the supplied digest does not match the
            recomputed HMAC digest.

    Example:
        Verify a parsed request::

            verify_signature(secret=secret, body=body_bytes, headers=headers)
    """

    expected = compute_signature(secret, body)
    if not hmac.compare_digest(expected, headers.signature_hex):
        raise WebhookSignatureError("invalid forgejo webhook signature")
