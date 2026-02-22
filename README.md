# WhatsApp Gemini Bot (FastAPI)

بوت واتساب بسيط يربط **WhatsApp Cloud API** مع **Google Gemini** لتقديم ردود خدمة عملاء (باللهجة المصرية) مع حفظ المحادثات والأحداث في **PostgreSQL**.

## المتطلبات

- Python 3.11+  
- قاعدة بيانات PostgreSQL متاحة (مع صلاحيات إنشاء جداول في `public`)
- حساب WhatsApp Cloud API (Meta) + Webhook شغال

## التثبيت (Windows)

افتح PowerShell داخل فولدر المشروع:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## إعداد متغيرات البيئة (ENV)

أنشئ ملف `.env` بجانب `main.py` وضع القيم التالية (الأهم أولاً):

```dotenv
# Gemini
GEMINI_API_KEY=your_api_key
GEMINI_MODEL=gemini-2.5-flash

# WhatsApp Cloud API (Meta)
WHATSAPP_TOKEN=your_whatsapp_token
PHONE_NUMBER_ID=your_phone_number_id

# Admin endpoints (اختياري لكنه مهم لتفعيل /debug)
ADMIN_TOKEN=change_me

# Postgres (يفضل تظبطها بدل القيم الافتراضية اللي في الكود)
PGHOST=localhost
PGPORT=5432
PGUSER=postgres
PGPASSWORD=postgres
PGDATABASE=bww_v1
# PGSSLMODE=require

# Logging (اختياري)
LOG_LEVEL=INFO
LOG_DIR=logs
```

## التشغيل

```powershell
uvicorn main:app --host 0.0.0.0 --port 8000
```

التطبيق هيعمل إنشاء تلقائي لجداول الشات عند الإقلاع (راجع `chat_db.init_chat_schema()`).

## Webhook (Meta / WhatsApp)

- **Callback URL**: استخدم مسار `/webhook`  
  مثال: `https://<your-domain>/webhook`
- **Verify Token**: متعرّف حاليًا **داخل الكود** في `main.py`:
  - `VERIFY_TOKEN = "pp1234567890"`
  لازم تخلّي نفس القيمة في إعدادات Webhook عند Meta (أو تغيّرها في الكود لتناسبك).

Endpoints:
- `GET /webhook`: التحقق (verification)
- `POST /webhook`: استقبال رسائل WhatsApp (Text فقط حاليًا)

## Debug Endpoints (محمية بـ Admin Token)

لازم تبعت هيدر:
- `X-Admin-Token: <ADMIN_TOKEN>`

Endpoints:
- `GET /debug/conversation/{user_number}`
- `GET /debug/last-gemini/{user_number}`
- `GET /debug/events/{correlation_id}`

## ملاحظات سريعة

- مشروعك يحتوي على فولدر `env/` (Virtualenv) داخل الريبو. عادةً بنستبعده من Git، لكن ده لا يمنع التشغيل.
- يوجد مثال NodeJS داخل `ngrok-webhook-nodejs-sample/` (مش ضروري لتشغيل نسخة الـ FastAPI).


