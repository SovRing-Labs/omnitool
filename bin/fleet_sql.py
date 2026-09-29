#!/usr/bin/env python3
"""
fleet-sql — Custom Database Tool Middleware & Token Regulator
Provides intelligent SQLite tool middleware with:
- LIMIT 5 clamping for SQL SELECT statements
- Schema caching with 5-minute TTL
- Token compression/decompression utilities
- Token rate limiting and tracking
- Configurable TTL for schema cache
"""

import argparse
import os
import re
import sqlite3
import subprocess
import sys
import json
import gzip
import base64
import hashlib
import time
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

# Schema cache with TTL (5 minutes)
_SCHEMA_CACHE: Dict[str, Tuple[float, Dict]] = {}
_SCHEMA_TTL = 300  # 5 minutes

def configure_schema_ttl(minutes: int) -> None:
    """Configure the schema cache TTL in minutes."""
    global _SCHEMA_TTL
    _SCHEMA_TTL = minutes * 60

# Token rate limiting and tracking
_TOKEN_USAGE_LOG: List[Dict[str, Any]] = []
_TOKEN_USAGE_LOCK = threading.RLock()
_TOKEN_LIMIT_PER_MINUTE = 1000
_TOKEN_LIMIT_PER_HOUR = 10000

# Token compression utilities
def compress_tokens(data: Any) -> str:
    """
    Compress token output for efficient LLM consumption.
    Uses JSON serialization + gzip compression + base64 encoding.
    """
    try:
        # Serialize to JSON
        json_str = json.dumps(data, separators=(',', ':'))  # Compact JSON
        # Compress with gzip
        compressed = gzip.compress(json_str.encode('utf-8'))
        # Encode as base64 for safe transport
        return base64.b64encode(compressed).decode('ascii')
    except Exception:
        # Fallback to plain JSON if compression fails
        return json.dumps(data)


def decompress_tokens(compressed_data: str) -> Any:
    """
    Decompress token output that was compressed with compress_tokens.
    """
    try:
        # Decode base64
        compressed = base64.b64decode(compressed_data.encode('ascii'))
        # Decompress with gzip
        json_str = gzip.decompress(compressed).decode('utf-8')
        # Parse JSON
        return json.loads(json_str)
    except Exception:
        # If decompression fails, try to parse as plain JSON
        return json.loads(compressed_data)


def get_schema_info(db_path: Path) -> Dict[str, Any]:
    """Get and cache schema information for a database."""
    cache_key = str(db_path.absolute())
    now = time.time()
    
    # Check cache
    if cache_key in _SCHEMA_CACHE:
        timestamp, schema_data = _SCHEMA_CACHE[cache_key]
        if now - timestamp < _SCHEMA_TTL:
            return schema_data
    
    # Fetch fresh schema
    if not db_path.exists():
        return {"error": f"Database not found: {db_path}"}
    
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Get table list
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row["name"] for row in cursor.fetchall()]
        
        schema_data = {
            "tables": {},
            "timestamp": now
        }
        
        # Get schema for each table
        for table in tables:
            cursor.execute(f"PRAGMA table_info({table})")
            columns = [dict(row) for row in cursor.fetchall()]
            
            # Get row count
            cursor.execute(f"SELECT COUNT(*) as cnt FROM {table}")
            row_count = cursor.fetchone()["cnt"]
            
            schema_data["tables"][table] = {
                "columns": columns,
                "row_count": row_count
            }
        
        conn.close()
        
        # Cache the result
        _SCHEMA_CACHE[cache_key] = (now, schema_data)
        return schema_data
        
    except Exception as e:
        return {"error": f"Failed to read schema: {str(e)}"}


EXPLICIT_LIMIT_MAX = 200


def clamp_limit(sql: str) -> str:
    """
    Automatically add LIMIT 5 to SELECT statements that don't have a LIMIT clause.
    Also ensures LIMIT is not greater than 5.
    """
    sql_upper = sql.upper().strip()
    
    # Only apply to SELECT statements
    if not sql_upper.startswith("SELECT"):
        return sql
    
    # Check if LIMIT already exists
    if " LIMIT " in sql_upper:
        # Extract the LIMIT value and clamp it to 5
        import re
        # Match LIMIT followed by optional whitespace and digits
        limit_match = re.search(r'LIMIT\s+(\d+)', sql_upper)
        if limit_match:
            # 2026-09-26 dogfood: an EXPLICIT limit is intent -- honour it up to a ceiling
            # (was: any explicit LIMIT >5 rewritten to 5, so `LIMIT 10` returned 5 rows silently).
            # The default for an unbounded SELECT stays LIMIT 5 (token protection).
            limit_val = int(limit_match.group(1))
            if limit_val > EXPLICIT_LIMIT_MAX:
                sql = re.sub(r'LIMIT\s+\d+', f' LIMIT {EXPLICIT_LIMIT_MAX}', sql, flags=re.IGNORECASE)
        return sql
    else:
        # No LIMIT clause, add LIMIT 5
        # Remove any trailing semicolon for clean appending
        sql = sql.rstrip(';')
        return sql + " LIMIT 5"


def strip_limit_hint(sql: str) -> str:
    """Remove LIMIT suffix so we can return count without fetching rows."""
    import re
    cleaned = re.sub(r'\s*LIMIT\s+\d+\s*;?\s*$', '', sql, flags=re.IGNORECASE).rstrip(';').strip()
    return cleaned


def _suggest_columns_for_error(table_name: str, db_path: Path, err_msg: str) -> Optional[str]:
    """If query failed due to bad column name, suggest valid columns from schema."""
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [row[0] for row in cur.fetchall()]
        if not table_name or table_name.lower() == '"column"':
            return None
        tname = table_name.strip('"').strip("'").lower()
        matched = [t for t in tables if t.lower() == tname or tname in t.lower()]
        if not matched:
            return None
        tbl = matched[0]
        cur.execute(f"PRAGMA table_info({tbl})")
        cols = [r[1] for r in cur.fetchall()]
        return f"Table '{tbl}' has columns: {', '.join(cols)}"
    except Exception:
        return None


def execute_query(db_path: Path, sql: str, params: Tuple = (), 
                  clamp: bool = True, compress: bool = False) -> Dict[str, Any]:
    start_time = time.time()
    
    executed_sql = clamp_limit(sql) if clamp else sql
    
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        if params:
            cursor.execute(executed_sql, params)
        else:
            cursor.execute(executed_sql)
        
        rows = cursor.fetchall()
        result = [dict(row) for row in rows]
        
        conn.close()
        
        execution_time = (time.time() - start_time) * 1000
        
        response = {
            "sql": executed_sql,
            "rows": result,
            "row_count": len(result),
            "execution_time_ms": round(execution_time, 2),
            "clamped": clamp and executed_sql != sql,
            "compressed": compress
        }
        
        if compress:
            response["rows"] = compress_tokens(result)
            
        return response
        
    except Exception as e:
        result = {
            "error": f"Query execution failed: {str(e)}",
            "sql": executed_sql,
            "execution_time_ms": round((time.time() - start_time) * 1000, 2)
        }
        
        err_text = str(e).lower()
        if ("no such column" in err_text or "ambiguous column name" in err_text):
            import re as _re
            table_match = _re.search(r'(?:from|join)\\s+[\\x27\\x22]?(\\w+)[\\x27\\x22]?', executed_sql, _re.IGNORECASE)
            if table_match:
                suggestion = _suggest_columns_for_error(table_match.group(1), db_path, err_text)
                if suggestion:
                    result["suggestion"] = suggestion
        elif "no such table" in err_text:
            try:
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                cur = conn.cursor()
                cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
                tables = [r[0] for r in cur.fetchall()]
                conn.close()
                if tables:
                    result["available_tables"] = tables
            except Exception:
                pass
        elif "not a database" in err_text or "database is locked" in err_text:
            result["hint"] = "File may be corrupted or busy — retry after a brief delay."
        
        return result


# Release 0.1.0: alias directory is configurable (was a fixed fleet path). Default = <tool root>/control-center.
_DB_DIR = Path(os.environ.get("OMNITOOL_DB_DIR") or Path(__file__).resolve().parent.parent / "control-center").expanduser()
DB_ALIASES = {
    "dispatch": _DB_DIR / "dispatch.db",
    "tasks": _DB_DIR / "dispatch.db",
    "lanes": _DB_DIR / "lane-catalog.db",
    "blackboard": _DB_DIR / "blackboard.db",
    "roadmap": _DB_DIR / "roadmap.db",
    "phases": _DB_DIR / "swarm-phases.db",
    "genr": _DB_DIR / "genr.db",
    "plans": _DB_DIR / "flight-plans.db",
    "vector": _DB_DIR / "system-docs.db",
    "system_docs": _DB_DIR / "system-docs.db",
}


def _resolve_db(db_str: str) -> Path:
    """2026-09-26 dogfood: only `omni` honoured DB_ALIASES; `query dispatch ...` failed with
    'unable to open database file'. Every subcommand now resolves aliases the same way."""
    return DB_ALIASES[db_str] if db_str in DB_ALIASES else Path(db_str).expanduser()


def execute_omni_batch(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a structured batch of SQL statements inside a single transaction.
    Supports db_exec specification from Omnitool Stigmergy design.
    Payload schema:
    {
      "database": "dispatch" | "lanes" | "vector" | "path/to/db.sqlite",
      "mode": "read_write" | "read_only" (default: "read_write"),
      "transaction": true | false (default: true),
      "clamp_limit": true | false (default: true),
      "count_only": true | false (default: false) — if True, all SELECT ops return row count only
      "operations" / "queries": [
        {"sql": "...", "params": [...]},
        ...
      ]
    }
    Per-op override: {"sql": "...", "params": [...], "_count_only": true}
    """
    start_time = time.time()
    db_str = payload.get("database")
    if not db_str:
        return {"ok": False, "status": "error", "error": "'database' field required in payload", "duration_ms": 0}
    
    if db_str in DB_ALIASES:
        db_path = DB_ALIASES[db_str]
    else:
        db_path = Path(db_str).expanduser()

    mode = payload.get("mode", "read_write")
    transaction = payload.get("transaction", True)
    clamp = payload.get("clamp_limit", True)
    global_count_only = payload.get("count_only", False)
    operations = payload.get("operations") or payload.get("queries") or []
    
    if not isinstance(operations, list):
        return {"ok": False, "status": "error", "error": "'operations' or 'queries' must be an array", "duration_ms": 0}
    
    conn = None
    idx = -1
    try:
        if mode == "read_only":
            if not db_path.exists():
                return {"ok": False, "error": f"Database not found: {db_path}", "duration_ms": 0}
            conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
        else:
            db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(db_path.resolve()), timeout=10.0)
            conn.execute("PRAGMA busy_timeout = 5000")
            
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Begin transaction if write mode and requested
        if transaction and mode == "read_write":
            cur.execute("BEGIN IMMEDIATE")
            
        results = []
        for idx, op in enumerate(operations):
            if isinstance(op, str):
                sql = op.strip()
                params = ()
                op_count_only = global_count_only
            elif isinstance(op, dict):
                sql = op.get("sql", "").strip()
                raw_params = op.get("params", ())
                if isinstance(raw_params, list):
                    params = tuple(raw_params)
                elif raw_params is not None and not isinstance(raw_params, tuple):
                    params = (raw_params,)
                else:
                    params = raw_params or ()
                op_count_only = global_count_only or op.get("_count_only", False)
            else:
                raise ValueError(f"Operation at index {idx} must be a string or dict with 'sql'")
                
            if not sql:
                continue
                
            sql_upper = sql.upper().strip()
            is_select = sql_upper.startswith("SELECT") or sql_upper.startswith("PRAGMA") or sql_upper.startswith("EXPLAIN")
            # CTE-READ (dogfood 2026-09-27): a read-only WITH ... SELECT was refused as mutating. A WITH can wrap
            # an INSERT/UPDATE, so in read_only mode it runs under PRAGMA query_only=ON: SQLite itself refuses writes.
            cte_guard = sql_upper.startswith("WITH") and mode == "read_only"
            is_select = is_select or cte_guard

            if is_select:
                exec_sql = clamp_limit(sql) if (clamp and sql_upper.startswith("SELECT")) else sql
                if cte_guard:
                    cur.execute("PRAGMA query_only=ON")
                    try:
                        cur.execute(exec_sql, params)
                        rows = [dict(r) for r in cur.fetchall()]
                    finally:
                        cur.execute("PRAGMA query_only=OFF")
                else:
                    cur.execute(exec_sql, params)
                    rows = [dict(r) for r in cur.fetchall()]
                result = {
                    "step": idx,
                    "type": "select",
                    "count": len(rows),
                    "clamped": clamp and exec_sql != sql
                }
                if op_count_only:
                    # Count-only: skip rows entirely — saves tokens when model just needs cardinality
                    pass
                else:
                    result["rows"] = rows
                results.append(result)
            else:
                if mode == "read_only":
                    raise PermissionError(f"Cannot execute mutating SQL in read_only mode at step {idx}")
                cur.execute(sql, params)
                results.append({
                    "step": idx,
                    "type": "mutation",
                    "rows_affected": cur.rowcount,
                })
                
        if transaction and mode == "read_write":
            conn.commit()
            
        conn.close()

        # Trigger state anchor compilation if any mutations occurred
        if mode == "read_write" and any(r.get("type") == "mutation" for r in results):
            try:
                # Release 0.1.0: optional post-mutation hook (was a fixed fleet script). Unset = no hook.
                _hook = os.environ.get("OMNITOOL_SQL_WRITE_HOOK")
                if _hook:
                    subprocess.Popen([_hook], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

        duration_ms = (time.time() - start_time) * 1000
        return {
            "ok": True,
            "executed": len(results),
            "duration_ms": round(duration_ms, 2),
            "results": results
        }
        
    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        if conn and transaction and mode == "read_write":
            try:
                conn.rollback()
            except Exception:
                pass
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        return {
            "ok": False,
            "error": str(e),
            "failed_step": idx,
            "rolled_back": bool(transaction and mode == "read_write"),
            "duration_ms": round(duration_ms, 2)
        }


def main():
    if len(sys.argv) > 0 and (
        sys.argv[0].endswith("omni-sql") or 
        sys.argv[0].endswith("fleet-omni-sql") or
        sys.argv[0].endswith("db_exec") or
        sys.argv[0].endswith("db-exec")
    ):
        if len(sys.argv) == 1 or sys.argv[1] not in ("query", "batch", "schema", "token", "config", "omni", "-h", "--help"):
            sys.argv.insert(1, "omni")

    # 2026-09-26 dogfood: bare `fleet-sql "SELECT ..."` died on argparse. Bare SQL -> `query dispatch <sql>`
    # (the board is the default db); `fleet-sql <alias|path.db> "SELECT ..."` -> `query <db> <sql>`.
    _subs = ("query", "batch", "schema", "token", "config", "omni", "-h", "--help")
    _sql_re = r"\s*(select|with|pragma|insert|update|delete|replace|explain)\b"
    if len(sys.argv) >= 2 and sys.argv[1] not in _subs:
        if re.match(_sql_re, sys.argv[1], re.I):
            sys.argv[1:1] = ["query", "dispatch"]
        elif len(sys.argv) >= 3 and re.match(_sql_re, sys.argv[2], re.I):
            sys.argv.insert(1, "query")

    parser = argparse.ArgumentParser(
        prog="fleet-sql",
        description="Intelligent SQLite Tool Middleware with LIMIT 5 clamping, schema caching, token compression, rate limiting and TTL configuration"
    )
    
    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    
    config_parser = subparsers.add_parser("config", help="Configuration and status commands")
    config_parser.add_argument("schema_ttl_minutes", nargs="?", type=int, help="Configure schema cache TTL in minutes (optional)")
    config_parser.add_argument("--status", action="store_true", help="Show system status and configuration")
    
    # Query command
    query_parser = subparsers.add_parser("query", help="Execute a SQL query")
    query_parser.add_argument("database", help="Path to SQLite database file")
    query_parser.add_argument("sql", help="SQL query to execute")
    query_parser.add_argument(
        "--params", 
        help="JSON-encoded parameters for the query",
        default="[]"
    )
    query_parser.add_argument(
        "--no-clamp",
        action="store_true",
        help="Disable automatic LIMIT 5 clamping"
    )
    query_parser.add_argument(
        "--compress",
        action="store_true",
        help="Compress output tokens using gzip+base64"
    )
    query_parser.add_argument(
        "--output",
        choices=["json", "pretty", "csv"],
        default="json",
        help="Output format"
    )
    
    # Batch query command
    batch_parser = subparsers.add_parser("batch", help="Execute multiple SQL queries in one batch")
    batch_parser.add_argument("database", help="Path to SQLite database file")
    batch_parser.add_argument("queries", nargs="?", default="", help="Semicolon-separated SQL queries (or read from --file)")
    batch_parser.add_argument("--file", help="Path to SQL file with queries")
    batch_parser.add_argument("--compress", action="store_true", help="Compress output tokens using gzip+base64")
    batch_parser.add_argument("--no-clamp", action="store_true", help="Disable automatic LIMIT 5 clamping")
    batch_parser.add_argument("--json", action="store_true", help="Output / input as Omni-SQL JSON")

    # Omni-Tool SQL command (structured transactional JSON)
    omni_parser = subparsers.add_parser("omni", help="Execute Omni-Tool SQL batch (JSON schema)")
    omni_parser.add_argument("payload", nargs="?", default="", help="JSON payload string, path to JSON file, or '-' for stdin")
    omni_parser.add_argument("--file", help="Path to JSON file with batch payload")
    omni_parser.add_argument("--database", help="Override database path in payload")
    omni_parser.add_argument("--mode", choices=["read_only", "read_write"], help="Override mode in payload")
    omni_parser.add_argument("--output", choices=["json", "pretty"], default="json", help="Output format")

    # Schema command
    schema_parser = subparsers.add_parser("schema", help="Get schema information")
    schema_parser.add_argument("database", help="Path to SQLite database file")
    schema_parser.add_argument(
        "--compress",
        action="store_true",
        help="Compress output tokens using gzip+base64"
    )
    schema_parser.add_argument(
        "--output",
        choices=["json", "pretty"],
        default="json",
        help="Output format"
    )
    
    # Schema caching directory
    SCHEMA_CACHE_DIR = Path(".cache/db-schemas")
    
    # Token compression/decompression commands
    token_parser = subparsers.add_parser("token", help="Token compression utilities")
    token_sub = token_parser.add_subparsers(dest="token_command")
    
    compress_parser = token_sub.add_parser("compress", help="Compress JSON data")
    compress_parser.add_argument("input_file", help="Input JSON file (or '-' for stdin)")
    
    decompress_parser = token_sub.add_parser("decompress", help="Decompress token data")
    decompress_parser.add_argument("input_file", help="Input compressed file (or '-' for stdin)")
    
    args = parser.parse_args()
    
    if args.command == "query":
        db_path = _resolve_db(args.database)
        try:
            params = json.loads(args.params)
            if not isinstance(params, list):
                params = [params]
        except json.JSONDecodeError:
            params = [args.params] if args.params else []
        
        result = execute_query(
            db_path=db_path,
            sql=args.sql,
            params=tuple(params),
            clamp=not args.no_clamp,
            compress=args.compress
        )
        
        if args.output == "pretty":
            print(json.dumps(result, indent=2))
        elif args.output == "csv" and "rows" in result and isinstance(result["rows"], list):
            if result["rows"]:
                # Print CSV header
                headers = list(result["rows"][0].keys())
                print(",".join(headers))
                # Print rows
                for row in result["rows"]:
                    values = [str(row.get(h, "")) for h in headers]
                    print(",".join(values))
            else:
                print("No data")
        else:  # json
            print(json.dumps(result))

    elif args.command == "batch":
        db_path = _resolve_db(args.database)
        raw_sql = args.queries
        if args.file and Path(args.file).exists():
            raw_sql = Path(args.file).read_text(encoding="utf-8")
        
        trimmed = raw_sql.strip()
        # If input is JSON payload or --json passed with JSON text, delegate to execute_omni_batch
        if (args.json or trimmed.startswith("{")) and (trimmed.startswith("{") or trimmed.startswith("[")):
            try:
                parsed = json.loads(trimmed)
                if isinstance(parsed, list):
                    payload = {"database": str(db_path), "operations": parsed}
                else:
                    payload = parsed
                    if "database" not in payload:
                        payload["database"] = str(db_path)
                result = execute_omni_batch(payload)
                print(json.dumps(result, indent=2 if args.json else None))
            except Exception as e:
                print(json.dumps({"ok": False, "error": f"JSON parse error: {e}"}))
        else:
            statements = [s.strip() for s in raw_sql.split(";") if s.strip()]
            batch_results = []
            for s in statements:
                res = execute_query(db_path=db_path, sql=s, clamp=not args.no_clamp, compress=args.compress)
                batch_results.append(res)
            print(json.dumps(batch_results))

    elif args.command == "omni":
        raw_payload = ""
        if args.file and Path(args.file).exists():
            raw_payload = Path(args.file).read_text(encoding="utf-8")
        elif args.payload == "-" or (not args.payload and not sys.stdin.isatty()):
            raw_payload = sys.stdin.read()
        elif args.payload:
            stripped = args.payload.strip()
            if stripped.startswith("{") or stripped.startswith("["):
                raw_payload = args.payload
            elif len(args.payload) < 255 and os.path.isfile(args.payload):
                raw_payload = Path(args.payload).read_text(encoding="utf-8")
            else:
                raw_payload = args.payload
        else:
            print(json.dumps({"ok": False, "error": "Omni-SQL requires a JSON payload (via arg, --file, or stdin)"}))
            sys.exit(1)

        try:
            payload = json.loads(raw_payload)
            if not isinstance(payload, dict):
                if isinstance(payload, list):
                    payload = {"operations": payload}
                else:
                    print(json.dumps({"ok": False, "error": "Omni-SQL payload must be a JSON object or array"}))
                    sys.exit(1)
        except Exception as e:
            print(json.dumps({"ok": False, "error": f"JSON payload parse error: {e}"}))
            sys.exit(1)

        if args.database:
            payload["database"] = args.database
        if args.mode:
            payload["mode"] = args.mode

        result = execute_omni_batch(payload)
        if args.output == "pretty":
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps(result))
            
    elif args.command == "schema":
        db_path = _resolve_db(args.database)
        schema_info = get_schema_info(db_path)
        
        if args.compress:
            schema_info = {"compressed": compress_tokens(schema_info)}
        
        if args.output == "pretty":
            print(json.dumps(schema_info, indent=2))
        else:
            print(json.dumps(schema_info))
            
    elif args.command == "token":
        if args.token_command == "compress":
            input_data = []
            if args.input_file == "-":
                input_data = sys.stdin.read()
            else:
                input_data = Path(args.input_file).read_text()
            
            try:
                data = json.loads(input_data)
                compressed = compress_tokens(data)
                print(compressed)
            except Exception as e:
                print(f"Error compressing tokens: {e}", file=sys.stderr)
                sys.exit(1)
                
        elif args.token_command == "decompress":
            input_data = []
            if args.input_file == "-":
                input_data = sys.stdin.read()
            else:
                input_data = Path(args.input_file).read_text().strip()
            
            try:
                decompressed = decompress_tokens(input_data)
                print(json.dumps(decompressed, indent=2))
            except Exception as e:
                print(f"Error decompressing tokens: {e}", file=sys.stderr)
                sys.exit(1)
        else:
            token_parser.print_help()
    
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
