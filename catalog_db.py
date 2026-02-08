import os
from typing import Any

import psycopg2
from psycopg2.extras import RealDictCursor


def _get_conn_params() -> dict[str, Any]:
    """
    Uses standard Postgres env vars:
      - PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE
    """
    host = os.getenv("PGHOST", "185.124.108.137")
    port = int(os.getenv("PGPORT", "5432"))
    user = os.getenv("PGUSER", "ai_user")
    # Prefer env var; fallback matches existing db.py for local convenience.
    password = os.getenv("PGPASSWORD", "STRONG_PASSWORD_2026")
    database = os.getenv("PGDATABASE", "bww_v1")
    sslmode = os.getenv("PGSSLMODE")

    params: dict[str, Any] = {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
    }
    if sslmode:
        params["sslmode"] = sslmode
    return params


def _connect():
    conn = psycopg2.connect(**_get_conn_params())
    conn.autocommit = True
    return conn


def _safe_str(v: Any, max_len: int = 800) -> Any:
    if v is None:
        return None
    if isinstance(v, (int, float, bool)):
        return v
    s = str(v)
    if len(s) > max_len:
        return s[: max_len - 1] + "…"
    return s


def search_products(user_text: str, limit: int = 3) -> list[dict[str, Any]]:
    """
    Lightweight search used to decide if a message is about products.
    Returns small candidate rows safe to show to the user/Gemini.
    """
    q = (user_text or "").strip()
    if not q:
        return []

    like = f"%{q}%"

    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # If user sends a numeric id, try direct lookup first.
            if q.isdigit():
                cur.execute(
                    """
                    SELECT
                        id,
                        slug,
                        sku,
                        product_code,
                        COALESCE(name->>'ar', name->>'en') AS display_name,
                        consumer_price,
                        stock_quantity,
                        main_image
                    FROM public.products
                    WHERE id = %s
                      AND deleted_at IS NULL
                    LIMIT 1;
                    """,
                    (int(q),),
                )
                row = cur.fetchone()
                if row:
                    return [_clean_row(row)]

            cur.execute(
                """
                SELECT
                    id,
                    slug,
                    sku,
                    product_code,
                    COALESCE(name->>'ar', name->>'en') AS display_name,
                    consumer_price,
                    stock_quantity,
                    main_image
                FROM public.products
                WHERE deleted_at IS NULL
                  AND (
                    (name->>'ar') ILIKE %s
                    OR (name->>'en') ILIKE %s
                    OR slug ILIKE %s
                    OR sku ILIKE %s
                    OR product_code ILIKE %s
                  )
                ORDER BY id DESC
                LIMIT %s;
                """,
                (like, like, like, like, like, limit),
            )
            rows = cur.fetchall() or []
            return [_clean_row(r) for r in rows]


def get_product_details(product_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    vendor_id,
                    name,
                    slug,
                    short_description,
                    consumer_price,
                    stock_quantity,
                    main_image,
                    is_published,
                    is_approved
                FROM public.products
                WHERE id = %s
                  AND deleted_at IS NULL
                LIMIT 1;
                """,
                (product_id,),
            )
            row = cur.fetchone()
            return _clean_row(row) if row else None


def get_product_images(product_id: int, limit: int = 10) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, image, color_id
                FROM public.product_images
                WHERE product_id = %s
                ORDER BY id
                LIMIT %s;
                """,
                (product_id, limit),
            )
            rows = cur.fetchall() or []
            return [_clean_row(r) for r in rows]


def get_product_variants(product_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    id,
                    product_id,
                    price,
                    wholesale_price,
                    half_wholesale_price,
                    stock_quantity,
                    sku_code,
                    color_id,
                    size_id
                FROM public.product_variants
                WHERE product_id = %s
                  AND deleted_at IS NULL
                ORDER BY id
                LIMIT %s;
                """,
                (product_id, limit),
            )
            rows = cur.fetchall() or []
            return [_clean_row(r) for r in rows]


def get_product_context(product_id: int) -> dict[str, Any] | None:
    product = get_product_details(product_id)
    if not product:
        return None
    images = get_product_images(product_id)
    variants = get_product_variants(product_id)
    return {
        "product": product,
        "images": images,
        "variants": variants,
    }


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    # Keep JSONB dicts as-is, shorten very long strings (e.g., HTML)
    cleaned: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, dict):
            cleaned[k] = v
        else:
            cleaned[k] = _safe_str(v)
    return cleaned


