"""AWS X-Ray trace correlation and structured CloudWatch logger."""

from contextlib import contextmanager
import json
import os
import secrets
import time
from typing import Any, Dict, Iterator, Optional

try:
    from aws_xray_sdk.core import patch_all, xray_recorder

    patch_all()
    HAS_XRAY_SDK = True
except ImportError:
    HAS_XRAY_SDK = False
    xray_recorder = None


def generate_trace_id() -> str:
    """Create an AWS X-Ray compliant Root trace header."""
    epoch_hex = format(int(time.time()), "x")
    unique_id = secrets.token_hex(12)
    return f"Root=1-{epoch_hex}-{unique_id}"


def resolve_trace_id(headers: Optional[Dict[str, str]] = None) -> str:
    """Extract X-Amzn-Trace-Id from headers or environment, or mint a new one."""
    if headers:
        for key, value in headers.items():
            if key.lower() == "x-amzn-trace-id" and value:
                return value
    env_trace = os.environ.get("_X_AMZN_TRACE_ID")
    if env_trace:
        return env_trace
    return generate_trace_id()


def log_structured(
    level: str,
    service: str,
    message: str,
    trace_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """Emit a single-line JSON log record compatible with CloudWatch Logs Insights."""
    payload: Dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "level": level.upper(),
        "service": service,
        "message": message,
        "traceId": trace_id or os.environ.get("_X_AMZN_TRACE_ID", "local"),
    }
    if fields:
        payload["details"] = fields
    print(json.dumps(payload, default=str))


@contextmanager
def trace_subsegment(
    name: str,
    annotations: Optional[Dict[str, Any]] = None,
) -> Iterator[None]:
    """Wrap an operation in an X-Ray subsegment when aws_xray_sdk is active."""
    if HAS_XRAY_SDK and xray_recorder is not None:
        subsegment = xray_recorder.begin_subsegment(name)
        try:
            if annotations and subsegment is not None:
                for key, val in annotations.items():
                    subsegment.put_annotation(key, val)
            yield
        finally:
            xray_recorder.end_subsegment()
    else:
        yield
