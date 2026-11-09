import os
import re
import json
import time
import threading
import requests
from flask import Flask, request, Response
from google import genai
from google.genai import types

# ---- הגדרות מתוך משתני סביבה (לא לשמור סיסמאות בקוד) ----
YEMOT_NUMBER = os.environ["YEMOT_NUMBER"]
YEMOT_PASSWORD = os.environ["YEMOT_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
# תיקייה לשמירת ההקלטות בימות המשיח
RECORD_PATH = os.environ.get("RECORD_PATH", "/8")
# שלוחה שאליה חוזרים בזמן ההמתנה (השלוחה הזו)
EXTENSION_PATH = os.environ.get("EXTENSION_PATH", "/8")
# קובץ מנגינת המתנה בימות המשיח, למשל /8/music (בלי סיומת). אם ריק, מושמעת הודעת "נא להמתין"
MUSIC_FILE = os.environ.get("MUSIC_FILE", "").strip()
# מצב בדיקה: אם TEST_MODE=1 השרת רק מקריא משפט פשוט
TEST_MODE = os.environ.get("TEST_MODE") == "1"

SYSTEM_PROMPT = (
    "אתה עוזר קולי בשיחת טלפון. המשתמש שולח הקלטה בעברית. "
    "החזר JSON בלבד בפורמט: {\"heard\": \"מה שהמשתמש אמר\", \"answer\": \"התשובה שלך\"}. "
    "התשובה בעברית, קצרה וברורה, עד שניים-שלושה משפטים, "
    "בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים. "
    "אם ההקלטה ריקה או לא ברורה, השאר heard ריק וב-answer בקש לחזור על השאלה."
)

app = Flask(__name__)
client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=20000))


@app.before_request
def log_every_request():
    print("REQUEST:", request.method, request.path, flush=True)


@app.route("/ping")
def ping():
    return "ok"


# היסטוריית שיחה (טקסט בלבד) ומצב עיבוד, לפי מזהה שיחה
histories = {}
calls = {}  # call_id -> {"turn": int, "last": str, "status": "idle|processing", "answer": str, "t": float}
lock = threading.Lock()


def clean_for_tts(text):
    # משאירים רק אותיות, ספרות ורווחים. סימנים מיוחדים עלולים לשבש את הפקודה
    text = re.sub(r"[^0-9A-Za-z֐-׿ ]", " ", text)
    return " ".join(text.split())[:700]


def read_command(prompt_text, call_id, turn):
    # מקריא טקסט ואז מקליט. ההקלטה מסתיימת בהקשת סולמית (#). שם הקובץ שונה בכל סבב.
    fname = "q" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10] + str(turn)
    return f"read=t-{clean_for_tts(prompt_text)}=question,no,record,{RECORD_PATH},{fname},no,yes,no"


def wait_command():
    # מנגינת המתנה (או הודעה) ואז חזרה לשלוחה, שתבדוק אם התשובה מוכנה
    if MUSIC_FILE:
        return f"id_list_message=f-{MUSIC_FILE}&go_to_folder={EXTENSION_PATH}"
    return f"id_list_message=t-נא להמתין&go_to_folder={EXTENSION_PATH}"


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


def candidate_models():
    # המודל שהוגדר ואחריו עד שני מודלי flash זמינים נוספים, למקרה של עומס או הסרה
    names = [MODEL]
    try:
        for m in client.models.list():
            n = (m.name or "").replace("models/", "")
            actions = getattr(m, "supported_actions", None) or []
            if "flash" in n and "generateContent" in actions and n not in names:
                if not any(x in n for x in ("image", "tts", "live", "audio", "preview-tts")):
                    names.append(n)
            if len(names) >= 3:
                break
    except Exception as e:
        print("list models failed:", e, flush=True)
    return names


def generate(contents):
    last_err = None
    for name in [MODEL]:
        for attempt in range(2):
            try:
                return client.models.generate_content(
                    model=name,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM_PROMPT,
                        response_mime_type="application/json",
                        max_output_tokens=400,
                    ),
                )
            except Exception as e:
                last_err = e
                print(f"gemini {name} attempt {attempt + 1} failed: {e}", flush=True)
                time.sleep(0.5)
    for name in candidate_models()[1:]:
        try:
            return client.models.generate_content(
                model=name,
                contents=contents,
                config=types.GenerateContentConfig(
                    system_instruction=SYSTEM_PROMPT,
                    response_mime_type="application/json",
                    max_output_tokens=400,
                ),
            )
        except Exception as e:
            last_err = e
            print(f"gemini fallback {name} failed: {e}", flush=True)
    raise last_err


def ask_gemini(call_id, audio_bytes):
    history = histories.setdefault(call_id, [])
    contents = list(history) + [
        types.Content(
            role="user",
            parts=[types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")],
        )
    ]
    res = generate(contents)
    data = json.loads(res.text)
    heard = (data.get("heard") or "").strip()
    answer = (data.get("answer") or "").strip() or "לא הבנתי נסה שוב"

    if heard:  # שומרים בהיסטוריה רק טקסט, כדי לשמור על מהירות
        history.append(types.Content(role="user", parts=[types.Part(text=heard)]))
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        history[:] = history[-12:]
    return answer


def worker(call_id, recorded):
    t0 = time.time()
    try:
        audio = download_recording(recorded)
        print(f"timing: download {time.time() - t0:.1f}s, {len(audio)} bytes", flush=True)
        t1 = time.time()
        answer = ask_gemini(call_id, audio)
        print(f"timing: gemini {time.time() - t1:.1f}s", flush=True)
    except Exception as e:
        print("error:", e, flush=True)
        answer = "אירעה שגיאה נסה שוב"
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["answer"] = answer
            st["status"] = "done"


def cleanup_old():
    now = time.time()
    for cid in [c for c, s in calls.items() if now - s["t"] > 3600]:
        calls.pop(cid, None)
        histories.pop(cid, None)


@app.route("/ivr", methods=["GET", "POST"])
def ivr():
    p = request.values
    call_id = p.get("ApiCallId", "unknown")

    if p.get("hangup") == "yes":
        with lock:
            calls.pop(call_id, None)
        histories.pop(call_id, None)
        return Response("", mimetype="text/plain")

    if TEST_MODE:
        return Response("id_list_message=t-שלום זו בדיקה של המערכת", mimetype="text/plain")

    recorded_all = p.getlist("question")
    recorded = recorded_all[-1] if recorded_all else None

    with lock:
        cleanup_old()
        st = calls.setdefault(
            call_id, {"turn": 0, "last": None, "status": "idle", "answer": "", "t": time.time(), "started": 0}
        )
        st["t"] = time.time()

        # הקלטה חדשה שעדיין לא טופלה: מתחילים עיבוד ברקע ומשמיעים המתנה
        if recorded and recorded != st["last"]:
            st["last"] = recorded
            st["status"] = "processing"
            st["started"] = time.time()
            threading.Thread(target=worker, args=(call_id, recorded), daemon=True).start()
            return Response(wait_command(), mimetype="text/plain")

        # עיבוד בעיצומו: ממשיכים להמתין, אבל לא יותר מ-45 שניות
        if st["status"] == "processing":
            if time.time() - st["started"] > 45:
                st["status"] = "idle"
                st["turn"] += 1
                return Response(
                    read_command("התשובה מתעכבת נסה לשאול שוב ובסיום הקש סולמית", call_id, st["turn"]),
                    mimetype="text/plain",
                )
            return Response(wait_command(), mimetype="text/plain")

        # התשובה מוכנה: מקריאים ומקליטים שאלה הבאה
        if st["status"] == "done":
            st["status"] = "idle"
            st["turn"] += 1
            text = st["answer"] + " לשאלה נוספת דבר ובסיום הקש סולמית"
            return Response(read_command(text, call_id, st["turn"]), mimetype="text/plain")

        # כניסה ראשונה לשלוחה
        return Response(
            read_command(
                "שלום אני העוזר החכם שאל אותי כל שאלה ובסיום השאלה הקש סולמית", call_id, st["turn"]
            ),
            mimetype="text/plain",
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
