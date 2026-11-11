"""Local serverless stack simulator (CloudFront, WAFv2, Cognito, API GW Cache, Lambda, DynamoDB Streams)."""

import base64
from collections import defaultdict, deque
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
import re
import secrets
import sys
import threading
import time
from typing import Any, Deque, Dict, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlparse
import uuid

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LAMBDA_ROOT = os.path.join(PROJECT_ROOT, "lambdas")
FRONTEND_ROOT = os.path.join(PROJECT_ROOT, "frontend")

if LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, LAMBDA_ROOT)

os.environ.setdefault("LOCAL_MODE", "true")

from cognito_triggers.post_confirmation import lambda_handler as cognito_confirm_handler
from customers_api.handler import lambda_handler as api_handler
from shared.store import SQLiteDynamoSimulatorStore, reset_local_store
from shared.xray_tracer import resolve_trace_id
from stream_processor.handler import lambda_handler as stream_handler

WAF_SIGNATURES = [
    re.compile(r"<script\b", re.IGNORECASE),
    re.compile(r"\bdrop\s+table\b", re.IGNORECASE),
    re.compile(r"\bunion\s+select\b", re.IGNORECASE),
    re.compile(r"(['\"])\s*or\s+1\s*=\s*1", re.IGNORECASE),
]


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(segment: str) -> bytes:
    padding = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + padding)


class LocalCognitoPool:
    """In-memory Cognito User Pool simulator issuing signed JWT tokens."""

    def __init__(self, jwt_secret: str) -> None:
        self._secret = jwt_secret.encode("utf-8")
        self._users: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _hash_password(self, password: str, salt: bytes) -> str:
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
        return f"{salt.hex()}:{digest.hex()}"

    def _verify_password(self, password: str, stored: str) -> bool:
        salt_hex, digest_hex = stored.split(":", 1)
        candidate = self._hash_password(password, bytes.fromhex(salt_hex)).split(":", 1)[1]
        return hmac.compare_digest(candidate, digest_hex)

    def signup(self, email: str, password: str, name: str) -> Dict[str, Any]:
        email_norm = email.strip().lower()
        if not email_norm or len(password) < 6:
            raise ValueError("Valid email and password (min 6 chars) are required")
        with self._lock:
            if email_norm in self._users:
                raise KeyError("User already exists in Cognito User Pool")
            sub = str(uuid.uuid4())
            self._users[email_norm] = {
                "sub": sub,
                "email": email_norm,
                "name": name.strip() or email_norm.split("@")[0],
                "passwordHash": self._hash_password(password, secrets.token_bytes(16)),
            }

        cognito_confirm_handler(
            {
                "triggerSource": "PostConfirmation_ConfirmSignUp",
                "userName": email_norm,
                "request": {"userAttributes": {"sub": sub, "email": email_norm}},
            }
        )
        return {"userSub": sub, "email": email_norm, "userConfirmed": True}

    def signin(self, email: str, password: str) -> Dict[str, Any]:
        email_norm = email.strip().lower()
        with self._lock:
            user = self._users.get(email_norm)
        if not user or not self._verify_password(password, user["passwordHash"]):
            raise PermissionError("Incorrect username or password")

        now = int(time.time())
        header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode("utf-8"))
        claims = {
            "sub": user["sub"],
            "email": user["email"],
            "name": user["name"],
            "cognito:username": user["email"],
            "token_use": "id",
            "iat": now,
            "exp": now + 3600,
        }
        payload = _b64url(json.dumps(claims).encode("utf-8"))
        sig_input = f"{header}.{payload}".encode("utf-8")
        signature = _b64url(hmac.new(self._secret, sig_input, hashlib.sha256).digest())
        return {
            "idToken": f"{header}.{payload}.{signature}",
            "expiresIn": 3600,
            "tokenType": "Bearer",
            "user": {"sub": user["sub"], "email": user["email"], "name": user["name"]},
        }

    def verify(self, token: str) -> Optional[Dict[str, Any]]:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        signing_input = f"{parts[0]}.{parts[1]}".encode("utf-8")
        expected_sig = _b64url(hmac.new(self._secret, signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(parts[2], expected_sig):
            return None
        try:
            claims = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return None
        if int(claims.get("exp", 0)) < int(time.time()):
            return None
        return claims


class EdgeRuntimeState:
    """Holds shared simulator components for WAF, Cognito, API GW Cache, and Streams."""

    def __init__(self, db_path: str = ":memory:", waf_limit: int = 120) -> None:
        self.store = SQLiteDynamoSimulatorStore(db_path=db_path)
        self.store.register_stream_listener(stream_handler)
        reset_local_store(self.store)

        self.cognito = LocalCognitoPool(
            jwt_secret=os.environ.get("JWT_SECRET", "local-cognito-sim-secret")
        )
        self.waf_limit = waf_limit
        self._rate_buckets: Dict[str, Deque[float]] = defaultdict(deque)
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def check_waf(self, client_ip: str, raw_target: str, body: str) -> Optional[str]:
        now = time.time()
        with self._lock:
            bucket = self._rate_buckets[client_ip]
            while bucket and now - bucket[0] > 60:
                bucket.popleft()
            if len(bucket) >= self.waf_limit:
                return "WAF RateBasedRule triggered (too many requests)"
            bucket.append(now)

        inspect_text = f"{unquote(raw_target)}\n{body}"
        for pattern in WAF_SIGNATURES:
            if pattern.search(inspect_text):
                return "WAF OWASP/SQLi ManagedRuleSet blocked suspicious payload"
        return None

    def cache_get(self, cache_key: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            entry = self._cache.get(cache_key)
            if not entry:
                return None
            expires_at, response = entry
            if time.time() > expires_at:
                self._cache.pop(cache_key, None)
                return None
            return response

    def cache_set(self, cache_key: str, response: Dict[str, Any], ttl: int = 30) -> None:
        with self._lock:
            self._cache[cache_key] = (time.time() + ttl, response)

    def cache_invalidate(self) -> None:
        with self._lock:
            self._cache.clear()


def make_handler(state: EdgeRuntimeState) -> type:
    """Build an HTTP handler bound to the provided EdgeRuntimeState."""

    class ServerlessGatewayHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            if os.environ.get("QUIET_SERVER") != "1":
                super().log_message(format, *args)

        def _send_json(
            self,
            status_code: int,
            payload: Dict[str, Any],
            extra_headers: Optional[Dict[str, str]] = None,
            raw_body: Optional[str] = None,
        ) -> None:
            body_bytes = (
                raw_body.encode("utf-8")
                if raw_body is not None
                else json.dumps(payload).encode("utf-8")
            )
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Amzn-Trace-Id")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,PUT,DELETE,OPTIONS")
            self.send_header("Server", "CloudFront-Simulator")
            if extra_headers:
                for k, v in extra_headers.items():
                    if k.lower() in {"content-type", "content-length"}:
                        continue
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body_bytes)

        def _read_body(self) -> str:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return ""
            return self.rfile.read(length).decode("utf-8")

        def _serve_static(self, path: str) -> None:
            if path == "/config.json":
                self._send_json(
                    200,
                    {
                        "mode": "local",
                        "apiBase": "",
                        "region": os.environ.get("AWS_REGION", "us-east-1"),
                        "wafEnabled": True,
                    },
                    {"X-Cache": "Miss from cloudfront"},
                )
                return

            rel = "index.html" if path in ("", "/") else path.lstrip("/")
            candidate = os.path.realpath(os.path.join(FRONTEND_ROOT, rel))
            if not candidate.startswith(os.path.realpath(FRONTEND_ROOT)) or not os.path.isfile(candidate):
                candidate = os.path.join(FRONTEND_ROOT, "index.html")

            with open(candidate, "rb") as fh:
                data = fh.read()
            mime, _ = mimetypes.guess_type(candidate)
            self.send_response(200)
            self.send_header("Content-Type", mime or "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Cache", "Hit from cloudfront")
            self.end_headers()
            self.wfile.write(data)

        def _handle_request(self, method: str) -> None:
            self.close_connection = True
            parsed = urlparse(self.path)
            path = parsed.path
            body = self._read_body() if method in ("POST", "PUT", "PATCH") else ""
            trace_id = resolve_trace_id(dict(self.headers.items()))

            if method == "OPTIONS":
                self._send_json(200, {"ok": True}, {"X-Amzn-Trace-Id": trace_id})
                return

            # Layer 1: AWS WAFv2 Inspection
            waf_block = state.check_waf(self.client_address[0], self.path, body)
            if waf_block:
                self._send_json(
                    403,
                    {"error": waf_block, "wafAction": "BLOCK"},
                    {"X-Amz-Waf-Action": "BLOCK", "X-Amzn-Trace-Id": trace_id},
                )
                return

            # Layer 2: Cognito Auth Endpoints (Local User Pool Simulator)
            if path == "/auth/signup" and method == "POST":
                try:
                    data = json.loads(body or "{}")
                    res = state.cognito.signup(
                        data.get("email", ""),
                        data.get("password", ""),
                        data.get("name", ""),
                    )
                    self._send_json(201, res, {"X-Amzn-Trace-Id": trace_id})
                except KeyError as exc:
                    self._send_json(409, {"error": str(exc)}, {"X-Amzn-Trace-Id": trace_id})
                except ValueError as exc:
                    self._send_json(400, {"error": str(exc)}, {"X-Amzn-Trace-Id": trace_id})
                return

            if path == "/auth/signin" and method == "POST":
                try:
                    data = json.loads(body or "{}")
                    res = state.cognito.signin(data.get("email", ""), data.get("password", ""))
                    self._send_json(200, res, {"X-Amzn-Trace-Id": trace_id})
                except PermissionError as exc:
                    self._send_json(401, {"error": str(exc)}, {"X-Amzn-Trace-Id": trace_id})
                return

            # Layer 3: Static Frontend via S3 + CloudFront
            if not path.startswith("/api"):
                self._serve_static(path)
                return

            # Layer 4: API Gateway Cognito Authorizer (all /api/* except /api/health)
            claims: Dict[str, Any] = {}
            if path != "/api/health":
                auth_header = self.headers.get("Authorization", "")
                token = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
                claims = state.cognito.verify(token) or {}
                if not claims:
                    self._send_json(
                        401,
                        {"message": "Unauthorized: Valid Cognito JWT token required"},
                        {"X-Amzn-Trace-Id": trace_id},
                    )
                    return

            # Layer 5: API Gateway Response Cache (for GET /api/customers*)
            cache_key = f"{method}:{self.path}"
            if method == "GET" and path.startswith("/api/customers"):
                cached = state.cache_get(cache_key)
                if cached:
                    headers = dict(cached.get("headers", {}))
                    headers["X-Cache"] = "Hit from cloudfront/apigw"
                    headers["X-Amzn-Trace-Id"] = trace_id
                    self._send_json(cached["statusCode"], {}, headers, raw_body=cached["body"])
                    return

            # Layer 6: Invoke AWS Lambda Proxy Integration
            raw_qs = parse_qs(parsed.query)
            qs = {k: v[0] for k, v in raw_qs.items() if v}
            path_params = {}
            if path.startswith("/api/customers/"):
                path_params["id"] = path.split("/")[-1]

            lambda_event = {
                "httpMethod": method,
                "path": path,
                "headers": {**dict(self.headers.items()), "X-Amzn-Trace-Id": trace_id},
                "queryStringParameters": qs or None,
                "pathParameters": path_params or None,
                "body": body or None,
                "requestContext": {"authorizer": {"claims": claims}},
            }
            lambda_resp = api_handler(lambda_event)

            if method in ("POST", "PUT", "DELETE") and 200 <= lambda_resp["statusCode"] < 300:
                state.cache_invalidate()
            elif method == "GET" and path.startswith("/api/customers") and lambda_resp["statusCode"] == 200:
                state.cache_set(cache_key, lambda_resp, ttl=30)

            resp_headers = dict(lambda_resp.get("headers", {}))
            resp_headers.setdefault("X-Cache", "Miss from cloudfront/apigw")
            self._send_json(
                lambda_resp["statusCode"],
                {},
                resp_headers,
                raw_body=lambda_resp["body"],
            )

        def do_OPTIONS(self) -> None:
            self._handle_request("OPTIONS")

        def do_GET(self) -> None:
            self._handle_request("GET")

        def do_POST(self) -> None:
            self._handle_request("POST")

        def do_PUT(self) -> None:
            self._handle_request("PUT")

        def do_DELETE(self) -> None:
            self._handle_request("DELETE")

    return ServerlessGatewayHandler


def main() -> None:
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("LOCAL_DB_PATH", ":memory:")
    waf_limit = int(os.environ.get("WAF_RATE_LIMIT", "120"))

    state = EdgeRuntimeState(db_path=db_path, waf_limit=waf_limit)
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(state))
    print(f"Serverless CRM Stack Simulator listening on http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down local serverless simulator...")
        server.shutdown()


if __name__ == "__main__":
    main()
