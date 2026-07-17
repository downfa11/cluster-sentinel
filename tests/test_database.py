from __future__ import annotations

import logging
import time
from typing import Any

import pytest

import sentinel.database as database_module
from sentinel.agent.tools import ToolRegistry
from sentinel.agent.orchestrator import AgentOrchestrator
from sentinel.audit import AuditLogger
from sentinel.config import Settings
from sentinel.database import (
    MAX_SLACK_BYTES,
    DatabaseService,
    render_slack_table,
    validate_readonly_sql,
)
from sentinel.identity import IdentityResolver
from sentinel.integrations.argocd import ArgoCdClient
from sentinel.integrations.github import GitHubClient
from sentinel.integrations.grafana import GrafanaClient
from sentinel.models import OperationRequest, Principal, Role, ToolResult
from sentinel.policy import PolicyEngine
from sentinel.runtime import SentinelRuntime


def request_for(role: Role) -> OperationRequest:
    return OperationRequest(
        request_id="db-request",
        channel_id="C1",
        text="database question",
        principal=Principal(
            user_id="U1",
            slack_user_id="U1",
            github_username=None,
            roles={role},
        ),
    )


def database_settings() -> Settings:
    return Settings(
        db_read_enabled=True,
        db_read_targets={
            "commerce": {
                "database": "commerce",
                "host": database_module.READ_ROUTER_HOST,
                "port": "6447",
                "username_env": "SENTINEL_COMMERCE_DB_USERNAME",
                "password_env": "SENTINEL_COMMERCE_DB_PASSWORD",
            },
            "wargame": {
                "database": "wargame",
                "host": database_module.READ_ROUTER_HOST,
                "port": "6447",
                "username_env": "SENTINEL_WARGAME_DB_USERNAME",
                "password_env": "SENTINEL_WARGAME_DB_PASSWORD",
            },
        },
    )


class FakeCursor:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        executed: list[tuple[str, object | None]],
        error: Exception | None = None,
        delay: float = 0,
    ) -> None:
        self.rows = rows
        self.executed = executed
        self.error = error
        self.delay = delay

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, sql: str, args: object | None = None) -> int:
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        self.executed.append((sql, args))
        return len(self.rows)

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class FakeConnection:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def cursor(self) -> FakeCursor:
        return self._cursor


class FakeConnector:
    def __init__(
        self,
        rows: list[dict[str, Any]],
        error: Exception | None = None,
        delay: float = 0,
    ) -> None:
        self.rows = rows
        self.error = error
        self.delay = delay
        self.executed: list[tuple[str, object | None]] = []
        self.kwargs: dict[str, object] = {}

    def __call__(self, **kwargs: object) -> FakeConnection:
        self.kwargs = kwargs
        return FakeConnection(FakeCursor(self.rows, self.executed, self.error, self.delay))


@pytest.fixture(autouse=True)
def mock_database_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SENTINEL_COMMERCE_DB_USERNAME", "synthetic-user")
    monkeypatch.setenv("SENTINEL_COMMERCE_DB_PASSWORD", "synthetic-value")
    monkeypatch.setenv("SENTINEL_WARGAME_DB_USERNAME", "synthetic-user")
    monkeypatch.setenv("SENTINEL_WARGAME_DB_PASSWORD", "synthetic-value")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, status FROM orders",
        "SELECT o.id, i.sku FROM orders o JOIN order_items i ON i.order_id = o.id",
        "SELECT status, COUNT(*) FROM orders GROUP BY status",
        "WITH recent AS (SELECT id FROM orders) SELECT id FROM recent",
    ],
)
def test_readonly_validator_allows_select_forms(sql: str) -> None:
    validated = validate_readonly_sql("commerce", sql)
    assert validated.sql.endswith("LIMIT 100")
    assert len(validated.sql_hash) == 64


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO orders(id) VALUES (1)",
        "UPDATE orders SET status = 'done'",
        "DELETE FROM orders",
        "CREATE TABLE unsafe(id INT)",
        "ALTER TABLE orders ADD COLUMN unsafe INT",
        "DROP TABLE orders",
        "CALL unsafe()",
        "SET @x = 1",
        "START TRANSACTION",
        "SELECT * FROM orders FOR UPDATE",
    ],
)
def test_readonly_validator_rejects_write_and_management_sql(sql: str) -> None:
    with pytest.raises(ValueError):
        validate_readonly_sql("commerce", sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "SELECT * FROM mysql.user",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM performance_schema.threads",
        "SELECT * FROM sys.user_summary",
        "SELECT * FROM wargame.matches",
        "SELECT SLEEP(1)",
        "SELECT BENCHMARK(2, MD5('x'))",
        "SELECT LOAD_FILE('/tmp/x')",
        "SELECT * INTO OUTFILE '/tmp/x' FROM orders",
        "SELECT password_hash AS harmless_name FROM users",
        "SELECT @@global.max_connections",
        "SELECT GET_LOCK('sentinel', 1)",
        "SELECT * FROM other_catalog.commerce.orders",
    ],
)
def test_readonly_validator_rejects_multiple_cross_database_and_unsafe_functions(
    sql: str,
) -> None:
    with pytest.raises(ValueError):
        validate_readonly_sql("commerce", sql)


def test_readonly_validator_inserts_and_caps_limit() -> None:
    assert validate_readonly_sql("commerce", "SELECT * FROM orders", 25).limit == 25
    capped = validate_readonly_sql("commerce", "SELECT * FROM orders LIMIT 999", 200)
    assert capped.limit == 200
    assert capped.sql.endswith("LIMIT 200")
    preserved = validate_readonly_sql("commerce", "SELECT * FROM orders LIMIT 5", 100)
    assert preserved.limit == 5


def test_target_metadata_cannot_redirect_credentials() -> None:
    targets = {name: dict(target) for name, target in database_settings().db_read_targets.items()}
    targets["commerce"]["host"] = "attacker.invalid"
    connector = FakeConnector([{"id": 1}])
    service = DatabaseService(
        Settings(db_read_enabled=True, db_read_targets=targets),
        AuditLogger(),
        connector,
    )
    result = service.query_readonly(
        request_for(Role.ADMIN),
        {"database": "commerce", "sql": "SELECT id FROM orders", "reason": "test"},
    )
    assert not result.ok
    assert connector.kwargs == {}


def test_query_uses_read_router_timeouts_masks_and_truncates() -> None:
    rows = [
        {"id": index, "api_token": f"value-{index}", "status": "complete"} for index in range(75)
    ]
    connector = FakeConnector(rows)
    service = DatabaseService(database_settings(), AuditLogger(), connector)
    result = service.query_readonly(
        request_for(Role.ADMIN),
        {
            "database": "commerce",
            "sql": "SELECT id, status FROM orders",
            "reason": "test",
            "limit": 100,
        },
    )

    assert result.ok
    assert result.data["row_count"] == 75
    assert result.data["displayed_rows"] == 50
    assert result.data["truncated"] is True
    assert "[MASKED]" in result.data["slack_table"]
    assert "value-1" not in result.data["slack_table"]
    assert connector.kwargs["port"] == 6447
    assert connector.kwargs["read_timeout"] == 5
    assert connector.kwargs["write_timeout"] == 5
    assert connector.kwargs["client_flag"] == 0
    assert connector.executed[0][0].endswith("LIMIT 100")


def test_schema_returns_only_metadata_and_hides_sensitive_columns() -> None:
    connector = FakeConnector(
        [
            {
                "table_name": "users",
                "column_name": "id",
                "data_type": "bigint",
                "is_nullable": "NO",
            },
            {
                "table_name": "users",
                "column_name": "password_hash",
                "data_type": "varchar",
                "is_nullable": "NO",
            },
        ]
    )
    service = DatabaseService(database_settings(), AuditLogger(), connector)
    result = service.get_schema(
        request_for(Role.OPERATOR),
        {"database": "commerce", "reason": "build query"},
    )
    assert result.ok
    assert result.data["untrusted_data"] is True
    assert result.data["schema"]["users"] == [{"name": "id", "type": "bigint", "nullable": "NO"}]
    assert connector.executed[0][1] == ("commerce",)


def test_query_timeout_is_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database_module, "QUERY_TIMEOUT_SECONDS", 0.01)
    service = DatabaseService(
        database_settings(),
        AuditLogger(),
        FakeConnector([{"id": 1}], delay=0.05),
    )
    result = service.query_readonly(
        request_for(Role.ADMIN),
        {"database": "commerce", "sql": "SELECT id FROM orders", "reason": "test"},
    )
    assert not result.ok
    assert "timeout" in result.message
    assert "mysql-router.test" not in result.message


def test_database_error_and_audit_do_not_leak_query_or_credentials(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="sentinel.audit")
    connector = FakeConnector(
        [],
        error=RuntimeError(
            "mysql-router.test synthetic-user synthetic-value SELECT private_data FROM users"
        ),
    )
    service = DatabaseService(database_settings(), AuditLogger(), connector)
    result = service.query_readonly(
        request_for(Role.ADMIN),
        {
            "database": "commerce",
            "sql": "SELECT private_data FROM users",
            "reason": "sensitive reason",
        },
    )
    assert not result.ok
    assert result.message == "database query failed (RuntimeError)"
    audit_text = caplog.text
    assert "private_data" not in audit_text
    assert "synthetic-user" not in audit_text
    assert "synthetic-value" not in audit_text
    assert "sql_hash" in audit_text


@pytest.mark.parametrize("role", [Role.ADMIN, Role.OPERATOR])
def test_database_policy_allows_admin_and_operator(role: Role) -> None:
    decision = PolicyEngine().authorize_tool_call(
        request_for(role),
        "db_query_readonly",
        {"database": "commerce"},
    )
    assert decision.allowed


@pytest.mark.parametrize("role", [Role.DEV, Role.GUI_USER])
def test_database_policy_denies_dev_and_gui_user(role: Role) -> None:
    decision = PolicyEngine().authorize_tool_call(
        request_for(role),
        "db_get_schema",
        {"database": "commerce"},
    )
    assert not decision.allowed


def test_unknown_slack_user_is_fail_closed() -> None:
    principal = IdentityResolver(Settings()).resolve_slack_user("U-UNKNOWN")
    assert principal.roles == set()
    decision = PolicyEngine().authorize_request(
        OperationRequest("unknown", "C1", "status", principal)
    )
    assert not decision.allowed


def test_tool_registry_never_audits_sql_text(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="sentinel.audit")
    connector = FakeConnector([{"count": 3}])
    settings = database_settings()
    audit = AuditLogger()
    registry = ToolRegistry(
        PolicyEngine(),
        GitHubClient(settings),
        audit,
        ArgoCdClient(settings),
        GrafanaClient(settings),
        DatabaseService(settings, audit, connector),
    )
    result = registry.execute(
        request_for(Role.ADMIN),
        "db_query_readonly",
        {
            "database": "commerce",
            "sql": "SELECT COUNT(*) AS count FROM orders",
            "reason": "do not log this reason",
        },
    )
    assert result.ok
    assert "SELECT COUNT" not in caplog.text
    assert "do not log this reason" not in caplog.text


def test_slack_table_honors_row_and_byte_limits() -> None:
    columns = [f"column_{index}" for index in range(20)]
    rows = [{column: ("한글" * 200) for column in columns} for _ in range(60)]
    rendered, displayed, truncated = render_slack_table(columns, rows)
    assert displayed <= 50
    assert len(rendered.encode("utf-8")) <= MAX_SLACK_BYTES
    assert truncated or displayed == 50


def test_runtime_formats_database_table_and_truncation() -> None:
    runtime = object.__new__(SentinelRuntime)
    result = ToolResult(
        True,
        "Read-only query completed for commerce",
        {
            "row_count": 75,
            "displayed_rows": 50,
            "truncated": True,
            "slack_table": "```\n| id |\n```",
        },
    )
    text = runtime.format_result(result)
    assert "Rows: 75; displayed: 50 (truncated)" in text
    assert "| id |" in text


class FakeFunctionCall:
    type = "function_call"

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class SequencedDatabaseResponses:
    def __init__(self) -> None:
        self.inputs: list[object] = []

    def create(self, **kwargs: object) -> object:
        self.inputs.append(kwargs.get("input"))
        if len(self.inputs) == 1:
            output = [
                FakeFunctionCall(
                    "db_get_schema",
                    '{"database":"commerce","reason":"find order columns"}',
                )
            ]
        else:
            output = [
                FakeFunctionCall(
                    "db_query_readonly",
                    (
                        '{"database":"commerce","sql":"SELECT COUNT(*) AS count FROM orders",'
                        '"reason":"count orders"}'
                    ),
                )
            ]
        return type("Response", (), {"output": output})()


class FakeDatabaseMcp:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_tools(self) -> list[object]:
        return []

    def openai_tool_schemas(self) -> list[dict[str, object]]:
        return []

    def call_tool(self, _request: OperationRequest, call: object) -> ToolResult:
        name = str(getattr(call, "name"))
        self.calls.append(name)
        if name == "db_get_schema":
            return ToolResult(
                True,
                "schema",
                {
                    "database": "commerce",
                    "schema": {"orders": [{"name": "id", "type": "bigint"}]},
                    "untrusted_data": True,
                },
            )
        return ToolResult(
            True,
            "query",
            {"slack_table": "table", "row_count": 1},
        )


def test_orchestrator_runs_schema_then_one_query_with_untrusted_context() -> None:
    mcp = FakeDatabaseMcp()
    orchestrator = AgentOrchestrator(Settings(openai_api_key="synthetic-key"), mcp)
    responses = SequencedDatabaseResponses()
    orchestrator.client = type("Client", (), {"responses": responses})()

    result = orchestrator.handle(request_for(Role.ADMIN))

    assert result.ok
    assert mcp.calls == ["db_get_schema", "db_query_readonly"]
    assert len(responses.inputs) == 2
    assert "UNTRUSTED DATABASE SCHEMA METADATA" in str(responses.inputs[1])


class DuplicateDatabaseResponses:
    def create(self, **_kwargs: object) -> object:
        arguments = '{"database":"commerce","sql":"SELECT 1","reason":"test"}'
        return type(
            "Response",
            (),
            {
                "output": [
                    FakeFunctionCall("db_query_readonly", arguments),
                    FakeFunctionCall("db_query_readonly", arguments),
                ]
            },
        )()


class NeverCalledMcp(FakeDatabaseMcp):
    def call_tool(self, _request: OperationRequest, _call: object) -> ToolResult:
        raise AssertionError("multiple queries must be rejected before execution")


def test_orchestrator_rejects_multiple_database_queries() -> None:
    orchestrator = AgentOrchestrator(
        Settings(openai_api_key="synthetic-key"),
        NeverCalledMcp(),
    )
    orchestrator.client = type("Client", (), {"responses": DuplicateDatabaseResponses()})()
    result = orchestrator.handle(request_for(Role.ADMIN))
    assert not result.ok
    assert "multiple database queries" in result.message
