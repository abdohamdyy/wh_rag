import google.generativeai as genai
import os
import json
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel("gemini-2.5-flash")

def ask_gemini(
    user_message: str,
    product_context: dict | None = None,
    product_candidates: list[dict] | None = None,
) -> str:
    """
    If product_context is provided, Gemini must answer grounded in it (no guessing).
    If product_candidates is provided, Gemini should ask the user to pick one.
    """
    user_message = (user_message or "").strip()

    base_rules = """
انت موظف خدمه عملاء.
رد بطريقه محترمه وبسيطه.
خلي الرد قصير و طبيعي.

قواعد مهمه:
- لو عندك بيانات منتجات (PRODUCT_CONTEXT_JSON) استخدمها فقط ولا تخمّن.
- لو مفيش بيانات كفاية، اطلب توضيح محدد من المستخدم.
- لو عندك قائمة منتجات مرشحة (PRODUCT_CANDIDATES_JSON)، اطلب من المستخدم يحدد المنتج بالـ id أو slug.
- لو الصور جاية كـ أسماء ملفات فقط، اذكر أسماء الملفات كما هي.
"""

    parts: list[str] = [base_rules]

    if product_candidates:
        candidates_json = json.dumps(product_candidates, ensure_ascii=False, default=str)
        parts.append("\nPRODUCT_CANDIDATES_JSON:\n" + candidates_json)

    if product_context:
        # Keep it structured so the model can reference fields safely.
        ctx_json = json.dumps(product_context, ensure_ascii=False, default=str)
        parts.append("\nPRODUCT_CONTEXT_JSON:\n" + ctx_json)

    parts.append(f"\nالسؤال: {user_message}\n")

    prompt = "\n".join(parts)
    response = model.generate_content(prompt)
    return (response.text or "").strip()