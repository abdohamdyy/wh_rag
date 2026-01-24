from fastapi import FastAPI, Request
from whatsapp import send_whatsapp_message
from gemini import ask_gemini
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

VERIFY_TOKEN = "my_verify_token"  

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

    
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return int(challenge)
    return {"error": "verification failed"}

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

        ai_reply = ask_gemini(user_text)
        print("AI REPLY:", ai_reply)
        
        response = send_whatsapp_message(user_number,ai_reply)
        print("WHATSAPP RESPONSE:", response)
        return {"status":"ok"}
    
    except Exception as e:
        print ("ERROR:", e)
        return {"status": "error", "message": str(e)}





