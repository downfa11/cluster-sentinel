from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from contextlib import suppress
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Callable, Literal, Protocol, TypeVar, cast

from sqlglot import exp, parse

from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.models import OperationRequest, ToolResult

DatabaseName = Literal["commerce", "wargame"]
MAX_QUERY_ROWS = 200
DEFAULT_QUERY_ROWS = 100
MAX_SLACK_ROWS = 50
MAX_SLACK_BYTES = 64 * 1024
MAX_SLACK_CHARACTERS = 40_000
QUERY_TIMEOUT_SECONDS = 5
READ_ROUTER_HOST = "home-mysql.mysql-prod.svc.cluster.local"
MAX_SCHEMA_ROWS = 2000
MAX_CELL_CHARACTERS = 80
MAX_RESULT_BUFFER_BYTES = MAX_SLACK_BYTES - 512
MAX_RESULT_CHARACTERS = MAX_SLACK_CHARACTERS - 512
ALLOWED_ANONYMOUS_FUNCTIONS = frozenset({"CRC32", "INET_ATON", "INET_NTOA"})
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

    def fetchone(self) -> dict[str, Any] | None: ...


class Connection(Protocol):
    def __enter__(self) -> Connection: ...

    def __exit__(self, *args: object) -> None: ...

    def cursor(self) -> Cursor: ...

    def close(self) -> None: ...


Connector = Callable[..., Connection]
ResultType = TypeVar("ResultType")


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


@dataclass(frozen=True)
class StreamedQueryResult:
    columns: list[str]
    rows: list[dict[str, Any]]
    row_count: int
    truncated: bool


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

            if isinstance(node, exp.Anonymous) and (
                isinstance(node.parent, exp.Dot)
                or function_name.upper() not in ALLOWED_ANONYMOUS_FUNCTIONS
            ):
                raise ValueError(f"unapproved SQL function: {function_name.upper()}")
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
                "ORDER BY table_name, ordinal_position LIMIT 2001"
            )
            rows = self._run_with_timeout(target, sql, (target.database,), self._fetch_schema_rows)
        except Exception as exc:
            return ToolResult(False, self._safe_database_error(exc))

        if len(rows) > MAX_SCHEMA_ROWS:
            return ToolResult(
                False,
                "database schema metadata exceeds the safe limit; refusing partial schema",
            )
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
            query_result = self._run_with_timeout(
                target, validated.sql, None, self._fetch_query_rows
            )
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

        rendered, displayed_rows, size_truncated = render_slack_table(
            query_result.columns, query_result.rows
        )
        truncated = query_result.truncated or size_truncated
        duration_ms = round((time.monotonic() - started) * 1000)
        self.audit.write(
            "database.query.completed",
            request,
            "success",
            {
                "database": target.database,
                "duration_ms": duration_ms,
                "row_count": query_result.row_count,
                "sql_hash": validated.sql_hash,
            },
        )
        return ToolResult(
            True,
            f"Read-only query completed for {target.database}",
            {
                "database": target.database,
                "row_count": query_result.row_count,
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
        self,
        target: DatabaseTarget,
        sql: str,
        parameters: object | None,
        fetch_rows: Callable[[Cursor], ResultType],
    ) -> ResultType:
        lock = threading.Lock()
        active_connection: list[Connection | None] = [None]
        timed_out = threading.Event()

        def execute() -> ResultType:
            return self._execute(
                target,
                sql,
                parameters,
                fetch_rows,
                active_connection,
                lock,
                timed_out,
            )

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="sentinel-db-read")
        future = executor.submit(execute)
        try:
            return future.result(timeout=QUERY_TIMEOUT_SECONDS)
        except FutureTimeoutError as exc:
            timed_out.set()
            with lock:
                connection = active_connection[0]
            if connection is not None:
                with suppress(Exception):
                    connection.close()
            future.cancel()
            raise TimeoutError("database query timed out") from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _execute(
        self,
        target: DatabaseTarget,
        sql: str,
        parameters: object | None,
        fetch_rows: Callable[[Cursor], ResultType],
        active_connection: list[Connection | None],
        lock: threading.Lock,
        timed_out: threading.Event,
    ) -> ResultType:
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
            with lock:
                if timed_out.is_set():
                    connection.close()
                    raise TimeoutError("database query timed out")
                active_connection[0] = connection
            try:
                with connection.cursor() as cursor:
                    cursor.execute(sql, parameters)
                    return fetch_rows(cursor)
            finally:
                with lock:
                    if active_connection[0] is connection:
                        active_connection[0] = None

    def _fetch_schema_rows(self, cursor: Cursor) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while len(rows) <= MAX_SCHEMA_ROWS:
            row = cursor.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def _fetch_query_rows(self, cursor: Cursor) -> StreamedQueryResult:
        columns: list[str] = []
        rows: list[dict[str, Any]] = []
        row_count = 0
        buffered_bytes = 0
        truncated = False
        buffer_full = False
        while (row := cursor.fetchone()) is not None:
            row_count += 1
            if not columns:
                columns = list(row.keys())
            if buffer_full or len(rows) >= MAX_SLACK_ROWS:
                truncated = True
                continue
            bounded_row: dict[str, Any] = {}
            row_bytes = 0
            row_clipped = False
            for column in columns:
                if _is_sensitive_column(column):
                    value = "[MASKED]"
                    clipped = False
                else:
                    value, clipped = _bounded_cell(row.get(column))
                bounded_row[column] = value
                row_bytes += len(column.encode("utf-8")) + len(value.encode("utf-8")) + 8
                row_clipped = row_clipped or clipped
            if buffered_bytes + row_bytes > MAX_RESULT_BUFFER_BYTES:
                buffer_full = True
                truncated = True
                continue
            rows.append(bounded_row)
            buffered_bytes += row_bytes
            truncated = truncated or row_clipped
        return StreamedQueryResult(columns, rows, row_count, truncated)

    def _pymysql_connect(self, **kwargs: object) -> Connection:
        import pymysql
        from pymysql.cursors import SSDictCursor

        return cast(Connection, pymysql.connect(cursorclass=SSDictCursor, **kwargs))  # type: ignore[call-overload]

    def _safe_database_error(self, exc: Exception) -> str:
        if isinstance(exc, TimeoutError):
            return "database query exceeded the 5 second timeout"
        if isinstance(exc, ValueError):
            return str(exc)
        return f"database query failed ({exc.__class__.__name__})"


def _is_sensitive_column(name: str) -> bool:
    normalized = name.lower().replace("-", "_").replace(" ", "_")
    return any(part in normalized for part in SENSITIVE_COLUMN_PARTS)


def _bounded_cell(value: Any) -> tuple[str, bool]:
    if value is None:
        return "", False
    if isinstance(value, bytes):
        clipped = len(value) > MAX_CELL_CHARACTERS
        text = value[:MAX_CELL_CHARACTERS].decode("utf-8", errors="replace")
    else:
        text = str(value)
        clipped = len(text) > MAX_CELL_CHARACTERS
        text = text[:MAX_CELL_CHARACTERS]
    return text.replace("\n", " ").replace("`", "'"), clipped


def render_slack_table(columns: list[str], rows: list[dict[str, Any]]) -> tuple[str, int, bool]:
    if not columns:
        return "_No rows returned._", 0, False
    safe_columns = [column.replace("`", "'") for column in columns]
    row_limit_truncated = len(rows) > MAX_SLACK_ROWS
    source_rows = rows[:MAX_SLACK_ROWS]
    selected_indexes: list[int] = []
    selected_values: list[list[str]] = [[] for _ in source_rows]
    widths: list[int] = []
    size_truncated = row_limit_truncated

    def line(parts: list[str], line_widths: list[int]) -> str:
        clipped = [value[: line_widths[index]] for index, value in enumerate(parts)]
        return (
            "| "
            + " | ".join(value.ljust(line_widths[index]) for index, value in enumerate(clipped))
            + " |"
        )

    for column_index, column in enumerate(columns):
        candidate_values = [
            str(row.get(column, "")).replace("\n", " ").replace("`", "'") for row in source_rows
        ]
        candidate_width = min(
            MAX_CELL_CHARACTERS,
            max([len(safe_columns[column_index]), *[len(value) for value in candidate_values]]),
        )
        candidate_indexes = [*selected_indexes, column_index]
        candidate_widths = [*widths, candidate_width]
        candidate_headers = [safe_columns[index] for index in candidate_indexes]
        base = "\n".join(
            [
                "```",
                line(candidate_headers, candidate_widths),
                line(["-" * width for width in candidate_widths], candidate_widths),
                "```",
            ]
        )
        if len(base.encode("utf-8")) > MAX_RESULT_BUFFER_BYTES or len(base) > MAX_RESULT_CHARACTERS:
            size_truncated = True
            break
        selected_indexes = candidate_indexes
        widths = candidate_widths
        for row_index, value in enumerate(candidate_values):
            selected_values[row_index].append(value)
        if len(column) > candidate_width or any(
            len(value) > candidate_width for value in candidate_values
        ):
            size_truncated = True

    if len(selected_indexes) < len(columns):
        size_truncated = True
    headers = [safe_columns[index] for index in selected_indexes]
    output = ["```", line(headers, widths), line(["-" * width for width in widths], widths)]
    displayed = 0
    for row in selected_values:
        candidate = "\n".join([*output, line(row, widths), "```"])
        if (
            len(candidate.encode("utf-8")) > MAX_RESULT_BUFFER_BYTES
            or len(candidate) > MAX_RESULT_CHARACTERS
        ):
            size_truncated = True
            break
        output.append(line(row, widths))
        displayed += 1
    output.append("```")
    return "\n".join(output), displayed, size_truncated


def serialize_untrusted_schema(result: ToolResult) -> str:
    return json.dumps(result.data, ensure_ascii=False, sort_keys=True)
