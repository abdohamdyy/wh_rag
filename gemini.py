import google.generativeai as genai
import os
import json
import re
from dotenv import load_dotenv
from google.api_core.exceptions import ResourceExhausted

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
model = genai.GenerativeModel(MODEL_NAME)

class GeminiRateLimitError(RuntimeError):
    def __init__(self, message: str, *, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def _retry_after_from_error_message(msg: str) -> float | None:
    """
    Attempts to parse "Please retry in 46.814s" from the Gemini error text.
    """
    m = re.search(r"Please retry in\s+([0-9]+(?:\.[0-9]+)?)s", msg or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except Exception:
        return None


def _extract_json(text: str) -> str:
    """
    Best-effort JSON extraction (handles code fences / extra text).
    """
    t = (text or "").strip()
    if not t:
        return ""

    # Remove ```json fences if present
    if t.startswith("```"):
        t = t.strip("`").strip()
        # Sometimes it becomes "json\n{...}"
        if "\n" in t:
            t = t.split("\n", 1)[1].strip()
    # Try to take the outermost JSON object/array
    first_obj = t.find("{")
    first_arr = t.find("[")
    start = min([x for x in [first_obj, first_arr] if x != -1], default=-1)
    if start == -1:
        return t
    # Find last closing brace/bracket
    end_obj = t.rfind("}")
    end_arr = t.rfind("]")
    end = max(end_obj, end_arr)
    if end == -1 or end <= start:
        return t[start:]
    return t[start : end + 1].strip()


def _generate(prompt: str) -> str:
    try:
        resp = model.generate_content(prompt)
        return (resp.text or "").strip()
    except ResourceExhausted as e:
        msg = str(e)
        raise GeminiRateLimitError(
            msg,
            retry_after_seconds=_retry_after_from_error_message(msg),
        ) from e


def _history_json(history: list[dict] | None) -> str | None:
    if not history:
        return None
    # Keep only safe fields and short text
    cleaned = []
    for m in history[-50:]:
        cleaned.append(
            {
                "role": m.get("role"),
                "direction": m.get("direction"),
                "text": (m.get("text") or "")[:1200],
                "created_at": str(m.get("created_at") or ""),
            }
        )
    return json.dumps(cleaned, ensure_ascii=False, default=str)


def build_customer_service_prompt(
    user_message: str,
    *,
    history: list[dict] | None = None,
    product_context: dict | None = None,
    product_candidates: list[dict] | None = None,
    extra_rules: str | None = None,
) -> str:
    user_message = (user_message or "").strip()

    base_rules = """
انت موظف خدمه عملاء لمتجر ملابس على واتساب.
رد بطريقه محترمه وبسيطه.
خلي الرد قصير و طبيعي.
اكتب باللهجة المصرية لو مناسب.

قواعد مهمه:
- لو عندك بيانات منتجات (PRODUCT_CONTEXT_JSON) استخدمها فقط ولا تخمّن.
- لو مفيش بيانات كفاية، اطلب توضيح محدد من المستخدم.
- لو عندك قائمة منتجات مرشحة (PRODUCT_CANDIDATES_JSON)، ممنوع تقترح أي منتج خارج القائمة.
- العميل مش هيعرف IDs/SKU، لو محتاج اختيار اعرض 3 اختيارات مرقمة (1/2/3) واسأل: تحب أنهي واحد؟
- لو الصور جاية كـ أسماء ملفات فقط، اذكر أسماء الملفات كما هي.
""".strip()

    parts: list[str] = [base_rules]

    if extra_rules:
        parts.append("\nEXTRA_RULES:\n" + extra_rules.strip())

    hj = _history_json(history)
    if hj:
        parts.append("\nCONVERSATION_HISTORY_JSON:\n" + hj)

    if product_candidates:
        candidates_json = json.dumps(product_candidates, ensure_ascii=False, default=str)
        parts.append("\nPRODUCT_CANDIDATES_JSON:\n" + candidates_json)

    if product_context:
        ctx_json = json.dumps(product_context, ensure_ascii=False, default=str)
        parts.append("\nPRODUCT_CONTEXT_JSON:\n" + ctx_json)

    parts.append(f"\nالسؤال: {user_message}\n")
    return "\n".join(parts)

def ask_gemini(
    user_message: str,
    product_context: dict | None = None,
    product_candidates: list[dict] | None = None,
    history: list[dict] | None = None,
) -> str:
    """
    If product_context is provided, Gemini must answer grounded in it (no guessing).
    If product_candidates is provided, Gemini should ask the user to pick one.
    """
    prompt = build_customer_service_prompt(
        user_message,
        history=history,
        product_context=product_context,
        product_candidates=product_candidates,
    )
    return _generate(prompt)


def ask_gemini_with_prompt(
    user_message: str,
    *,
    history: list[dict] | None = None,
    product_context: dict | None = None,
    product_candidates: list[dict] | None = None,
    extra_rules: str | None = None,
) -> tuple[str, str]:
    prompt = build_customer_service_prompt(
        user_message,
        history=history,
        product_context=product_context,
        product_candidates=product_candidates,
        extra_rules=extra_rules,
    )
    return _generate(prompt), prompt


def parse_search_request(
    user_message: str,
    *,
    history: list[dict] | None = None,
) -> tuple[dict, str, str]:
    """
    Returns (parsed_json, prompt, raw_response_text).
    Output JSON only.
    """
    extra_rules = """
انت بتحول كلام العميل لفهم منظم يساعدنا نبحث في الداتابيز.
مطلوب منك ترجع JSON فقط بدون أي كلام.
الشكل:
{
  "intent": "search" | "select" | "question" | "other",
  "keywords": ["..."],
  "constraints": {"color": "...", "size": "...", "price_max": 0, "fit": "...", "gender": "..."},
  "needs_clarification": true/false,
  "clarifying_question": "..." 
}
قواعد:
- keywords: كلمات قصيرة من كلام العميل (عربي/انجليزي)، ما تزيدش عن 12 كلمة.
- لو مش واضح، needs_clarification=true واكتب سؤال توضيحي واحد.
""".strip()

    prompt = build_customer_service_prompt(
        user_message,
        history=history,
        extra_rules=extra_rules,
    )
    raw = _generate(prompt)
    extracted = _extract_json(raw)
    try:
        data = json.loads(extracted) if extracted else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("intent", "other")
    data.setdefault("keywords", [])
    data.setdefault("constraints", {})
    return data, prompt, raw


def rerank_candidates(
    user_message: str,
    *,
    history: list[dict] | None = None,
    candidates: list[dict],
    max_results: int = 3,
) -> tuple[dict, str, str]:
    """
    Returns JSON with a user-friendly reply and the presented candidate IDs (subset of candidates).
    """
    extra_rules = f"""
اختار أفضل {int(max_results)} منتجات فقط من PRODUCT_CANDIDATES_JSON بناءً على كلام العميل والسياق.
ممنوع تقترح أي منتج خارج القائمة.
ارجع JSON فقط بالشكل:
{{
  "reply_text": "نص الرد اللي هيتبعت للعميل، لازم يكون فيه 1/2/3 قدام الاختيارات",
  "presented_candidate_ids": [123, 456, 789],
  "needs_clarification": true/false,
  "clarifying_question": "لو محتاج توضيح، اكتب سؤال واحد هنا وإلا خليه فاضي"
}}
قواعد لصياغة reply_text:
- اكتب اسم المنتج + السعر لو موجود.
- لو المخزون = 0 اذكر انه غير متوفر أو متوفر حسب الكمية.
- اسأل في الآخر: تحب أنهي واحد؟
""".strip()

    prompt = build_customer_service_prompt(
        user_message,
        history=history,
        product_candidates=candidates,
        extra_rules=extra_rules,
    )
    raw = _generate(prompt)
    extracted = _extract_json(raw)
    try:
        data = json.loads(extracted) if extracted else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("reply_text", "")
    data.setdefault("presented_candidate_ids", [])
    data.setdefault("needs_clarification", False)
    data.setdefault("clarifying_question", "")
    return data, prompt, raw


def choose_from_presented(
    user_message: str,
    *,
    presented_candidates: list[dict],
    history: list[dict] | None = None,
) -> tuple[dict, str, str]:
    """
    Given a SMALL list of candidates previously shown, pick the best matching one based on the user's reply.
    Returns JSON: { "selected_id": 123 | null }
    """
    extra_rules = """
العميل بيرد على اختيارات اتعرضت عليه قبل كده.
مطلوب ترجع JSON فقط بالشكل:
{ "selected_id": 123 } أو { "selected_id": null }
قواعد:
- اختار selected_id من PRODUCT_CANDIDATES_JSON فقط.
- لو رد العميل مش كفاية، رجّع null.
""".strip()
    prompt = build_customer_service_prompt(
        user_message,
        history=history,
        product_candidates=presented_candidates,
        extra_rules=extra_rules,
    )
    raw = _generate(prompt)
    extracted = _extract_json(raw)
    try:
        data = json.loads(extracted) if extracted else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if "selected_id" not in data:
        data["selected_id"] = None
    return data, prompt, raw