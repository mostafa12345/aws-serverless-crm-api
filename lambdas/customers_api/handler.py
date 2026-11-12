"""AWS Lambda handler for the Serverless Customer Records REST API."""

from decimal import Decimal
import json
import os
import re
import sys
import time
from typing import Any, Dict, Optional, Tuple
import uuid

# Ensure shared package resolves cleanly in both Lambda and local execution
_LAMBDA_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.store import ConflictError, get_store
from shared.xray_tracer import log_structured, resolve_trace_id, trace_subsegment

VALID_TIERS = {"starter", "growth", "enterprise"}
VALID_STATUSES = {"active", "inactive", "pending"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class DecimalEncoder(json.JSONEncoder):
    """Serialize DynamoDB Decimal numbers safely to JSON."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def _response(
    status_code: int,
    body: Dict[str, Any],
    trace_id: str,
    cache_control: str = "no-store",
) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": os.environ.get("CORS_ORIGIN", "*"),
            "Access-Control-Allow-Headers": "Content-Type,Authorization,X-Amz-Date,X-Api-Key,X-Amzn-Trace-Id",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
            "Cache-Control": cache_control,
            "X-Amzn-Trace-Id": trace_id,
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def _parse_json_body(event: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw = event.get("body")
    if not raw:
        return None, "Request body is required"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Request body must be valid JSON"
    if not isinstance(parsed, dict):
        return None, "Request body must be a JSON object"
    return parsed, None


def _extract_claims(event: Dict[str, Any]) -> Dict[str, Any]:
    return (
        event.get("requestContext", {})
        .get("authorizer", {})
        .get("claims", {})
    )


def _validate_create(payload: Dict[str, Any]) -> Optional[str]:
    for field in ("name", "email", "company"):
        if not str(payload.get(field, "")).strip():
            return f"Field '{field}' is required"
    email = str(payload["email"]).strip().lower()
    if not EMAIL_RE.match(email):
        return "Invalid email address format"
    tier = str(payload.get("tier", "starter")).lower()
    if tier not in VALID_TIERS:
        return f"Invalid tier '{tier}'. Allowed: {sorted(VALID_TIERS)}"
    status = str(payload.get("status", "active")).lower()
    if status not in VALID_STATUSES:
        return f"Invalid status '{status}'. Allowed: {sorted(VALID_STATUSES)}"
    return None


def _handle_health(trace_id: str) -> Dict[str, Any]:
    return _response(
        200,
        {
            "status": "healthy",
            "service": "customers-serverless-api",
            "environment": os.environ.get("ENVIRONMENT", "production"),
            "runtime": f"python{sys.version_info.major}.{sys.version_info.minor}",
            "traceId": trace_id,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        trace_id,
        cache_control="no-cache",
    )


def _handle_list_customers(event: Dict[str, Any], store: Any, trace_id: str) -> Dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    tier = qs.get("tier")
    status = qs.get("status")
    next_token = qs.get("nextToken")
    try:
        limit = max(1, min(100, int(qs.get("limit", 20))))
    except ValueError:
        return _response(400, {"error": "Invalid limit parameter"}, trace_id)

    with trace_subsegment("DynamoDB.ListCustomers", {"tier": tier or "all"}):
        result = store.list_customers(
            tier=tier.lower() if tier else None,
            status=status.lower() if status else None,
            limit=limit,
            next_token=next_token,
        )
    return _response(
        200,
        {"customers": result["items"], "count": len(result["items"]), "nextToken": result.get("nextToken")},
        trace_id,
        cache_control="no-store",
    )


def _handle_create_customer(event: Dict[str, Any], store: Any, trace_id: str) -> Dict[str, Any]:
    payload, err = _parse_json_body(event)
    if err or payload is None:
        return _response(400, {"error": err}, trace_id)

    validation_error = _validate_create(payload)
    if validation_error:
        return _response(400, {"error": validation_error}, trace_id)

    claims = _extract_claims(event)
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    customer = {
        "customerId": str(uuid.uuid4()),
        "name": str(payload["name"]).strip(),
        "email": str(payload["email"]).strip().lower(),
        "company": str(payload["company"]).strip(),
        "tier": str(payload.get("tier", "starter")).lower(),
        "status": str(payload.get("status", "active")).lower(),
        "phone": str(payload.get("phone", "")).strip(),
        "ownerId": claims.get("sub", claims.get("email", "cognito-user")),
        "createdAt": now,
        "updatedAt": now,
    }

    try:
        with trace_subsegment("DynamoDB.CreateCustomer", {"customerId": customer["customerId"]}):
            created = store.create_customer(customer)
    except ConflictError as exc:
        return _response(409, {"error": str(exc)}, trace_id)

    log_structured("INFO", "customers-api", "Customer created", trace_id, customerId=created["customerId"])
    return _response(201, {"customer": created}, trace_id)


def _handle_get_customer(customer_id: str, store: Any, trace_id: str) -> Dict[str, Any]:
    with trace_subsegment("DynamoDB.GetCustomer", {"customerId": customer_id}):
        customer = store.get_customer(customer_id)
    if not customer:
        return _response(404, {"error": f"Customer '{customer_id}' not found"}, trace_id)
    return _response(
        200,
        {"customer": customer},
        trace_id,
        cache_control="public, max-age=60",
    )


def _handle_update_customer(
    customer_id: str, event: Dict[str, Any], store: Any, trace_id: str
) -> Dict[str, Any]:
    payload, err = _parse_json_body(event)
    if err or payload is None:
        return _response(400, {"error": err}, trace_id)

    updates: Dict[str, Any] = {}
    for field in ("name", "company", "phone"):
        if field in payload:
            updates[field] = str(payload[field]).strip()
    if "tier" in payload:
        tier = str(payload["tier"]).lower()
        if tier not in VALID_TIERS:
            return _response(400, {"error": f"Invalid tier '{tier}'"}, trace_id)
        updates["tier"] = tier
    if "status" in payload:
        status = str(payload["status"]).lower()
        if status not in VALID_STATUSES:
            return _response(400, {"error": f"Invalid status '{status}'"}, trace_id)
        updates["status"] = status

    if not updates:
        return _response(400, {"error": "No valid updatable fields provided"}, trace_id)

    updates["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with trace_subsegment("DynamoDB.UpdateCustomer", {"customerId": customer_id}):
        updated = store.update_customer(customer_id, updates)
    if not updated:
        return _response(404, {"error": f"Customer '{customer_id}' not found"}, trace_id)

    log_structured("INFO", "customers-api", "Customer updated", trace_id, customerId=customer_id)
    return _response(200, {"customer": updated}, trace_id)


def _handle_delete_customer(customer_id: str, store: Any, trace_id: str) -> Dict[str, Any]:
    with trace_subsegment("DynamoDB.DeleteCustomer", {"customerId": customer_id}):
        deleted = store.delete_customer(customer_id)
    if not deleted:
        return _response(404, {"error": f"Customer '{customer_id}' not found"}, trace_id)

    log_structured("INFO", "customers-api", "Customer deleted", trace_id, customerId=customer_id)
    return _response(200, {"deleted": True, "customerId": customer_id}, trace_id)


def _handle_list_audit(event: Dict[str, Any], store: Any, trace_id: str) -> Dict[str, Any]:
    qs = event.get("queryStringParameters") or {}
    customer_id = qs.get("customerId")
    try:
        limit = max(1, min(100, int(qs.get("limit", 25))))
    except ValueError:
        return _response(400, {"error": "Invalid limit parameter"}, trace_id)

    events = store.list_audit_events(customer_id=customer_id, limit=limit)
    return _response(200, {"auditEvents": events, "count": len(events)}, trace_id)


def lambda_handler(event: Dict[str, Any], context: Any = None) -> Dict[str, Any]:
    """Primary API Gateway Lambda Proxy entrypoint."""
    trace_id = resolve_trace_id(event.get("headers"))
    method = event.get("httpMethod", "GET").upper()
    path = (event.get("path") or "/").rstrip("/") or "/"

    if method == "OPTIONS":
        return _response(200, {"ok": True}, trace_id)

    store = get_store()

    try:
        if path == "/api/health" and method == "GET":
            return _handle_health(trace_id)

        if path == "/api/audit" and method == "GET":
            return _handle_list_audit(event, store, trace_id)

        if path == "/api/customers":
            if method == "GET":
                return _handle_list_customers(event, store, trace_id)
            if method == "POST":
                return _handle_create_customer(event, store, trace_id)

        if path.startswith("/api/customers/"):
            customer_id = (event.get("pathParameters") or {}).get("id") or path.split("/")[-1]
            if method == "GET":
                return _handle_get_customer(customer_id, store, trace_id)
            if method == "PUT":
                return _handle_update_customer(customer_id, event, store, trace_id)
            if method == "DELETE":
                return _handle_delete_customer(customer_id, store, trace_id)

        return _response(404, {"error": f"Route not found: {method} {path}"}, trace_id)
    except Exception as exc:  # noqa: BLE001 - safe top-level API boundary
        log_structured("ERROR", "customers-api", "Unhandled exception", trace_id, error=str(exc))
        return _response(500, {"error": "Internal server error", "traceId": trace_id}, trace_id)

