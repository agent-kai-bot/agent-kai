"""HMAC validation for the taskboard webhook ingress.

This module owns the security-critical pieces of the
``POST /api/webhooks/taskboard`` route: header parsing, timestamp skew
checks, signature verification, and the small set of typed errors the
route handler maps to HTTP responses.

The signed string format is::

    <unix_timestamp>.<delivery_id>.<raw_body_bytes>

The signature header carries the hex digest of HMAC-SHA256 over that
string, prefixed with ``sha256=``.

Example:
    Validate a request inside a FastAPI route::

        from daemon.webhook_auth import (
            DEFAULT_TIMESTAMP_SKEW_SECONDS,
            HEADER_DELIVERY,
            HEADER_EVENT,
            HEADER_SIGNATURE,
            HEADER_TIMESTAMP,
            verify_signature,
        )

        verify_signature(
            secret=secret_bytes,
            body=raw_body,
            timestamp_header=request.headers.get(HEADER_TIMESTAMP),
            delivery_header=request.headers.get(HEADER_DELIVERY),
            signature_header=request.headers.get(HEADER_SIGNATURE),
        )
"""

from __future__ import annotations

import hmac
import time
import uuid
from dataclasses import dataclass
from hashlib import sha256

HEADER_EVENT = "x-taskboard-event"
HEADER_DELIVERY = "x-taskboard-delivery"
HEADER_TIMESTAMP = "x-taskboard-timestamp"
HEADER_SIGNATURE = "x-taskboard-signature"

SIGNATURE_PREFIX = "sha256="
DEFAULT_TIMESTAMP_SKEW_SECONDS = 300


class WebhookAuthError(Exception):
    """Base class for webhook authentication failures."""


class WebhookHeaderError(WebhookAuthError):
    """Raised when a required header is missing or malformed."""


class WebhookSignatureError(WebhookAuthError):
    """Raised when the supplied HMAC signature does not match the body."""


class WebhookTimestampError(WebhookAuthError):
    """Raised when the request timestamp is outside the allowed skew."""


@dataclass(frozen=True)
class VerifiedHeaders:
    """Parsed and validated webhook headers.

    Attributes:
        event_type: Value of the ``X-Taskboard-Event`` header.
        delivery_id: Value of the ``X-Taskboard-Delivery`` header,
            confirmed to be a well-formed UUID.
        timestamp: Unix-second integer parsed from the
            ``X-Taskboard-Timestamp`` header.
        signature_hex: Hex digest portion of the
            ``X-Taskboard-Signature`` header, lower-cased.
    """

    event_type: str
    delivery_id: str
    timestamp: int
    signature_hex: str


def _require(value: str | None, header_name: str) -> str:
    if value is None or not value.strip():
        raise WebhookHeaderError(f"missing required header: {header_name}")
    return value.strip()


def _parse_timestamp(raw: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise WebhookHeaderError(
            "X-Taskboard-Timestamp must be an integer Unix-second value"
        ) from exc


def _parse_delivery_id(raw: str) -> str:
    try:
        parsed = uuid.UUID(raw)
    except (TypeError, ValueError) as exc:
        raise WebhookHeaderError(
            "X-Taskboard-Delivery must be a UUID"
        ) from exc
    return str(parsed)


def _parse_signature(raw: str) -> str:
    if not raw.lower().startswith(SIGNATURE_PREFIX):
        raise WebhookHeaderError(
            "X-Taskboard-Signature must use 'sha256=<hex>' format"
        )
    digest = raw[len(SIGNATURE_PREFIX):].strip().lower()
    if not digest or any(c not in "0123456789abcdef" for c in digest):
        raise WebhookHeaderError(
            "X-Taskboard-Signature digest must be hexadecimal"
        )
    return digest


def parse_headers(
    *,
    event_header: str | None,
    delivery_header: str | None,
    timestamp_header: str | None,
    signature_header: str | None,
) -> VerifiedHeaders:
    """Parse and shallow-validate the four taskboard webhook headers.

    Args:
        event_header: Value of the ``X-Taskboard-Event`` header.
        delivery_header: Value of the ``X-Taskboard-Delivery`` header.
        timestamp_header: Value of the ``X-Taskboard-Timestamp`` header.
        signature_header: Value of the ``X-Taskboard-Signature`` header.

    Returns:
        A :class:`VerifiedHeaders` instance with normalized values. The
        signature has not yet been verified against the request body.

    Raises:
        WebhookHeaderError: If any header is missing or malformed.
    """

    event = _require(event_header, "X-Taskboard-Event")
    delivery = _parse_delivery_id(_require(delivery_header, "X-Taskboard-Delivery"))
    timestamp = _parse_timestamp(_require(timestamp_header, "X-Taskboard-Timestamp"))
    signature = _parse_signature(_require(signature_header, "X-Taskboard-Signature"))
    return VerifiedHeaders(
        event_type=event,
        delivery_id=delivery,
        timestamp=timestamp,
        signature_hex=signature,
    )


def signed_string(timestamp: int, delivery_id: str, body: bytes) -> bytes:
    """Build the canonical string the sender signs and the receiver verifies.

    The format is ``<timestamp>.<delivery_id>.<body>`` encoded as bytes.
    Producing the same string on both sides is the only way the HMAC
    signatures will compare equal.

    Args:
        timestamp: Unix-second integer from the timestamp header.
        delivery_id: UUID string from the delivery header.
        body: Raw request body bytes (not the parsed JSON).

    Returns:
        Bytes ready to be passed to :func:`hmac.new` or
        :func:`compute_signature`.
    """

    prefix = f"{timestamp}.{delivery_id}.".encode("utf-8")
    return prefix + body


def compute_signature(secret: bytes, timestamp: int, delivery_id: str, body: bytes) -> str:
    """Compute the lower-case hex HMAC-SHA256 of the signed string.

    Args:
        secret: Shared HMAC secret bytes.
        timestamp: Unix-second integer from the timestamp header.
        delivery_id: UUID string from the delivery header.
        body: Raw request body bytes.

    Returns:
        Lower-case hexadecimal HMAC-SHA256 digest.
    """

    msg = signed_string(timestamp, delivery_id, body)
    return hmac.new(secret, msg, sha256).hexdigest()


def check_timestamp_skew(
    timestamp: int,
    *,
    now: int | None = None,
    max_skew_seconds: int = DEFAULT_TIMESTAMP_SKEW_SECONDS,
) -> None:
    """Reject timestamps that are too old or too far in the future.

    Args:
        timestamp: Unix-second integer from the timestamp header.
        now: Reference Unix-second timestamp. Defaults to the current
            wall-clock time. Tests inject a fixed value for repeatable
            assertions.
        max_skew_seconds: Maximum allowed absolute difference, defaults
            to :data:`DEFAULT_TIMESTAMP_SKEW_SECONDS` (5 minutes).

    Raises:
        WebhookTimestampError: When the timestamp differs from ``now``
            by more than ``max_skew_seconds``.
    """

    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > max_skew_seconds:
        raise WebhookTimestampError(
            "X-Taskboard-Timestamp is outside the allowed skew window"
        )


def verify_signature(
    *,
    secret: bytes,
    body: bytes,
    headers: VerifiedHeaders,
    now: int | None = None,
    max_skew_seconds: int = DEFAULT_TIMESTAMP_SKEW_SECONDS,
) -> None:
    """Verify timestamp skew and HMAC-SHA256 over the request body.

    The function performs the timestamp check before the signature check
    so that a clock-skew failure is reported even if the signature would
    also have failed. Both errors are surfaced as 401 by the HTTP route.

    Args:
        secret: Shared HMAC secret bytes obtained from the secret
            provider.
        body: Raw request body bytes (not the parsed JSON).
        headers: Parsed webhook headers from :func:`parse_headers`.
        now: Optional reference timestamp for tests.
        max_skew_seconds: Maximum allowed absolute clock difference.

    Raises:
        WebhookTimestampError: When the timestamp is outside the skew
            window.
        WebhookSignatureError: When the supplied signature does not
            match the recomputed HMAC.
    """

    check_timestamp_skew(
        headers.timestamp,
        now=now,
        max_skew_seconds=max_skew_seconds,
    )
    expected = compute_signature(
        secret,
        headers.timestamp,
        headers.delivery_id,
        body,
    )
    if not hmac.compare_digest(expected, headers.signature_hex):
        raise WebhookSignatureError("invalid taskboard webhook signature")
