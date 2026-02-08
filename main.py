from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response
from whatsapp import send_whatsapp_message
from gemini import ask_gemini
from dotenv import load_dotenv
from catalog_db import get_product_context, search_products

load_dotenv()

app = FastAPI()

VERIFY_TOKEN = "pp1234567890"  

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
    try:
        data = await request.json()
        print("RAW DATA:", data)

        if "entry" not in data:
            return {"status": "no entry"}
        
        value = data["entry"][0]["changes"][0]["value"]

        if "messages" not in value:
            return {"status": "not a message event"}

        message = value["messages"][0]
        user_text = message["text"]["body"]
        user_number = message["from"]
        print("USER MESSAGE:", user_text)
        print("USER NUMBER:", user_number)

        product_ctx = None
        product_candidates = None

        # Retrieval: try to detect if message is about a product.
        # Keep it conservative: only treat as product-related if search returns matches.
        try:
            product_candidates = search_products(user_text, limit=3)
            if product_candidates and len(product_candidates) == 1:
                product_id = int(product_candidates[0]["id"])
                product_ctx = get_product_context(product_id)
        except Exception as e:
            print("DB RETRIEVAL ERROR:", e)
            product_candidates = None
            product_ctx = None

        if product_ctx:
            ai_reply = ask_gemini(user_text, product_context=product_ctx)
        elif product_candidates and len(product_candidates) > 1:
            ai_reply = ask_gemini(user_text, product_candidates=product_candidates)
        else:
            ai_reply = ask_gemini(user_text)
        print("AI REPLY:", ai_reply)
        
        response = send_whatsapp_message(user_number,ai_reply)
        print("WHATSAPP RESPONSE:", response)
        return {"status":"ok"}
    
    except Exception as e:
        print ("ERROR:", e)
        return {"status": "error", "message": str(e)}





