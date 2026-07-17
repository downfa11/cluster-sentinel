from __future__ import annotations

import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, cast

from sqlglot import exp, parse

from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult

DatabaseName = Literal["commerce", "wargame"]
MAX_QUERY_ROWS = 200
DEFAULT_QUERY_ROWS = 100
MAX_SLACK_ROWS = 50
MAX_SLACK_BYTES = 64 * 1024
QUERY_TIMEOUT_SECONDS = 5
READ_ROUTER_HOST = "home-mysql.mysql-prod.svc.cluster.local"
READ_CREDENTIAL_ENVS = {
    "commerce": ("SENTINEL_COMMERCE_DB_USERNAME", "SENTINEL_COMMERCE_DB_PASSWORD"),
    "wargame": ("SENTINEL_WARGAME_DB_USERNAME", "SENTINEL_WARGAME_DB_PASSWORD"),
}
SLACK_ENVELOPE_BYTES = 512
SYSTEM_SCHEMAS = {"information_schema", "mysql", "performance_schema", "sys"}
BLOCKED_NODE_KEYS = {
    "alter",
    "call",
    "command",
    "commit",
    "create",
    "delete",
    "drop",
    "grant",
    "insert",
    "into",
    "load_data",
    "lock",
    "merge",
    "replace",
    "parameter",
    "sessionparameter",
    "revoke",
    "rollback",
    "set",
    "transaction",
    "truncate_table",
    "update",
    "use",
}
BLOCKED_FUNCTIONS = {
    "BENCHMARK",
    "GET_LOCK",
    "IS_FREE_LOCK",
    "IS_USED_LOCK",
    "LOAD_FILE",
    "MASTER_POS_WAIT",
    "RELEASE_ALL_LOCKS",
    "RELEASE_LOCK",
    "SLEEP",
    "WAIT_FOR_EXECUTED_GTID_SET",
}
SENSITIVE_COLUMN_PARTS = {
    "access_key",
    "api_key",
    "credential",
    "encryption_key",
    "password",
    "passwd",
    "private_key",
    "secret",
    "signing_key",
    "token",
}


class Cursor(Protocol):
    def __enter__(self) -> Cursor: ...

    def __exit__(self, *args: object) -> None: ...

    def execute(self, sql: str, args: object | None = None) -> int: ...

    def fetchall(self) -> list[dict[str, Any]]: ...


class Connection(Protocol):
    def __enter__(self) -> Connection: ...

    def __exit__(self, *args: object) -> None: ...

    def cursor(self) -> Cursor: ...


Connector = Callable[..., Connection]


@dataclass(frozen=True)
class DatabaseTarget:
    database: DatabaseName
    host: str
    port: int
    username_env: str
    password_env: str


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str
    sql_hash: str
    limit: int


def validate_readonly_sql(
    database: DatabaseName, sql: str, limit: int = DEFAULT_QUERY_ROWS
) -> ValidatedQuery:
    requested_limit = max(1, min(int(limit), MAX_QUERY_ROWS))
    try:
        statements = [statement for statement in parse(sql, read="mysql") if statement is not None]
    except Exception as exc:
        raise ValueError("SQL could not be parsed as MySQL") from exc
    if len(statements) != 1:
        raise ValueError("exactly one SQL statement is required")

    statement = statements[0]
    if not isinstance(statement, exp.Select):
        raise ValueError("only SELECT or WITH ... SELECT is allowed")

    for node in statement.walk():
        if node.key.lower() in BLOCKED_NODE_KEYS:
            raise ValueError(f"blocked SQL operation: {node.key.upper()}")
        if isinstance(node, exp.Func):
            function_name = str(node.this) if isinstance(node, exp.Anonymous) else node.sql_name()
            if function_name.upper() in BLOCKED_FUNCTIONS:
                raise ValueError(f"blocked SQL function: {function_name.upper()}")

    for column in statement.find_all(exp.Column):
        if _is_sensitive_column(column.name):
            raise ValueError("sensitive columns are not allowed")

    for table in statement.find_all(exp.Table):
        qualifier = table.db.lower()
        catalog = table.catalog.lower()
        if qualifier in SYSTEM_SCHEMAS:
            raise ValueError("system schema access is not allowed")
        if catalog or (qualifier and qualifier != database):
            raise ValueError("cross-database references are not allowed")

    query_limit = requested_limit
    limit_node = statement.args.get("limit")
    if limit_node is not None:
        expression = getattr(limit_node, "expression", None)
        if not isinstance(expression, exp.Literal) or not expression.is_int:
            raise ValueError("LIMIT must be an integer literal")
        query_limit = min(int(expression.this), requested_limit, MAX_QUERY_ROWS)
        if query_limit < 1:
            raise ValueError("LIMIT must be positive")
    statement.limit(query_limit, copy=False)
    normalized = statement.sql(dialect="mysql", pretty=False)
    return ValidatedQuery(
        sql=normalized,
        sql_hash=hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        limit=query_limit,
    )


class DatabaseService:
    def __init__(
        self,
        settings: Settings,
        audit: AuditLogger,
        connector: Connector | None = None,
    ) -> None:
        self.settings = settings
        self.audit = audit
        self._connector = connector or self._pymysql_connect

    def get_schema(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        try:
            target = self._target(args.get("database"))
            started = time.monotonic()
            sql = (
                "SELECT table_name, column_name, data_type, is_nullable "
                "FROM information_schema.columns WHERE table_schema = %s "
                "ORDER BY table_name, ordinal_position LIMIT 2000"
            )
            rows = self._run_with_timeout(target, sql, (target.database,))
        except Exception as exc:
            return ToolResult(False, self._safe_database_error(exc))

        metadata: dict[str, list[dict[str, str]]] = {}
        for row in rows:
            column = str(row.get("column_name", ""))
            if _is_sensitive_column(column):
                continue
            metadata.setdefault(str(row.get("table_name", "")), []).append(
                {
                    "name": column,
                    "type": str(row.get("data_type", "")),
                    "nullable": str(row.get("is_nullable", "")),
                }
            )
        duration_ms = round((time.monotonic() - started) * 1000)
        self.audit.write(
            "database.schema.completed",
            request,
            "success",
            {
                "database": target.database,
                "duration_ms": duration_ms,
                "table_count": len(metadata),
            },
        )
        return ToolResult(
            True,
            f"Schema metadata loaded for {target.database}",
            {"database": target.database, "schema": metadata, "untrusted_data": True},
        )

    def query_readonly(self, request: OperationRequest, args: dict[str, Any]) -> ToolResult:
        try:
            target = self._target(args.get("database"))
            validated = validate_readonly_sql(
                target.database,
                str(args.get("sql", "")),
                int(args.get("limit", DEFAULT_QUERY_ROWS)),
            )
        except Exception as exc:
            return ToolResult(False, self._safe_database_error(exc))

        started = time.monotonic()
        try:
            rows = self._run_with_timeout(target, validated.sql)
        except Exception as exc:
            self.audit.write(
                "database.query.completed",
                request,
                "error",
                {
                    "database": target.database,
                    "duration_ms": round((time.monotonic() - started) * 1000),
                    "row_count": 0,
                    "sql_hash": validated.sql_hash,
                },
            )
            return ToolResult(False, self._safe_database_error(exc))

        columns = list(rows[0].keys()) if rows else []
        masked_rows = [
            {
                column: "[MASKED]" if _is_sensitive_column(column) else row.get(column)
                for column in columns
            }
            for row in rows
        ]
        rendered, displayed_rows, size_truncated = render_slack_table(columns, masked_rows)
        truncated = displayed_rows < len(rows) or size_truncated
        duration_ms = round((time.monotonic() - started) * 1000)
        self.audit.write(
            "database.query.completed",
            request,
            "success",
            {
                "database": target.database,
                "duration_ms": duration_ms,
                "row_count": len(rows),
                "sql_hash": validated.sql_hash,
            },
        )
        return ToolResult(
            True,
            f"Read-only query completed for {target.database}",
            {
                "database": target.database,
                "row_count": len(rows),
                "displayed_rows": displayed_rows,
                "truncated": truncated,
                "slack_table": rendered,
            },
        )

    def _target(self, raw_database: object) -> DatabaseTarget:
        database = str(raw_database)
        if database not in {"commerce", "wargame"}:
            raise ValueError("database must be commerce or wargame")
        if not self.settings.db_read_enabled:
            raise ValueError("production database queries are disabled")
        raw = self.settings.db_read_targets.get(database)
        if not raw:
            raise ValueError(f"database target metadata is missing for {database}")
        host = raw.get("host", "")
        port = int(raw.get("port", "0"))
        username_env = raw.get("username_env", "")
        password_env = raw.get("password_env", "")
        expected_envs = READ_CREDENTIAL_ENVS[database]
        if (
            host != READ_ROUTER_HOST
            or port != 6447
            or raw.get("database") != database
            or (username_env, password_env) != expected_envs
        ):
            raise ValueError("database target metadata is invalid")
        return DatabaseTarget(cast(DatabaseName, database), host, port, username_env, password_env)

    def _run_with_timeout(
        self, target: DatabaseTarget, sql: str, parameters: object | None = None
    ) -> list[dict[str, Any]]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sentinel-db-read")
        future = executor.submit(self._execute, target, sql, parameters)
        try:
            return future.result(timeout=QUERY_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            future.cancel()
            raise TimeoutError("database query timed out") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _execute(
        self, target: DatabaseTarget, sql: str, parameters: object | None
    ) -> list[dict[str, Any]]:
        username = os.getenv(target.username_env)
        password = os.getenv(target.password_env)
        if not username or not password:
            raise RuntimeError("database credentials are unavailable")
        with self._connector(
            host=target.host,
            port=target.port,
            user=username,
            password=password,
            database=target.database,
            connect_timeout=QUERY_TIMEOUT_SECONDS,
            read_timeout=QUERY_TIMEOUT_SECONDS,
            write_timeout=QUERY_TIMEOUT_SECONDS,
            autocommit=True,
            client_flag=0,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, parameters)
                return cursor.fetchall()

    def _pymysql_connect(self, **kwargs: object) -> Connection:
        import pymysql
        from pymysql.cursors import DictCursor

        return cast(Connection, pymysql.connect(cursorclass=DictCursor, **kwargs))  # type: ignore[call-overload]

    def _safe_database_error(self, exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "database query exceeded the 5 second timeout"
        if isinstance(exc, ValueError):
            return str(exc)
        return f"database query failed ({exc.__class__.__name__})"


def _is_sensitive_column(name: str) -> bool:
    normalized = name.lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in SENSITIVE_COLUMN_PARTS)


def render_slack_table(columns: list[str], rows: list[dict[str, Any]]) -> tuple[str, int, bool]:
    if not columns:
        return "_No rows returned._", 0, False
    safe_columns = [column.replace("`", "'") for column in columns]
    values = [
        [str(row.get(column, "")).replace("\n", " ").replace("`", "'") for column in columns]
        for row in rows[:MAX_SLACK_ROWS]
    ]
    widths = [
        min(80, max([len(safe_columns[index]), *[len(row[index]) for row in values]]))
        for index in range(len(columns))
    ]

    def line(parts: list[str]) -> str:
        clipped = [value[: widths[index]] for index, value in enumerate(parts)]
        return (
            "| "
            + " | ".join(value.ljust(widths[index]) for index, value in enumerate(clipped))
            + " |"
        )

    output = ["```", line(safe_columns), line(["-" * width for width in widths])]
    displayed = 0
    size_truncated = False
    for row in values:
        candidate = "\n".join([*output, line(row), "```"])
        if len(candidate.encode("utf-8")) > MAX_SLACK_BYTES - SLACK_ENVELOPE_BYTES:
            size_truncated = True
            break
        output.append(line(row))
        displayed += 1
    output.append("```")
    return "\n".join(output), displayed, size_truncated


def serialize_untrusted_schema(result: ToolResult) -> str:
    return json.dumps(result.data, ensure_ascii=False, sort_keys=True)
