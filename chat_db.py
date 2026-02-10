import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import psycopg2
from psycopg2.extras import RealDictCursor


def _get_conn_params() -> dict[str, Any]:
    """
    Uses standard Postgres env vars:
      - PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE, optional PGSSLMODE
    """
    host = os.getenv("PGHOST", "185.124.108.137")
    port = int(os.getenv("PGPORT", "5432"))
    user = os.getenv("PGUSER", "ai_user")
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


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _truncate_text(s: str | None, max_len: int) -> str | None:
    if s is None:
        return None
    s = str(s)
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def init_chat_schema() -> None:
    """
    Creates chat persistence/log tables if they don't exist.
    Safe to call on startup.
    """
    ddl = """
    CREATE TABLE IF NOT EXISTS public.chat_conversations (
        id BIGSERIAL PRIMARY KEY,
        user_number TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'open',
        state JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_activity_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE UNIQUE INDEX IF NOT EXISTS chat_conversations_open_user_uq
    ON public.chat_conversations (user_number)
    WHERE status = 'open';

    CREATE TABLE IF NOT EXISTS public.chat_messages (
        id BIGSERIAL PRIMARY KEY,
        conversation_id BIGINT NOT NULL REFERENCES public.chat_conversations(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        direction TEXT NOT NULL,
        text TEXT NOT NULL,
        wa_message_id TEXT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS chat_messages_conv_created_idx
    ON public.chat_messages (conversation_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS chat_messages_wa_message_id_idx
    ON public.chat_messages (wa_message_id)
    WHERE wa_message_id IS NOT NULL;

    CREATE TABLE IF NOT EXISTS public.gemini_calls (
        id BIGSERIAL PRIMARY KEY,
        conversation_id BIGINT NOT NULL REFERENCES public.chat_conversations(id) ON DELETE CASCADE,
        correlation_id UUID NOT NULL,
        model TEXT NOT NULL,
        prompt TEXT NOT NULL,
        response_text TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS gemini_calls_conv_created_idx
    ON public.gemini_calls (conversation_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS gemini_calls_corr_idx
    ON public.gemini_calls (correlation_id);

    CREATE TABLE IF NOT EXISTS public.chat_events (
        id BIGSERIAL PRIMARY KEY,
        conversation_id BIGINT NULL REFERENCES public.chat_conversations(id) ON DELETE SET NULL,
        correlation_id UUID NOT NULL,
        event_type TEXT NOT NULL,
        payload JSONB NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS chat_events_corr_idx
    ON public.chat_events (correlation_id);
    CREATE INDEX IF NOT EXISTS chat_events_conv_created_idx
    ON public.chat_events (conversation_id, created_at DESC);
    """

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(ddl)


def get_or_create_open_conversation(user_number: str, ttl_hours: int = 24) -> int:
    """
    Returns an 'open' conversation id for a user. If the last activity is older than ttl_hours,
    closes it and creates a new one.
    """
    user_number = (user_number or "").strip()
    if not user_number:
        raise ValueError("user_number is required")

    cutoff = _utcnow() - timedelta(hours=ttl_hours)

    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            # Try existing open conversation
            cur.execute(
                """
                SELECT id, last_activity_at
                FROM public.chat_conversations
                WHERE user_number = %s AND status = 'open'
                LIMIT 1;
                """,
                (user_number,),
            )
            row = cur.fetchone()
            if row:
                last_activity_at = row.get("last_activity_at")
                if last_activity_at and last_activity_at < cutoff:
                    cur.execute(
                        "UPDATE public.chat_conversations SET status='closed' WHERE id=%s;",
                        (int(row["id"]),),
                    )
                else:
                    return int(row["id"])

            # Create new
            cur.execute(
                """
                INSERT INTO public.chat_conversations (user_number, status, state, last_activity_at)
                VALUES (%s, 'open', '{}'::jsonb, now())
                RETURNING id;
                """,
                (user_number,),
            )
            new_id = cur.fetchone()["id"]
            return int(new_id)


def touch_conversation(conversation_id: int) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.chat_conversations SET last_activity_at = now() WHERE id = %s;",
                (int(conversation_id),),
            )


def get_conversation_state(conversation_id: int) -> dict[str, Any]:
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                "SELECT state FROM public.chat_conversations WHERE id = %s LIMIT 1;",
                (int(conversation_id),),
            )
            row = cur.fetchone()
            return row["state"] if row and isinstance(row.get("state"), dict) else {}


def set_conversation_state(conversation_id: int, state: dict[str, Any]) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE public.chat_conversations SET state = %s::jsonb, last_activity_at = now() WHERE id = %s;",
                (json.dumps(state or {}, ensure_ascii=False, default=str), int(conversation_id)),
            )


def append_message(
    conversation_id: int,
    *,
    role: str,
    direction: str,
    text: str,
    wa_message_id: str | None = None,
) -> int:
    text = (text or "").strip()
    if not text:
        raise ValueError("text is required")

    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                INSERT INTO public.chat_messages (conversation_id, role, direction, text, wa_message_id)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id;
                """,
                (int(conversation_id), role, direction, text, wa_message_id),
            )
            msg_id = int(cur.fetchone()["id"])
            cur.execute(
                "UPDATE public.chat_conversations SET last_activity_at = now() WHERE id = %s;",
                (int(conversation_id),),
            )
            return msg_id


def wa_message_id_exists(wa_message_id: str | None) -> bool:
    """
    Best-effort dedupe helper for WhatsApp webhook deliveries.
    WhatsApp may retry / deliver duplicates; we can skip processing if we already saw the id.
    """
    wa_message_id = (wa_message_id or "").strip()
    if not wa_message_id:
        return False
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT 1
                FROM public.chat_messages
                WHERE wa_message_id = %s
                LIMIT 1;
                """,
                (wa_message_id,),
            )
            return cur.fetchone() is not None


def get_recent_messages(conversation_id: int, limit: int = 20) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 100))
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT role, direction, text, wa_message_id, created_at
                FROM public.chat_messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (int(conversation_id), limit),
            )
            rows = cur.fetchall() or []
            # Return chronological order
            return list(reversed(rows))


def insert_event(
    *,
    correlation_id: UUID,
    event_type: str,
    payload: dict[str, Any],
    conversation_id: int | None = None,
    max_field_len: int = 2000,
) -> None:
    # Truncate long strings inside payload (best-effort)
    def _tr(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (int, float, bool)):
            return v
        if isinstance(v, dict):
            return {k: _tr(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_tr(x) for x in v[:200]]
        return _truncate_text(str(v), max_field_len)

    safe_payload = _tr(payload or {})

    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.chat_events (conversation_id, correlation_id, event_type, payload)
                VALUES (%s, %s, %s, %s::jsonb);
                """,
                (
                    int(conversation_id) if conversation_id is not None else None,
                    str(correlation_id),
                    event_type,
                    json.dumps(safe_payload, ensure_ascii=False, default=str),
                ),
            )


def insert_gemini_call(
    *,
    conversation_id: int,
    correlation_id: UUID,
    model: str,
    prompt: str,
    response_text: str,
    max_len: int = 30000,
) -> None:
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO public.gemini_calls (conversation_id, correlation_id, model, prompt, response_text)
                VALUES (%s, %s, %s, %s, %s);
                """,
                (
                    int(conversation_id),
                    str(correlation_id),
                    model,
                    _truncate_text(prompt, max_len) or "",
                    _truncate_text(response_text, max_len) or "",
                ),
            )


def get_open_conversation_for_user(user_number: str) -> dict[str, Any] | None:
    user_number = (user_number or "").strip()
    if not user_number:
        return None
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, user_number, status, state, last_activity_at, created_at
                FROM public.chat_conversations
                WHERE user_number = %s AND status = 'open'
                ORDER BY id DESC
                LIMIT 1;
                """,
                (user_number,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_messages_for_conversation(conversation_id: int, limit: int = 50) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, role, direction, text, wa_message_id, created_at
                FROM public.chat_messages
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                LIMIT %s;
                """,
                (int(conversation_id), limit),
            )
            rows = cur.fetchall() or []
            return list(reversed(rows))


def get_last_gemini_call_for_conversation(conversation_id: int) -> dict[str, Any] | None:
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, correlation_id, model, prompt, response_text, created_at
                FROM public.gemini_calls
                WHERE conversation_id = %s
                ORDER BY created_at DESC
                LIMIT 1;
                """,
                (int(conversation_id),),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_events_by_correlation_id(correlation_id: UUID, limit: int = 500) -> list[dict[str, Any]]:
    limit = max(1, min(int(limit), 1000))
    with _connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(
                """
                SELECT id, conversation_id, correlation_id, event_type, payload, created_at
                FROM public.chat_events
                WHERE correlation_id = %s
                ORDER BY id ASC
                LIMIT %s;
                """,
                (str(correlation_id), limit),
            )
            rows = cur.fetchall() or []
            return [dict(r) for r in rows]


