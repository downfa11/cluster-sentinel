# Production database read tools

Cluster Sentinel exposes two MCP tools when `SENTINEL_DB_READ_ENABLED=true`:

- `db_get_schema(database, reason)` returns non-sensitive table and column metadata;
- `db_query_readonly(database, sql, reason, limit=100)` executes one safe read.

`database` is restricted to `commerce` or `wargame`. Either a Slack identity resolved as
`admin`/`operator`, or any member asking in the single configured private onboarding channel, may
call these read-only tools. Channel membership does not assign a role. Unknown Slack users remain
fail-closed outside that channel; `dev` and `gui-user` identities remain denied there. Deployment,
access-change, and other PR-writing tools never inherit channel access.

Example Slack questions:

- `commerce에서 오늘 생성된 주문 수 보여줘`
- `wargame에서 최근 완료된 매치 20개 보여줘`

The LLM may request schema metadata when needed and then make one query call. Schema metadata,
database values, and query results are untrusted data and never become system instructions.
Ambiguous questions and requests to change data must not select a database tool.

## SQL boundary

Every user-supplied query is parsed with sqlglot's MySQL AST parser immediately before execution.
The validator requires exactly one `SELECT`, including a `WITH ... SELECT`, and rejects:

- `INSERT`, `UPDATE`, `DELETE`, `REPLACE`, `MERGE`, and every DDL statement;
- `CALL`, `SET`, transactions, locks, grants, revokes, and multiple statements;
- `information_schema`, `mysql`, `performance_schema`, and `sys` references;
- cross-database or catalog-qualified references;
- `INTO OUTFILE`, `LOAD_FILE`, `SLEEP`, `BENCHMARK`, and system variables;
- credential-like columns, including aliased references.

A missing `LIMIT` becomes 100. The caller may request 1 through 200 rows, and an existing larger
limit is reduced. The client uses the fixed Router endpoint
`home-mysql.mysql-prod.svc.cluster.local:6447`, fixed credential environment-variable names,
five-second connect/read/write and overall timeouts, autocommit, and no multi-statement client flag.

## Output and audit

Slack output is an ASCII table limited to 50 rows and a 64 KiB message budget. Credential-like
result columns are masked as a defense in depth, and truncation reports returned and displayed
row counts. Database errors expose only a sanitized exception category.

Database audit events contain actor identity, database, duration, returned row count, and a
SHA-256 hash of normalized SQL. They never contain SQL text, result rows, credentials, DSNs,
hostnames, usernames, or query reasons.

## Configuration

Target metadata is passwordless JSON. Credential values must arrive only through the named
environment variables:

```text
SENTINEL_DB_READ_ENABLED=true
SENTINEL_DB_READ_TARGETS={"commerce":{"database":"commerce","host":"home-mysql.mysql-prod.svc.cluster.local","port":"6447","username_env":"SENTINEL_COMMERCE_DB_USERNAME","password_env":"SENTINEL_COMMERCE_DB_PASSWORD"},"wargame":{"database":"wargame","host":"home-mysql.mysql-prod.svc.cluster.local","port":"6447","username_env":"SENTINEL_WARGAME_DB_USERNAME","password_env":"SENTINEL_WARGAME_DB_PASSWORD"}}
```

Set `SENTINEL_DB_READ_ENABLED=false` to remove both tools without disabling Sentinel's Slack,
GitOps, Argo CD, or Grafana features. Never place credential values in this metadata.
