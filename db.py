import os
import sys
import json
import argparse

import psycopg2
from psycopg2 import Error
from psycopg2 import sql
from psycopg2.extras import RealDictCursor


def _get_conn_params() -> dict:
    """
    Supports standard Postgres env vars:
      - PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
    Falls back to the previous hardcoded values for convenience.
    """
    host = os.getenv("PGHOST", "185.124.108.137")
    port = int(os.getenv("PGPORT", "5432"))
    user = os.getenv("PGUSER", "ai_user")
    password = os.getenv("PGPASSWORD", "STRONG_PASSWORD_2026")
    database = os.getenv("PGDATABASE", "bww_v1")

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
    }


def _configure_utf8_output() -> None:
    # Avoid UnicodeEncodeError on some Windows terminals/codepages.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")


def _shorten(value, max_len: int = 200):
    if value is None:
        return None
    s = str(value)
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def _print_table_preview_rows(rows: list[dict], max_value_len: int) -> None:
    if not rows:
        print("(لا يوجد صفوف)")
        return
    for i, row in enumerate(rows, start=1):
        cleaned = {k: _shorten(v, max_value_len) for k, v in row.items()}
        print(f"[{i}] {json.dumps(cleaned, ensure_ascii=False, default=str)}")


def _get_table_columns(cursor, schema: str, table: str) -> list[tuple[str, str]]:
    """
    Returns list of (column_name, data_type).
    Tries information_schema first; falls back to SELECT * LIMIT 0 if needed.
    """
    try:
        cursor.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position;
            """,
            (schema, table),
        )
        cols = cursor.fetchall()
        if cols:
            return [(c[0], c[1]) for c in cols]
    except Exception:
        pass

    cursor.execute(
        sql.SQL("SELECT * FROM {}.{} LIMIT 0").format(sql.Identifier(schema), sql.Identifier(table))
    )
    return [(d.name, "unknown") for d in (cursor.description or [])]


def list_schemas_and_tables() -> None:
    params = _get_conn_params()

    _configure_utf8_output()

    schemas_sql = """
        SELECT nspname
        FROM pg_namespace
        WHERE nspname NOT LIKE 'pg_%'
          AND nspname <> 'information_schema'
        ORDER BY 1;
    """

    # NOTE: information_schema.tables can hide objects if the current role lacks privileges.
    # pg_catalog gives a more complete view of what's in the DB.
    tables_sql = """
        SELECT n.nspname AS table_schema, c.relname AS table_name
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        WHERE c.relkind IN ('r', 'p') -- ordinary tables + partitioned tables
          AND n.nspname NOT IN ('pg_catalog', 'information_schema')
        ORDER BY 1, 2;
    """

    sizes_sql = """
        SELECT
            schemaname,
            relname,
            n_live_tup,
            pg_total_relation_size(quote_ident(schemaname) || '.' || quote_ident(relname)) AS total_bytes
        FROM pg_stat_user_tables
        ORDER BY total_bytes DESC;
    """

    try:
        with psycopg2.connect(**params) as connection:
            connection.autocommit = True
            with connection.cursor() as cursor:
                print(f"متصل بقاعدة البيانات: {params['database']}")

                cursor.execute(schemas_sql)
                schemas = [r[0] for r in cursor.fetchall()]
                print("\nSchemas:")
                for s in schemas:
                    print(f"- {s}")

                cursor.execute(tables_sql)
                tables = cursor.fetchall()
                print(f"\nTables ({len(tables)}):")
                for schema, table in tables:
                    print(f"- {schema}.{table}")

                # اختياري: ترتيب الجداول حسب الحجم (لو عندك صلاحيات)
                try:
                    cursor.execute(sizes_sql)
                    rows = cursor.fetchall()
                    print("\nLargest tables (by total size):")
                    for schemaname, relname, n_live_tup, total_bytes in rows[:30]:
                        print(f"- {schemaname}.{relname} | rows~{n_live_tup} | bytes={total_bytes}")
                except Exception as e:
                    print("\n(ملاحظة) ماقدرتش أجيب أحجام الجداول:", e)

        print("\nنجح!")
    except Error as e:
        print("خطأ:", e)


def preview_tables(
    tables: list[str],
    default_schema: str = "public",
    limit: int = 5,
    max_value_len: int = 200,
) -> None:
    params = _get_conn_params()
    _configure_utf8_output()

    try:
        with psycopg2.connect(**params) as connection:
            connection.autocommit = True
            print(f"متصل بقاعدة البيانات: {params['database']}")

            for t in tables:
                t = t.strip()
                if not t:
                    continue

                if "." in t:
                    schema, table = t.split(".", 1)
                else:
                    schema, table = default_schema, t

                print("\n" + "=" * 80)
                print(f"Preview: {schema}.{table} (LIMIT {limit})")

                # Check privileges first (helps explain permission errors)
                try:
                    with connection.cursor() as cursor:
                        cursor.execute(
                            "SELECT has_table_privilege(current_user, %s, 'SELECT');",
                            (f"{schema}.{table}",),
                        )
                        can_select = cursor.fetchone()[0]
                        print(f"SELECT privilege: {'YES' if can_select else 'NO'}")
                except Exception as e:
                    print("تعذر التحقق من الصلاحيات:", e)

                try:
                    with connection.cursor() as cursor:
                        cols = _get_table_columns(cursor, schema, table)
                        if cols:
                            print("Columns:")
                            for name, dtype in cols:
                                print(f"- {name} ({dtype})")
                except Exception as e:
                    print("تعذر جلب الأعمدة:", e)
                    try:
                        connection.rollback()
                    except Exception:
                        pass

                try:
                    with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                        q = sql.SQL("SELECT * FROM {}.{} LIMIT %s").format(
                            sql.Identifier(schema), sql.Identifier(table)
                        )
                        cursor.execute(q, (limit,))
                        rows = cursor.fetchall()
                        print("\nRows:")
                        _print_table_preview_rows(rows, max_value_len=max_value_len)
                except Exception as e:
                    print("تعذر جلب الصفوف:", e)
                    try:
                        connection.rollback()
                    except Exception:
                        pass

            print("\nنجح!")
    except Error as e:
        print("خطأ:", e)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Postgres DB helper (list/preview tables).")
    parser.add_argument("--list", action="store_true", help="List schemas and tables")
    parser.add_argument(
        "--preview",
        type=str,
        help="Comma-separated table names (optionally schema.table), e.g. products,orders or public.products",
    )
    parser.add_argument("--schema", type=str, default=os.getenv("PREVIEW_SCHEMA", "public"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("PREVIEW_LIMIT", "5")))
    parser.add_argument("--max-len", type=int, default=int(os.getenv("PREVIEW_MAX_LEN", "200")))
    args = parser.parse_args()

    if not args.list and not args.preview:
        args.list = True

    if args.list:
        list_schemas_and_tables()

    if args.preview:
        preview_tables(
            tables=[t.strip() for t in args.preview.split(",") if t.strip()],
            default_schema=args.schema,
            limit=args.limit,
            max_value_len=args.max_len,
        )
