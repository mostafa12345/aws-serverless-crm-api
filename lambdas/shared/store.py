"""Data access layer supporting AWS DynamoDB and a local SQLite DynamoDB/Streams simulator."""

import base64
from decimal import Decimal
import json
import os
import sqlite3
import threading
import time
from typing import Any, Callable, Dict, List, Optional

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError


class ConflictError(Exception):
    """Raised when a uniqueness or conditional check constraint fails."""


def _encode_cursor(payload: Dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def _decode_cursor(token: str) -> Dict[str, Any]:
    raw = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
    return json.loads(raw)


def _to_dynamodb_image(item: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Convert a plain dictionary into DynamoDB Stream AttributeValue format."""
    if item is None:
        return None
    image: Dict[str, Any] = {}
    for key, val in item.items():
        if isinstance(val, bool):
            image[key] = {"BOOL": val}
        elif isinstance(val, (int, float, Decimal)):
            image[key] = {"N": str(val)}
        elif val is not None:
            image[key] = {"S": str(val)}
    return image


class DynamoDBStore:
    """Production storage backed by Amazon DynamoDB tables and GSIs."""

    def __init__(self) -> None:
        region = os.environ.get("AWS_REGION", "us-east-1")
        resource = boto3.resource("dynamodb", region_name=region)
        self.customers = resource.Table(
            os.environ.get("CUSTOMERS_TABLE", "crm-customers")
        )
        self.audit_logs = resource.Table(
            os.environ.get("AUDIT_TABLE", "crm-audit-logs")
        )

    def _find_by_email(self, email: str) -> Optional[Dict[str, Any]]:
        res = self.customers.query(
            IndexName="EmailIndex",
            KeyConditionExpression=Key("email").eq(email),
            Limit=1,
        )
        items = res.get("Items", [])
        return items[0] if items else None

    def create_customer(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if self._find_by_email(item["email"]):
            raise ConflictError(f"Customer with email '{item['email']}' already exists")
        try:
            self.customers.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(customerId)",
            )
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                raise ConflictError("Customer ID collision") from exc
            raise
        return item

    def get_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        res = self.customers.get_item(Key={"customerId": customer_id})
        return res.get("Item")

    def list_customers(
        self,
        tier: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        next_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        kwargs: Dict[str, Any] = {"Limit": limit}
        if next_token:
            kwargs["ExclusiveStartKey"] = _decode_cursor(next_token)

        if tier:
            kwargs["IndexName"] = "TierStatusIndex"
            kwargs["KeyConditionExpression"] = Key("tier").eq(tier)
            kwargs["ScanIndexForward"] = False
            if status:
                kwargs["FilterExpression"] = Attr("status").eq(status)
            res = self.customers.query(**kwargs)
        else:
            if status:
                kwargs["FilterExpression"] = Attr("status").eq(status)
            res = self.customers.scan(**kwargs)

        out: Dict[str, Any] = {"items": res.get("Items", [])}
        if "LastEvaluatedKey" in res:
            out["nextToken"] = _encode_cursor(res["LastEvaluatedKey"])
        return out

    def update_customer(
        self, customer_id: str, updates: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        if not updates:
            return self.get_customer(customer_id)

        expr_parts = []
        expr_names: Dict[str, str] = {}
        expr_values: Dict[str, Any] = {}
        for idx, (field, value) in enumerate(updates.items()):
            name_ph = f"#f{idx}"
            val_ph = f":v{idx}"
            expr_parts.append(f"{name_ph} = {val_ph}")
            expr_names[name_ph] = field
            expr_values[val_ph] = value

        try:
            res = self.customers.update_item(
                Key={"customerId": customer_id},
                UpdateExpression="SET " + ", ".join(expr_parts),
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues=expr_values,
                ConditionExpression="attribute_exists(customerId)",
                ReturnValues="ALL_NEW",
            )
            return res.get("Attributes")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def delete_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        try:
            res = self.customers.delete_item(
                Key={"customerId": customer_id},
                ConditionExpression="attribute_exists(customerId)",
                ReturnValues="ALL_OLD",
            )
            return res.get("Attributes")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return None
            raise

    def write_audit_event(self, event: Dict[str, Any]) -> None:
        self.audit_logs.put_item(Item=event)

    def list_audit_events(
        self, customer_id: Optional[str] = None, limit: int = 25
    ) -> List[Dict[str, Any]]:
        if customer_id:
            res = self.audit_logs.query(
                IndexName="CustomerAuditIndex",
                KeyConditionExpression=Key("customerId").eq(customer_id),
                ScanIndexForward=False,
                Limit=limit,
            )
        else:
            res = self.audit_logs.scan(Limit=limit)
        items = res.get("Items", [])
        items.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return items[:limit]


class SQLiteDynamoSimulatorStore:
    """Thread-safe SQLite store simulating DynamoDB GSIs and DynamoDB Streams."""

    def __init__(self, db_path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._stream_listeners: List[Callable[[Dict[str, Any]], None]] = []
        self._seq = 0
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS customers (
                    customerId TEXT PRIMARY KEY,
                    email TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL,
                    company TEXT NOT NULL,
                    tier TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phone TEXT,
                    ownerId TEXT NOT NULL,
                    createdAt TEXT NOT NULL,
                    updatedAt TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_tier_created ON customers(tier, createdAt)"
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    eventId TEXT PRIMARY KEY,
                    timestamp TEXT NOT NULL,
                    customerId TEXT NOT NULL,
                    eventName TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    changedFields TEXT,
                    expiresAt INTEGER NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_customer ON audit_logs(customerId, timestamp)"
            )
            self._conn.commit()

    def register_stream_listener(
        self, callback: Callable[[Dict[str, Any]], None]
    ) -> None:
        self._stream_listeners.append(callback)

    def _emit_stream_event(
        self,
        event_name: str,
        keys: Dict[str, Any],
        new_image: Optional[Dict[str, Any]],
        old_image: Optional[Dict[str, Any]],
    ) -> None:
        if not self._stream_listeners:
            return
        self._seq += 1
        event_payload = {
            "Records": [
                {
                    "eventID": f"sim-{int(time.time() * 1000)}-{self._seq}",
                    "eventName": event_name,
                    "eventVersion": "1.1",
                    "eventSource": "aws:dynamodb",
                    "awsRegion": os.environ.get("AWS_REGION", "us-east-1"),
                    "dynamodb": {
                        "ApproximateCreationDateTime": int(time.time()),
                        "Keys": _to_dynamodb_image(keys),
                        "NewImage": _to_dynamodb_image(new_image),
                        "OldImage": _to_dynamodb_image(old_image),
                        "SequenceNumber": str(self._seq).zfill(10),
                        "StreamViewType": "NEW_AND_OLD_IMAGES",
                    },
                }
            ]
        }
        for listener in self._stream_listeners:
            listener(event_payload)

    def create_customer(self, item: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO customers (
                        customerId, email, name, company, tier,
                        status, phone, ownerId, createdAt, updatedAt
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        item["customerId"],
                        item["email"],
                        item["name"],
                        item["company"],
                        item["tier"],
                        item["status"],
                        item.get("phone", ""),
                        item["ownerId"],
                        item["createdAt"],
                        item["updatedAt"],
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    f"Customer with email '{item['email']}' or ID already exists"
                ) from exc

        self._emit_stream_event(
            "INSERT", {"customerId": item["customerId"]}, item, None
        )
        return item

    def get_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            row = cur.execute(
                "SELECT * FROM customers WHERE customerId = ?", (customer_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_customers(
        self,
        tier: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 20,
        next_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        offset = 0
        if next_token:
            offset = int(_decode_cursor(next_token).get("offset", 0))

        clauses = []
        params: List[Any] = []
        if tier:
            clauses.append("tier = ?")
            params.append(tier)
        if status:
            clauses.append("status = ?")
            params.append(status)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM customers {where_sql} ORDER BY createdAt DESC LIMIT ? OFFSET ?"
        params.extend([limit + 1, offset])

        with self._lock:
            cur = self._conn.cursor()
            rows = [dict(r) for r in cur.execute(sql, params).fetchall()]

        has_more = len(rows) > limit
        items = rows[:limit]
        result: Dict[str, Any] = {"items": items}
        if has_more:
            result["nextToken"] = _encode_cursor({"offset": offset + limit})
        return result

    def update_customer(
        self, customer_id: str, updates: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        old_item = self.get_customer(customer_id)
        if not old_item:
            return None
        if not updates:
            return old_item

        allowed = {"name", "company", "tier", "status", "phone", "updatedAt"}
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return old_item

        assignments = ", ".join(f"{col} = ?" for col in filtered)
        values = list(filtered.values()) + [customer_id]

        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                f"UPDATE customers SET {assignments} WHERE customerId = ?", values
            )
            self._conn.commit()

        new_item = self.get_customer(customer_id)
        self._emit_stream_event(
            "MODIFY", {"customerId": customer_id}, new_item, old_item
        )
        return new_item

    def delete_customer(self, customer_id: str) -> Optional[Dict[str, Any]]:
        old_item = self.get_customer(customer_id)
        if not old_item:
            return None

        with self._lock:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM customers WHERE customerId = ?", (customer_id,))
            self._conn.commit()

        self._emit_stream_event(
            "REMOVE", {"customerId": customer_id}, None, old_item
        )
        return old_item

    def write_audit_event(self, event: Dict[str, Any]) -> None:
        with self._lock:
            cur = self._conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO audit_logs (
                    eventId, timestamp, customerId, eventName,
                    summary, changedFields, expiresAt
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event["eventId"],
                    event["timestamp"],
                    event["customerId"],
                    event["eventName"],
                    event["summary"],
                    json.dumps(event.get("changedFields", [])),
                    int(event["expiresAt"]),
                ),
            )
            self._conn.commit()

    def list_audit_events(
        self, customer_id: Optional[str] = None, limit: int = 25
    ) -> List[Dict[str, Any]]:
        with self._lock:
            cur = self._conn.cursor()
            if customer_id:
                rows = cur.execute(
                    "SELECT * FROM audit_logs WHERE customerId = ? ORDER BY timestamp DESC LIMIT ?",
                    (customer_id, limit),
                ).fetchall()
            else:
                rows = cur.execute(
                    "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        events = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("changedFields"), str):
                item["changedFields"] = json.loads(item["changedFields"])
            events.append(item)
        return events


_STORE_SINGLETON: Optional[Any] = None
_STORE_LOCK = threading.Lock()


def get_store(custom_store: Optional[Any] = None) -> Any:
    """Return the active store backend (DynamoDB in AWS or SQLite simulator locally)."""
    global _STORE_SINGLETON
    if custom_store is not None:
        return custom_store
    with _STORE_LOCK:
        if _STORE_SINGLETON is None:
            if os.environ.get("LOCAL_MODE", "").lower() == "true":
                db_path = os.environ.get("LOCAL_DB_PATH", ":memory:")
                _STORE_SINGLETON = SQLiteDynamoSimulatorStore(db_path=db_path)
            else:
                _STORE_SINGLETON = DynamoDBStore()
        return _STORE_SINGLETON


def reset_local_store(store: Optional[Any] = None) -> None:
    """Override or reset the store singleton (primarily for local server and tests)."""
    global _STORE_SINGLETON
    with _STORE_LOCK:
        _STORE_SINGLETON = store
