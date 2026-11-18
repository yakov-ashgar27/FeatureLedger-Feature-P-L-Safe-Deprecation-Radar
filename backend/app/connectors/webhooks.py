"""Webhook signature verification (constant-time). A webhook is only a fast path: every source is also
polled on schedule, so a dropped webhook costs latency, never data."""
from __future__ import annotations

import base64
import hashlib
import hmac
import time


class BadSignature(ValueError):
    pass


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def verify_stripe(secret: str, header: str | None, body: bytes, tolerance: int = 300, now: float | None = None) -> None:
    """Stripe-Signature: t=<ts>,v1=<hex hmac of "<ts>.<body>">"""
    if not header:
        raise BadSignature("missing Stripe-Signature")
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts, sigs = parts.get("t"), [v for k, v in (p.split("=", 1) for p in header.split(",") if "=" in p) if k == "v1"]
    if not ts or not sigs:
        raise BadSignature("malformed Stripe-Signature")
    if abs((now or time.time()) - int(ts)) > tolerance:
        raise BadSignature("timestamp outside tolerance")
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    if not any(_eq(expected, s) for s in sigs):
        raise BadSignature("signature mismatch")


def verify_zendesk(secret: str, signature: str | None, timestamp: str | None, body: bytes) -> None:
    """X-Zendesk-Webhook-Signature = base64(hmac_sha256(secret, timestamp + body))."""
    if not signature or not timestamp:
        raise BadSignature("missing Zendesk signature headers")
    expected = base64.b64encode(hmac.new(secret.encode(), timestamp.encode() + body, hashlib.sha256).digest()).decode()
    if not _eq(expected, signature):
        raise BadSignature("signature mismatch")


def verify_hub_signature(secret: str, header: str | None, body: bytes) -> None:
    """Jira Cloud (and GitHub) style: X-Hub-Signature: sha256=<hex hmac of body>."""
    if not header or not header.startswith("sha256="):
        raise BadSignature("missing X-Hub-Signature")
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    if not _eq(expected, header):
        raise BadSignature("signature mismatch")


def verify_shared_secret(secret: str, supplied: str | None) -> None:
    if not supplied or not _eq(secret, supplied):
        raise BadSignature("bad shared secret")
