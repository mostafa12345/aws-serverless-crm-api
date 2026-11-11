"""Amazon Cognito Post-Confirmation trigger for onboarding new CRM users."""

import os
import sys
import time
from typing import Any, Dict

_LAMBDA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.store import get_store
from shared.xray_tracer import log_structured, resolve_trace_id

NINETY_DAYS_SECONDS = 90 * 24 * 60 * 60


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Record user verification event after Cognito account confirmation."""
    trace_id = resolve_trace_id()
    user_attrs = event.get("request", {}).get("userAttributes", {})
    sub = user_attrs.get("sub", event.get("userName", "unknown"))
    email = user_attrs.get("email", "unknown")

    now_ts = int(time.time())
    audit_entry = {
        "eventId": f"cognito-confirm-{sub}-{now_ts}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
        "customerId": f"user:{sub}",
        "eventName": "USER_CONFIRMED",
        "summary": f"Cognito user confirmed: {email}",
        "changedFields": [],
        "expiresAt": now_ts + NINETY_DAYS_SECONDS,
    }

    try:
        get_store().write_audit_event(audit_entry)
    except Exception as exc:  # noqa: BLE001 - never block Cognito sign-up on audit error
        log_structured("WARN", "cognito-trigger", "Audit write skipped", trace_id, error=str(exc))

    log_structured("INFO", "cognito-trigger", "User confirmed", trace_id, email=email, sub=sub)
    return event
