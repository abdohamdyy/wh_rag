import requests
import os

def send_whatsapp_message(to: str, message: str):
    phone_number_id = os.getenv("PHONE_NUMBER_ID")
    token = os.getenv("WHATSAPP_TOKEN")

    url= f"https://graph.facebook.com/v17.0/{phone_number_id}/messages"

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }

    payload = {
        "messaging_product" : "whatsapp",
        "to" : to,
        "type": "text",
        "text": {"body": message}
    }

    response = requests.post(url, headers=headers, json=payload)

    print("WHATSAPP STATUS:", response.status_code)
    print("WHATSAPP BODY:", response.text)

    return response.json()