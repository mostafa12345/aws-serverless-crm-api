"""DynamoDB Streams Lambda processor writing audit logs and dispatching SNS alerts."""

import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import boto3

_LAMBDA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.store import get_store
from shared.xray_tracer import log_structured, resolve_trace_id, trace_subsegment

NINETY_DAYS_SECONDS = 90 * 24 * 60 * 60


def _decode_image(raw_image: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Decode DynamoDB Stream AttributeValue map into native Python types."""
    if not raw_image:
        return {}
    decoded: Dict[str, Any] = {}
    for key, typed_val in raw_image.items():
        if not isinstance(typed_val, dict):
            decoded[key] = typed_val
            continue
        if "S" in typed_val:
            decoded[key] = typed_val["S"]
        elif "N" in typed_val:
            num_str = typed_val["N"]
            decoded[key] = float(num_str) if "." in num_str else int(num_str)
        elif "BOOL" in typed_val:
            decoded[key] = bool(typed_val["BOOL"])
    return decoded


def _compute_diff(old_img: Dict[str, Any], new_img: Dict[str, Any]) -> List[str]:
    ignored = {"updatedAt", "createdAt"}
    changed = []
    for key in sorted(set(old_img.keys()) | set(new_img.keys())):
        if key in ignored:
            continue
        if old_img.get(key) != new_img.get(key):
            changed.append(key)
    return changed


def _publish_enterprise_alert(
    event_name: str, customer_id: str, record: Dict[str, Any], trace_id: str
) -> None:
    topic_arn = os.environ.get("ALERTS_TOPIC_ARN", "")
    if not topic_arn or os.environ.get("LOCAL_MODE", "").lower() == "true":
        return
    try:
        sns = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        sns.publish(
            TopicArn=topic_arn,
            Subject=f"[CRM Audit] Enterprise Customer {event_name}",
            Message=json.dumps(
                {
                    "event": event_name,
                    "customerId": customer_id,
                    "company": record.get("company"),
                    "email": record.get("email"),
                    "traceId": trace_id,
                }
            ),
        )
    except Exception as exc:  # noqa: BLE001 - non-fatal alert failure
        log_structured("WARN", "stream-processor", "SNS publish skipped", trace_id, error=str(exc))


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Process DynamoDB Stream batches (INSERT, MODIFY, REMOVE)."""
    trace_id = resolve_trace_id()
    store = get_store()
    processed = 0

    for record in event.get("Records", []):
        event_name = record.get("eventName", "UNKNOWN")
        ddb = record.get("dynamodb", {})
        keys = _decode_image(ddb.get("Keys"))
        new_img = _decode_image(ddb.get("NewImage"))
        old_img = _decode_image(ddb.get("OldImage"))

        customer_id = keys.get("customerId") or new_img.get("customerId") or old_img.get("customerId", "unknown")
        active = new_img or old_img
        changed_fields = _compute_diff(old_img, new_img) if event_name == "MODIFY" else []

        summary = (
            f"{event_name} customer '{active.get('name', customer_id)}' "
            f"({active.get('company', 'N/A')}) [{active.get('tier', 'starter')}]"
        )
        if changed_fields:
            summary += f" - updated: {', '.join(changed_fields)}"

        now_ts = int(time.time())
        audit_entry = {
            "eventId": record.get("eventID", f"evt-{now_ts}-{processed}"),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now_ts)),
            "customerId": customer_id,
            "eventName": event_name,
            "summary": summary,
            "changedFields": changed_fields,
            "expiresAt": now_ts + NINETY_DAYS_SECONDS,
        }

        with trace_subsegment("AuditTable.WriteEvent", {"eventName": event_name}):
            store.write_audit_event(audit_entry)

        if active.get("tier") == "enterprise":
            _publish_enterprise_alert(event_name, customer_id, active, trace_id)

        log_structured("INFO", "stream-processor", summary, trace_id, eventId=audit_entry["eventId"])
        processed += 1

    return {"status": "ok", "processedRecords": processed}
