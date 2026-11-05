import os
import json
import requests
from flask import Flask, request, Response
from google import genai
from google.genai import types

# ---- הגדרות מתוך משתני סביבה (לא לשמור סיסמאות בקוד) ----
YEMOT_NUMBER = os.environ["YEMOT_NUMBER"]
YEMOT_PASSWORD = os.environ["YEMOT_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")

SYSTEM_PROMPT = (
    "אתה עוזר קולי בשיחת טלפון. המשתמש שולח הקלטה בעברית. "
    "החזר JSON בלבד בפורמט: {\"heard\": \"מה שהמשתמש אמר\", \"answer\": \"התשובה שלך\"}. "
    "התשובה בעברית, קצרה וברורה, עד שניים-שלושה משפטים, "
    "בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים. "
    "אם ההקלטה ריקה או לא ברורה, השאר heard ריק וב-answer בקש לחזור על השאלה."
)

app = Flask(__name__)
client = genai.Client(api_key=GEMINI_API_KEY)


@app.before_request
def log_every_request():
    # רושם כל בקשה שמגיעה לשרת, כדי לראות אם ימות המשיח פונים אליו
    print("REQUEST:", request.method, request.full_path, dict(request.values), flush=True)


@app.route("/ping")
def ping():
    return "ok"

# היסטוריית שיחה (טקסט בלבד) לפי מזהה שיחה
histories = {}


def clean_for_tts(text):
    for ch in ['&', '=', ',', '*', '#', '"', "'", '\n', '\r', '-', '_']:
        text = text.replace(ch, ' ')
    return ' '.join(text.split())[:700]


def download_recording(path):
    r = requests.get(
        "https://www.call2all.co.il/ym/api/DownloadFile",
        params={"token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}", "path": f"ivr2:{path}"},
        timeout=15,
    )
    r.raise_for_status()
    return r.content


def ask_gemini(call_id, audio_bytes):
    history = histories.setdefault(call_id, [])
    contents = list(history) + [
        types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")],
        )
    ]
    res = client.models.generate_content(
        model=MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=400,
        ),
    )
    data = json.loads(res.text)
    heard = (data.get("heard") or "").strip()
    answer = (data.get("answer") or "").strip() or "לא הבנתי, נסה שוב"

    if heard:  # שומרים בהיסטוריה רק טקסט, כדי לשמור על מהירות
        history.append(types.Content(role="user", parts=[types.Part(text=heard)]))
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        history[:] = history[-12:]
    return answer


def read_command(prompt_text):
    # מקריא טקסט ואז מקליט את השאלה הבאה. ההקלטה נעצרת אוטומטית בשקט.
    return f"read=t-{clean_for_tts(prompt_text)}=question,no,record,/ai_questions,q,no,yes,no"


@app.route("/ivr", methods=["GET", "POST"])
def ivr():
    p = request.values
    call_id = p.get("ApiCallId", "unknown")

    if p.get("hangup") == "yes":
        histories.pop(call_id, None)
        return Response("", mimetype="text/plain")

    recorded = p.get("question")

    if not recorded:
        return Response(
            read_command("שלום, אני העוזר החכם. שאל אותי כל שאלה, ואחרי התשובה אפשר להמשיך לשאול."),
            mimetype="text/plain",
        )

    try:
        audio = download_recording(recorded)
        answer = ask_gemini(call_id, audio)
        return Response(read_command(answer), mimetype="text/plain")
    except Exception as e:
        print("error:", e)
        return Response(read_command("אירעה שגיאה, נסה שוב"), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
