"""Automated unit & HTTP integration tests for the Serverless CRM Stack."""

import http.client
import json
import os
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "lambdas"))

os.environ["LOCAL_MODE"] = "true"
os.environ["QUIET_SERVER"] = "1"

from local_server.dev_server import EdgeRuntimeState, make_handler


class ServerlessCRMIntegrationTest(unittest.TestCase):
    """Spins up an isolated local CloudFront/WAF/Cognito/Lambda/DynamoDB server per suite."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.state = EdgeRuntimeState(db_path=":memory:", waf_limit=30)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _request(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        token: str | None = None,
    ) -> tuple[int, dict[str, str], dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        raw = json.dumps(body) if body is not None else None
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        text = resp.read().decode("utf-8")
        conn.close()
        parsed = json.loads(text) if text.startswith("{") else {"raw": text}
        return resp.status, resp_headers, parsed

    def test_01_health_and_frontend_served(self) -> None:
        status, headers, body = self._request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "healthy")
        self.assertIn("x-amzn-trace-id", headers)

        fe_status, fe_headers, _ = self._request("GET", "/")
        self.assertEqual(fe_status, 200)
        self.assertIn("cloudfront", fe_headers.get("x-cache", "").lower())

    def test_02_cognito_auth_required_for_protected_routes(self) -> None:
        status, _, _ = self._request("GET", "/api/customers")
        self.assertEqual(status, 401)

    def test_03_waf_blocks_sqli_and_xss_payloads(self) -> None:
        status_xss, hdr_xss, _ = self._request(
            "POST",
            "/api/customers",
            body={"name": "<script>alert(1)</script>", "email": "x@y.com", "company": "Evil"},
        )
        self.assertEqual(status_xss, 403)
        self.assertEqual(hdr_xss.get("x-amz-waf-action"), "BLOCK")

        status_sqli, _, _ = self._request("GET", "/api/customers?tier=starter%27%20OR%201%3D1")
        self.assertEqual(status_sqli, 403)

    def test_04_full_cognito_crud_cache_gsi_and_streams_workflow(self) -> None:
        # 1. Cognito Sign-Up + PostConfirmation Trigger
        s_up, _, _ = self._request(
            "POST",
            "/auth/signup",
            body={"name": "Mostafa", "email": "mostafa@example.com", "password": "Password123!"},
        )
        self.assertEqual(s_up, 201)

        # 2. Cognito Sign-In -> JWT ID Token
        s_in, _, auth_data = self._request(
            "POST",
            "/auth/signin",
            body={"email": "mostafa@example.com", "password": "Password123!"},
        )
        self.assertEqual(s_in, 200)
        token = auth_data["idToken"]
        self.assertTrue(len(token.split(".")) == 3)

        # 3. Create Customers across Tiers
        c1_status, _, c1 = self._request(
            "POST",
            "/api/customers",
            token=token,
            body={
                "name": "Alice Enterprise",
                "email": "alice@enterprise.io",
                "company": "Vertex Cloud",
                "tier": "enterprise",
                "status": "active",
            },
        )
        self.assertEqual(c1_status, 201)
        customer_id = c1["customer"]["customerId"]

        c2_status, _, _ = self._request(
            "POST",
            "/api/customers",
            token=token,
            body={
                "name": "Bob Starter",
                "email": "bob@starter.io",
                "company": "Seed Labs",
                "tier": "starter",
                "status": "pending",
            },
        )
        self.assertEqual(c2_status, 201)

        # Duplicate email check via EmailIndex
        dup_status, _, _ = self._request(
            "POST",
            "/api/customers",
            token=token,
            body={"name": "Dup", "email": "alice@enterprise.io", "company": "X"},
        )
        self.assertEqual(dup_status, 409)

        # 4. Verify API Gateway Caching (Miss -> Hit)
        g1_status, g1_hdr, _ = self._request("GET", f"/api/customers/{customer_id}", token=token)
        self.assertEqual(g1_status, 200)
        self.assertIn("miss", g1_hdr.get("x-cache", "").lower())

        g2_status, g2_hdr, _ = self._request("GET", f"/api/customers/{customer_id}", token=token)
        self.assertEqual(g2_status, 200)
        self.assertIn("hit", g2_hdr.get("x-cache", "").lower())

        # 5. Query GSI (TierStatusIndex)
        tier_status, _, tier_body = self._request("GET", "/api/customers?tier=enterprise", token=token)
        self.assertEqual(tier_status, 200)
        self.assertEqual(tier_body["count"], 1)
        self.assertEqual(tier_body["customers"][0]["email"], "alice@enterprise.io")

        # 6. Update Customer -> Invalidate Cache & Emit DynamoDB MODIFY Stream
        u_status, _, u_body = self._request(
            "PUT",
            f"/api/customers/{customer_id}",
            token=token,
            body={"company": "Vertex Global AI", "status": "active"},
        )
        self.assertEqual(u_status, 200)
        self.assertEqual(u_body["customer"]["company"], "Vertex Global AI")

        g3_status, g3_hdr, g3_body = self._request("GET", f"/api/customers/{customer_id}", token=token)
        self.assertEqual(g3_status, 200)
        self.assertIn("miss", g3_hdr.get("x-cache", "").lower())
        self.assertEqual(g3_body["customer"]["company"], "Vertex Global AI")

        # 7. Delete Customer -> Emit DynamoDB REMOVE Stream
        d_status, _, _ = self._request("DELETE", f"/api/customers/{customer_id}", token=token)
        self.assertEqual(d_status, 200)

        # 8. Verify DynamoDB Streams Audit Log recorded USER_CONFIRMED, INSERT, MODIFY, REMOVE
        a_status, _, audit_body = self._request("GET", "/api/audit?limit=10", token=token)
        self.assertEqual(a_status, 200)
        event_names = {e["eventName"] for e in audit_body["auditEvents"]}
        self.assertTrue({"USER_CONFIRMED", "INSERT", "MODIFY", "REMOVE"}.issubset(event_names))

        modify_events = [
            e for e in audit_body["auditEvents"]
            if e["eventName"] == "MODIFY" and e["customerId"] == customer_id
        ]
        self.assertEqual(modify_events[0]["changedFields"], ["company"])

    def test_05_waf_rate_limiting_blocks_excessive_bursts(self) -> None:
        burst_state = EdgeRuntimeState(db_path=":memory:", waf_limit=3)
        burst_srv = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(burst_state))
        t = threading.Thread(target=burst_srv.serve_forever, daemon=True)
        t.start()
        try:
            codes = []
            for _ in range(5):
                conn = http.client.HTTPConnection("127.0.0.1", burst_srv.server_port, timeout=3)
                conn.request("GET", "/api/health")
                codes.append(conn.getresponse().status)
                conn.close()
            self.assertEqual(codes[:3], [200, 200, 200])
            self.assertEqual(codes[3:], [403, 403])
        finally:
            burst_srv.shutdown()
            burst_srv.server_close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
