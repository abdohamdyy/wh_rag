import google.generativeai as genai
import os
from dotenv import load_dotenv

load_dotenv()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

model = genai.GenerativeModel("gemini-2.5-flash")

def ask_gemini(user_message: str) -> str:
    prompt= f"""
    انت موظف خدمه عملاء.
    رد بطريقه محترمه وبسيطه.
    خلي الرد قصير و طبيعي.
    
    السؤال: {user_message}
    """
    response= model.generate_content(prompt)
    return response.text