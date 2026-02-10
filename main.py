import os
import re
from uuid import uuid4, UUID

from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import PlainTextResponse, Response
from whatsapp import send_whatsapp_message
from gemini import (
    MODEL_NAME,
    GeminiRateLimitError,
    ask_gemini_with_prompt,
    choose_from_presented,
    parse_search_request,
    rerank_candidates,
)
from dotenv import load_dotenv
from catalog_db import get_product_context, search_products, search_products_by_terms
from chat_db import (
    append_message,
    get_conversation_state,
    get_messages_for_conversation,
    get_open_conversation_for_user,
    get_or_create_open_conversation,
    get_recent_messages,
    get_last_gemini_call_for_conversation,
    get_events_by_correlation_id,
    init_chat_schema,
    insert_gemini_call,
    set_conversation_state,
    wa_message_id_exists,
)
from logging_utils import log_event, setup_logging

load_dotenv()

app = FastAPI()

VERIFY_TOKEN = "pp1234567890"  
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()

logger = setup_logging()
init_chat_schema()


def _selection_index(user_text: str, max_n: int) -> int | None:
    """
    Try to interpret a human selection like:
    - "1", "2", "3"
    - "الأول", "التاني", "الثالث", "الاخير", "في النص"
    Returns 1-based index.
    """
    t = (user_text or "").strip().lower()
    if not t or max_n <= 0:
        return None

    # Numeric choice
    if t.isdigit():
        idx = int(t)
        if 1 <= idx <= max_n:
            return idx

    mapping = {
        "الأول": 1,
        "اول": 1,
        "اول واحد": 1,
        "التاني": 2,
        "الثاني": 2,
        "تاني": 2,
        "الثالث": 3,
        "تالت": 3,
        "التالت": 3,
        "الأخير": max_n,
        "الاخير": max_n,
    }
    for k, v in mapping.items():
        if k in t:
            if 1 <= v <= max_n:
                return v

    if "في النص" in t or "النص" in t or "middle" in t:
        if max_n == 2:
            return 1
        return 2 if max_n >= 3 else None

    # "رقم 2"
    m = re.search(r"\b(\d)\b", t)
    if m:
        idx = int(m.group(1))
        if 1 <= idx <= max_n:
            return idx
    return None


def _looks_like_selection_reply(user_text: str) -> bool:
    """
    Heuristic: if the user reply looks like they are choosing from a numbered list,
    we may spend a Gemini call to resolve fuzzy selection. Otherwise, treat it as a new query
    to avoid unnecessary quota usage.
    """
    t = (user_text or "").strip()
    if not t:
        return False
    # Most selection replies are short.
    if len(t) <= 20:
        return True
    tl = t.lower()
    if tl.isdigit():
        return True
    if re.search(r"\b[1-9]\b", tl):
        return True
    selection_terms = [
        "الأول",
        "الاول",
        "اول",
        "التاني",
        "الثاني",
        "تاني",
        "الثالث",
        "التالت",
        "تالت",
        "الأخير",
        "الاخير",
        "رقم",
        "#",
    ]
    return any(s in tl for s in selection_terms)


def _require_admin(x_admin_token: str | None) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="ADMIN_TOKEN not configured")
    if not x_admin_token or x_admin_token.strip() != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="forbidden")

@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/webhook")
async def verify_webhook(request: Request):
    params = request.query_params
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")
    
    print("VERIFY PARAMS:", params)

    
    # Meta webhook verification:
    # - Must return hub.challenge as *plain text* when verify_token matches.
    # - Some validators probe with HEAD; handle that via @app.head("/webhook").
    if mode == "subscribe" and token == VERIFY_TOKEN and challenge is not None:
        return PlainTextResponse(content=str(challenge), status_code=200)

    # If someone hits /webhook without verification params, return a simple OK.
    if mode is None and token is None and challenge is None:
        return PlainTextResponse(content="ok", status_code=200)

    return PlainTextResponse(content="verification failed", status_code=403)


@app.head("/webhook")
async def webhook_head():
    # Some clients validate reachability with HEAD before the GET challenge.
    return Response(status_code=200)

@app.post("/webhook")
async def webhook(request: Request):
    correlation_id = uuid4()
    user_number = None
    conversation_id = None
    try:
        data = await request.json()
        log_event(
            logger,
            correlation_id=correlation_id,
            event_type="webhook_raw",
            payload={"raw": data},
            conversation_id=None,
        )

        if "entry" not in data:
            return {"status": "no entry"}
        
        value = data["entry"][0]["changes"][0]["value"]

        if "messages" not in value:
            return {"status": "not a message event"}

        message = value["messages"][0]
        user_number = message.get("from")
        wa_message_id = message.get("id")
        msg_type = message.get("type")

        if not user_number:
            log_event(
                logger,
                correlation_id=correlation_id,
                event_type="webhook_missing_from",
                payload={"wa_message_id": wa_message_id, "type": msg_type},
                conversation_id=None,
            )
            return {"status": "ok"}

        # WhatsApp may retry and deliver duplicates; skip processing if we already saw this id.
        if wa_message_id_exists(wa_message_id):
            log_event(
                logger,
                correlation_id=correlation_id,
                event_type="webhook_duplicate_ignored",
                payload={"from": user_number, "wa_message_id": wa_message_id, "type": msg_type},
                conversation_id=None,
            )
            return {"status": "ok"}

        if msg_type != "text":
            # Non-text message: store event and ask user for text
            conversation_id = get_or_create_open_conversation(user_number or "")
            append_message(
                conversation_id,
                role="user",
                direction="inbound",
                text=f"[non-text message type={msg_type}]",
                wa_message_id=wa_message_id,
            )
            reply = "ممكن تبعتلي رسالتك نص؟ (دلوقتي أنا بستقبل رسائل Text بس)"
            send_whatsapp_message(user_number, reply)
            append_message(conversation_id, role="assistant", direction="outbound", text=reply)
            log_event(
                logger,
                correlation_id=correlation_id,
                event_type="webhook_non_text",
                payload={"from": user_number, "type": msg_type, "wa_message_id": wa_message_id},
                conversation_id=conversation_id,
            )
            return {"status": "ok"}

        user_text = (message.get("text") or {}).get("body") or ""
        user_text = user_text.strip()

        conversation_id = get_or_create_open_conversation(user_number or "")
        state = get_conversation_state(conversation_id)

        log_event(
            logger,
            correlation_id=correlation_id,
            event_type="webhook_in",
            payload={"from": user_number, "text": user_text, "wa_message_id": wa_message_id},
            conversation_id=conversation_id,
        )

        # Persist inbound message
        append_message(
            conversation_id,
            role="user",
            direction="inbound",
            text=user_text or "[empty]",
            wa_message_id=wa_message_id,
        )

        history = get_recent_messages(conversation_id, limit=20)

        product_ctx = None
        product_candidates = None

        # 1) If we previously presented options, interpret selection first.
        last_presented = state.get("last_presented_candidates") or []
        last_presented_ids = state.get("last_presented_candidate_ids") or []

        presented_list: list[dict] = []
        if isinstance(last_presented, list) and last_presented:
            presented_list = [x for x in last_presented if isinstance(x, dict) and x.get("id") is not None]
        elif isinstance(last_presented_ids, list) and last_presented_ids:
            presented_list = [{"id": int(pid)} for pid in last_presented_ids[:10] if str(pid).isdigit()]

        if presented_list:
            idx = _selection_index(user_text, max_n=len(presented_list))
            selected_id = None
            if idx is not None:
                selected_id = int(presented_list[idx - 1]["id"])
            else:
                # Avoid spending quota if the message doesn't look like a selection reply.
                if _looks_like_selection_reply(user_text):
                    # Let Gemini resolve fuzzy selection from the presented list (small).
                    try:
                        sel_json, sel_prompt, sel_raw = choose_from_presented(
                            user_text, history=history, presented_candidates=presented_list[:10]
                        )
                        insert_gemini_call(
                            conversation_id=conversation_id,
                            correlation_id=correlation_id,
                            model=MODEL_NAME,
                            prompt=sel_prompt,
                            response_text=sel_raw,
                        )
                        log_event(
                            logger,
                            correlation_id=correlation_id,
                            event_type="gemini_choose_from_presented",
                            payload={"result": sel_json},
                            conversation_id=conversation_id,
                        )
                        if sel_json.get("selected_id"):
                            selected_id = int(sel_json["selected_id"])
                    except GeminiRateLimitError:
                        raise
                    except Exception as e:
                        log_event(
                            logger,
                            correlation_id=correlation_id,
                            event_type="choose_from_presented_failed",
                            payload={"error": str(e)},
                            conversation_id=conversation_id,
                        )
                else:
                    # Treat as a new query: clear the presented list to avoid getting stuck.
                    state.pop("last_presented_candidates", None)
                    state.pop("last_presented_candidate_ids", None)
                    set_conversation_state(conversation_id, state)

            if selected_id:
                try:
                    product_ctx = get_product_context(int(selected_id))
                    log_event(
                        logger,
                        correlation_id=correlation_id,
                        event_type="db_get_product_context",
                        payload={"product_id": int(selected_id), "has_context": bool(product_ctx)},
                        conversation_id=conversation_id,
                    )
                except Exception as e:
                    log_event(
                        logger,
                        correlation_id=correlation_id,
                        event_type="db_get_product_context_failed",
                        payload={"product_id": int(selected_id), "error": str(e)},
                        conversation_id=conversation_id,
                    )
                    product_ctx = None

                if product_ctx:
                    # Clear selection list and store selected product
                    state["selected_product_id"] = int(selected_id)
                    state.pop("last_presented_candidate_ids", None)
                    set_conversation_state(conversation_id, state)

                    ai_reply, prompt = ask_gemini_with_prompt(
                        user_text, history=history, product_context=product_ctx
                    )
                    insert_gemini_call(
                        conversation_id=conversation_id,
                        correlation_id=correlation_id,
                        model=MODEL_NAME,
                        prompt=prompt,
                        response_text=ai_reply,
                    )
                    log_event(
                        logger,
                        correlation_id=correlation_id,
                        event_type="gemini_answer_with_context",
                        payload={"selected_product_id": int(selected_id)},
                        conversation_id=conversation_id,
                    )
                else:
                    ai_reply = "تمام—ممكن تقولي تاني تقصد أنهي اختيار؟"
            else:
                # Not a clear selection: continue to search flow
                ai_reply = ""
        else:
            ai_reply = ""

        # 2) Hybrid search flow if we haven't answered yet
        if not ai_reply:
            parsed, parse_prompt, parse_raw = parse_search_request(user_text, history=history)
            insert_gemini_call(
                conversation_id=conversation_id,
                correlation_id=correlation_id,
                model=MODEL_NAME,
                prompt=parse_prompt,
                response_text=parse_raw,
            )
            log_event(
                logger,
                correlation_id=correlation_id,
                event_type="gemini_parse_search_request",
                payload={"parsed": parsed},
                conversation_id=conversation_id,
            )

            keywords = parsed.get("keywords") if isinstance(parsed, dict) else None
            if not isinstance(keywords, list):
                keywords = []

            # DB retrieval: get a wider candidate set for reranking.
            try:
                if keywords:
                    product_candidates = search_products_by_terms([str(k) for k in keywords], limit=50)
                    search_mode = "by_terms"
                else:
                    # Fallback: keep old behavior, but with more results.
                    product_candidates = search_products(user_text, limit=10)
                    search_mode = "full_query_fallback"

                log_event(
                    logger,
                    correlation_id=correlation_id,
                    event_type="db_search_products",
                    payload={
                        "mode": search_mode,
                        "keywords": keywords,
                        "count": len(product_candidates or []),
                        "top": [
                            {"id": c.get("id"), "display_name": c.get("display_name")}
                            for c in (product_candidates or [])[:5]
                        ],
                    },
                    conversation_id=conversation_id,
                )
            except Exception as e:
                log_event(
                    logger,
                    correlation_id=correlation_id,
                    event_type="db_search_products_failed",
                    payload={"error": str(e)},
                    conversation_id=conversation_id,
                )
                product_candidates = []

            if product_candidates:
                # Let Gemini choose best 3 and craft the human reply.
                rr, rr_prompt, rr_raw = rerank_candidates(
                    user_text, history=history, candidates=product_candidates, max_results=3
                )
                insert_gemini_call(
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    model=MODEL_NAME,
                    prompt=rr_prompt,
                    response_text=rr_raw,
                )
                log_event(
                    logger,
                    correlation_id=correlation_id,
                    event_type="gemini_rerank_candidates",
                    payload={"result": rr},
                    conversation_id=conversation_id,
                )

                reply_text = (rr.get("reply_text") or "").strip() if isinstance(rr, dict) else ""
                presented_ids = rr.get("presented_candidate_ids") if isinstance(rr, dict) else []
                if not isinstance(presented_ids, list):
                    presented_ids = []

                # Validate presented IDs are within candidates
                cand_ids = {int(c.get("id")) for c in product_candidates if c.get("id") is not None}
                safe_presented = []
                for pid in presented_ids[:5]:
                    try:
                        ip = int(pid)
                        if ip in cand_ids:
                            safe_presented.append(ip)
                    except Exception:
                        continue

                if safe_presented:
                    state["last_presented_candidate_ids"] = safe_presented
                    # Store small details to help later fuzzy selection
                    id_to_row = {int(c.get("id")): c for c in product_candidates if c.get("id") is not None}
                    state["last_presented_candidates"] = [
                        {
                            "id": pid,
                            "display_name": (id_to_row.get(pid) or {}).get("display_name"),
                            "consumer_price": (id_to_row.get(pid) or {}).get("consumer_price"),
                            "stock_quantity": (id_to_row.get(pid) or {}).get("stock_quantity"),
                        }
                        for pid in safe_presented
                    ]
                    set_conversation_state(conversation_id, state)

                if reply_text:
                    ai_reply = reply_text
                else:
                    # fallback: simple list from top candidates
                    top3 = product_candidates[:3]
                    lines = ["قدامي كذا اختيار مناسب:"]
                    for i, c in enumerate(top3, start=1):
                        name = c.get("display_name") or c.get("slug") or "منتج"
                        price = c.get("consumer_price")
                        if price is not None:
                            lines.append(f"{i}) {name} - {price}ج")
                        else:
                            lines.append(f"{i}) {name}")
                    lines.append("تحب أنهي واحد؟")
                    ai_reply = "\n".join(lines)
                    top_ids = [int(c["id"]) for c in top3 if c.get("id")]
                    state["last_presented_candidate_ids"] = top_ids
                    state["last_presented_candidates"] = [
                        {
                            "id": int(c.get("id")),
                            "display_name": c.get("display_name"),
                            "consumer_price": c.get("consumer_price"),
                            "stock_quantity": c.get("stock_quantity"),
                        }
                        for c in top3
                        if c.get("id") is not None
                    ]
                    set_conversation_state(conversation_id, state)
            else:
                # No candidates: normal assistant with history (ask clarifying question)
                ai_reply, prompt = ask_gemini_with_prompt(user_text, history=history)
                insert_gemini_call(
                    conversation_id=conversation_id,
                    correlation_id=correlation_id,
                    model=MODEL_NAME,
                    prompt=prompt,
                    response_text=ai_reply,
                )
                log_event(
                    logger,
                    correlation_id=correlation_id,
                    event_type="gemini_no_candidates_answer",
                    payload={},
                    conversation_id=conversation_id,
                )
        
        response = send_whatsapp_message(user_number, ai_reply)
        log_event(
            logger,
            correlation_id=correlation_id,
            event_type="whatsapp_send",
            payload={"to": user_number, "response": response},
            conversation_id=conversation_id,
        )

        append_message(conversation_id, role="assistant", direction="outbound", text=ai_reply)
        return {"status":"ok"}

    except GeminiRateLimitError as e:
        retry_s = getattr(e, "retry_after_seconds", None)
        if retry_s is not None and retry_s > 0:
            msg = f"معلش حصل ضغط على الخدمة—جرّب كمان {int(retry_s) + 1} ثانية."
        else:
            msg = "معلش حصل ضغط على الخدمة—جرّب بعد شوية."
        try:
            log_event(
                logger,
                correlation_id=correlation_id,
                event_type="gemini_rate_limited",
                payload={"retry_after_seconds": retry_s, "error": str(e)},
                conversation_id=conversation_id,
            )
        except Exception:
            pass
        if user_number:
            try:
                send_whatsapp_message(user_number, msg)
                if conversation_id is not None:
                    append_message(conversation_id, role="assistant", direction="outbound", text=msg)
            except Exception:
                pass
        return {"status": "ok"}

    except Exception as e:
        # Keep a last-resort log; correlation_id may not exist if error happens early.
        try:
            logger.exception("webhook_error")
        except Exception:
            pass
        return {"status": "error", "message": str(e)}


@app.get("/debug/conversation/{user_number}")
async def debug_conversation(
    user_number: str,
    limit: int = 50,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_admin_token)
    conv = get_open_conversation_for_user(user_number)
    if not conv:
        return {"status": "not_found"}
    msgs = get_messages_for_conversation(int(conv["id"]), limit=limit)
    return {"conversation": conv, "messages": msgs}


@app.get("/debug/last-gemini/{user_number}")
async def debug_last_gemini(
    user_number: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_admin_token)
    conv = get_open_conversation_for_user(user_number)
    if not conv:
        return {"status": "not_found"}
    last = get_last_gemini_call_for_conversation(int(conv["id"]))
    return {"conversation_id": int(conv["id"]), "last_gemini_call": last}


@app.get("/debug/events/{correlation_id}")
async def debug_events(
    correlation_id: str,
    x_admin_token: str | None = Header(default=None, alias="X-Admin-Token"),
):
    _require_admin(x_admin_token)
    cid: UUID = UUID(correlation_id)
    rows = get_events_by_correlation_id(cid)
    return {"correlation_id": str(cid), "events": rows}





