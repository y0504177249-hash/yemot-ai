import os
import re
import json
import threading
import requests
from flask import Flask, request, Response
from google import genai
from google.genai import types

# ---- הגדרות מתוך משתני סביבה (לא לשמור סיסמאות בקוד) ----
YEMOT_NUMBER = os.environ["YEMOT_NUMBER"]
YEMOT_PASSWORD = os.environ["YEMOT_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# תיקייה לשמירת ההקלטות בימות המשיח (התיקייה 8 כבר קיימת)
RECORD_PATH = os.environ.get("RECORD_PATH", "/8")
# מצב בדיקה: אם מוגדר TEST_MODE=1 השרת רק מקריא משפט פשוט, בלי הקלטה
TEST_MODE = os.environ.get("TEST_MODE") == "1"
# מנגינת המתנה: שם קובץ מנגינה שהועלה לימות המשיח (למשל /8/music). ריק = בלי מנגינה
MUSIC_FILE = os.environ.get("MUSIC_FILE", "").strip()
# השלוחה שאליה חוזרים בזמן ההמתנה
IVR_FOLDER = os.environ.get("IVR_FOLDER", "/8")

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
# עבודות שרצות ברקע בזמן שמושמעת מנגינת המתנה: call_id -> {"done": Event, "answer": str}
jobs = {}


def run_job(call_id, recorded):
    job = jobs[call_id]
    try:
        audio = download_recording(recorded)
        job["answer"] = ask_gemini(call_id, audio)
    except Exception as e:
        print("error:", e, flush=True)
        job["answer"] = "אירעה שגיאה נסה שוב"
    finally:
        job["done"].set()


def hold_response():
    return Response(
        f"id_list_message=f-{MUSIC_FILE}&go_to_folder={IVR_FOLDER}",
        mimetype="text/plain",
    )


def clean_for_tts(text):
    # משאירים רק אותיות, ספרות ורווחים. סימנים מיוחדים עלולים לשבש את הפקודה בימות המשיח
    text = re.sub(r"[^0-9A-Za-z\u0590-\u05FF ]", " ", text)
    return " ".join(text.split())[:700]


def download_recording(path):
    if not path.startswith("/"):
        path = "/" + path
    if not path.lower().endswith(".wav"):
        path += ".wav"
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


def read_command(prompt_text, call_id="x"):
    # מקריא טקסט ואז מקליט את השאלה הבאה. ההקלטה מסתיימת בהקשת סולמית (#)
    fname = "q" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10]
    return f"read=t-{clean_for_tts(prompt_text)}=question,no,record,{RECORD_PATH},{fname},no,yes,no"


@app.route("/ivr", methods=["GET", "POST"])
def ivr():
    p = request.values
    call_id = p.get("ApiCallId", "unknown")

    if p.get("hangup") == "yes":
        histories.pop(call_id, None)
        jobs.pop(call_id, None)
        return Response("", mimetype="text/plain")

    if TEST_MODE:
        return Response("id_list_message=t-שלום זו בדיקה של המערכת", mimetype="text/plain")

    recorded = p.get("question")

    # מצב מנגינת המתנה (פעיל רק אם הוגדר MUSIC_FILE)
    if MUSIC_FILE:
        job = jobs.get(call_id)
        if job is not None:
            if job["done"].wait(timeout=1.5):
                jobs.pop(call_id, None)
                return Response(
                    read_command(job["answer"] + " לשאלה נוספת דבר ובסיום הקש סולמית", call_id),
                    mimetype="text/plain",
                )
            return hold_response()
        if recorded:
            jobs[call_id] = {"done": threading.Event(), "answer": ""}
            threading.Thread(target=run_job, args=(call_id, recorded), daemon=True).start()
            return hold_response()

    if not recorded:
        return Response(
            read_command("שלום אני העוזר החכם שאל אותי כל שאלה ובסיום השאלה הקש סולמית", call_id),
            mimetype="text/plain",
        )

    try:
        audio = download_recording(recorded)
        answer = ask_gemini(call_id, audio)
        return Response(read_command(answer + " לשאלה נוספת דבר ובסיום הקש סולמית", call_id), mimetype="text/plain")
    except Exception as e:
        print("error:", e)
        return Response(read_command("אירעה שגיאה נסה שוב בסיום הקש סולמית", call_id), mimetype="text/plain")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
