import os
import io
import re
import wave
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

# ---- שליחת מיילים (Brevo). אם לא מוגדר, היכולת כבויה ----
BREVO_API_KEY = os.environ.get("BREVO_API_KEY", "").strip()
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip()  # כתובת שנוספה ואומתה ב-Brevo
# אנשי קשר מותרים כ-JSON, למשל {"אני": "me@gmail.com", "אבא": "dad@gmail.com"}
try:
    MAIL_CONTACTS = json.loads(os.environ.get("MAIL_CONTACTS", "{}"))
except Exception:
    MAIL_CONTACTS = {}
# מספרי טלפון שמורשים לשלוח מיילים (מופרדים בפסיק). ריק = אף אחד
ALLOWED_PHONES = [x.strip() for x in os.environ.get("ALLOWED_PHONES", "").split(",") if x.strip()]
# שליחה דרך ה-Gmail שלך באמצעות Google Apps Script (בלי שירות צד שלישי)
MAIL_WEBHOOK_URL = os.environ.get("MAIL_WEBHOOK_URL", "").strip()
MAIL_WEBHOOK_SECRET = os.environ.get("MAIL_WEBHOOK_SECRET", "").strip()
EMAIL_ENABLED = bool(MAIL_CONTACTS and ((MAIL_WEBHOOK_URL and MAIL_WEBHOOK_SECRET) or (BREVO_API_KEY and MAIL_FROM)))

EMAIL_PROMPT = (
    " אפשר לשלוח מייל. אנשי הקשר המותרים: " + ", ".join(MAIL_CONTACTS.keys()) + ". "
    "בפורמט ה-JSON הוסף שדה send_email, ברירת מחדל null. "
    "כשהמשתמש מבקש לשלוח מייל, אל תשלח מיד: ב-answer אמור למי, מה הנושא ומה התוכן בקצרה, "
    "ושאל אם לשלוח, ובקש שיגיד כן או לא, ושדה send_email נשאר null. "
    "רק אם בהקלטה הנוכחית המשתמש אישר במפורש (כן), מלא "
    "send_email={\"to\": \"שם איש קשר מהרשימה בדיוק\", \"subject\": \"נושא\", \"body\": \"תוכן\"} "
    "עם אותם פרטים שאושרו. אל תשלח לשם שאינו ברשימה."
)

# ---- קול טבעי (Gemini TTS) ----
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1") == "1"
TTS_MODEL = os.environ.get("TTS_MODEL", "").strip()
# קולות חמים ורגועים קודם. מחליפים בהאמירה "החלף קול"
VOICES = ["Sulafat", "Vindemiatrix", "Achernar", "Achird", "Algieba", "Gacrux", "Despina", "Schedar"]
VOICE_PROMPT = (
    " כשהמשתמש מבקש להחליף קול (למשל החלף קול), הגדר בפורמט ה-JSON את השדה change_voice=true, "
    "וב-answer אמור שהחלפת קול. בכל מקרה אחר השדה change_voice הוא false."
)

SYSTEM_PROMPT = (
    "אתה עוזר קולי בשיחת טלפון. המשתמש שולח הקלטה בעברית. "
    "החזר JSON בלבד בפורמט: {\"heard\": \"מה שהמשתמש אמר\", \"answer\": \"התשובה שלך\"}. "
    "התשובה בעברית, קצרה וברורה, עד שניים-שלושה משפטים, "
    "בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים. "
    "אם ההקלטה ריקה או לא ברורה, השאר heard ריק וב-answer בקש לחזור על השאלה."
)

LOOKUP_PROMPT = (
    " אפשר לחפש מידע. בפורמט ה-JSON הוסף שדה lookup, ברירת מחדל null. "
    "לשאלות על ידע כללי, אנשים, מקומות, מושגים והיסטוריה הגדר "
    "lookup={\"type\": \"wikipedia\", \"query\": \"שם הערך\"}. "
    "לחדשות, מזג אוויר, מחירים, תוצאות ספורט ושאלות עדכניות הגדר "
    "lookup={\"type\": \"web\", \"query\": \"מה לחפש\"}. "
    "כשמגדירים lookup השאר answer ריק. אל תחפש לשיחה רגילה או לשאלה שאתה יודע לענות עליה בביטחון."
)
SYSTEM_PROMPT += LOOKUP_PROMPT
if VOICE_ENABLED:
    SYSTEM_PROMPT += VOICE_PROMPT

if EMAIL_ENABLED:
    SYSTEM_PROMPT += EMAIL_PROMPT

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


def send_email(to_name, subject, body):
    to_addr = MAIL_CONTACTS.get(to_name)
    if not to_addr:
        raise ValueError(f"contact not allowed: {to_name}")
    if MAIL_WEBHOOK_URL and MAIL_WEBHOOK_SECRET:
        r = requests.post(
            MAIL_WEBHOOK_URL,
            json={
                "secret": MAIL_WEBHOOK_SECRET,
                "to": to_addr,
                "subject": (subject or "הודעה")[:200],
                "body": (body or "")[:5000],
            },
            timeout=20,
        )
        r.raise_for_status()
        if r.text.strip() != "ok":
            raise RuntimeError(f"webhook said: {r.text[:100]}")
        return
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
        json={
            "sender": {"email": MAIL_FROM, "name": "העוזר החכם"},
            "to": [{"email": to_addr}],
            "subject": (subject or "הודעה")[:200],
            "textContent": (body or "")[:5000],
        },
        timeout=15,
    )
    r.raise_for_status()


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


FALLBACK_MODELS = [
    x.strip()
    for x in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.8-flash-lite,gemini-3.5-flash-lite,gemini-3.5-flash").split(",")
    if x.strip()
]
_bad_models = {}  # שם מודל -> זמן (epoch) שעד אליו מדלגים עליו
_discovered = {"done": False, "names": []}


def _mark_bad(name, err):
    msg = str(err)
    if "404" in msg or "NOT_FOUND" in msg:
        _bad_models[name] = time.time() + 86400  # לא קיים: יום שלם
    elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        _bad_models[name] = time.time() + 3600  # מכסה נגמרה: שעה
    else:
        _bad_models[name] = time.time() + 30


def _is_bad(name):
    return _bad_models.get(name, 0) > time.time()


def discover_models():
    # גילוי מודלי flash זמינים נוספים, פעם אחת בלבד
    if not _discovered["done"]:
        _discovered["done"] = True
        try:
            for m in client.models.list():
                n = (m.name or "").replace("models/", "")
                actions = getattr(m, "supported_actions", None) or []
                skip = ("tts", "image", "live", "audio", "embed", "2.5", "2.0", "1.5")
                if "flash" in n and "generateContent" in actions and not any(x in n for x in skip):
                    _discovered["names"].append(n)
        except Exception as e:
            print("list models failed:", e, flush=True)
    return _discovered["names"]


def call_model(contents, config):
    last_err = None
    base = [MODEL] + [m for m in FALLBACK_MODELS if m != MODEL]
    for stage in (0, 1):
        names = base if stage == 0 else [n for n in discover_models() if n not in base]
        for name in names:
            if _is_bad(name):
                continue
            for attempt in (1, 2):
                try:
                    return client.models.generate_content(model=name, contents=contents, config=config)
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    print(f"gemini {name} attempt {attempt} failed: {msg[:200]}", flush=True)
                    transient = any(x in msg for x in ("503", "UNAVAILABLE", "504", "DEADLINE"))
                    if transient and attempt == 1:
                        time.sleep(0.5)
                        continue
                    _mark_bad(name, e)
                    break
    raise last_err or RuntimeError("no model available")


def generate(contents):
    return call_model(
        contents,
        types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            response_mime_type="application/json",
            max_output_tokens=400,
        ),
    )


TTS_MODELS = [TTS_MODEL] if TTS_MODEL else ["gemini-3.8-flash-lite-tts", "gemini-3.8-flash-tts"]
TTS_STYLE = "warm, calm and soothing, slow gentle pace, like a kind friend"


def tts_wav(text, voice):
    last_err = None
    for name in TTS_MODELS:
        if _is_bad(name):
            continue
        for with_style in (True, False):
            part = {"text": text}
            if with_style:
                part["speech_metadata"] = {"style": TTS_STYLE}
            try:
                res = client.models.generate_content(
                    model=name,
                    contents=[{"role": "user", "parts": [part]}],
                    config={
                        "response_modalities": ["AUDIO"],
                        "speech_config": {"voice_config": {"voice": voice}},
                    },
                )
                data = res.candidates[0].content.parts[0].inline_data.data
                if data[:4] == b"RIFF":  # הבקשה מחזירה כבר קובץ WAV שלם
                    return data
                buf = io.BytesIO()
                w = wave.open(buf, "wb")
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(24000)
                w.writeframes(data)
                w.close()
                return buf.getvalue()
            except Exception as e:
                last_err = e
                msg = str(e)
                print(f"tts {name} style={with_style} failed: {msg[:200]}", flush=True)
                if any(x in msg for x in ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")):
                    _mark_bad(name, e)
                    break
    raise last_err or RuntimeError("no tts model available")


def upload_to_yemot(wav_bytes, name):
    r = requests.post(
        "https://www.call2all.co.il/ym/api/UploadFile",
        data={
            "token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}",
            "path": f"ivr2:{RECORD_PATH}/{name}.wav",
            "convertAudio": "1",
        },
        files={"file": (name + ".wav", wav_bytes, "audio/wav")},
        timeout=20,
    )
    r.raise_for_status()
    try:
        status = r.json().get("responseStatus")
    except Exception:
        status = None
    if status != "OK":
        raise RuntimeError(f"upload failed: {r.text[:200]}")
    return f"{RECORD_PATH}/{name}"


WIKI_HEADERS = {"User-Agent": "YemotAIAssistant/1.0 (personal phone assistant)"}
SHORT_PROMPT = "ענה בעברית בשניים עד שלושה משפטים קצרים, בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים."


def wikipedia_lookup(query):
    for lang in ("he", "en"):
        r = requests.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query, "format": "json", "srlimit": 1},
            headers=WIKI_HEADERS,
            timeout=8,
        )
        r.raise_for_status()
        hits = r.json().get("query", {}).get("search", [])
        if not hits:
            continue
        title = hits[0]["title"]
        r2 = requests.get(
            f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title.replace(' ', '_'), safe='')}",
            headers=WIKI_HEADERS,
            timeout=8,
        )
        r2.raise_for_status()
        extract = (r2.json().get("extract") or "").strip()
        if extract:
            return f"{title}: {extract[:1500]}"
    return ""


def answer_from_text(question, info):
    res = call_model(
        f"שאלה: {question}\nמידע מוויקיפדיה: {info}\nענה על השאלה לפי המידע.",
        types.GenerateContentConfig(system_instruction=SHORT_PROMPT, max_output_tokens=300),
    )
    return (res.text or "").strip()


def web_lookup(question):
    res = call_model(
        question,
        types.GenerateContentConfig(
            system_instruction=SHORT_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=400,
        ),
    )
    return (res.text or "").strip()


def do_lookup(lookup, heard):
    kind = (lookup.get("type") or "").lower()
    query = (lookup.get("query") or heard or "").strip()
    try:
        if kind == "wikipedia":
            info = wikipedia_lookup(query)
            if info:
                return answer_from_text(heard or query, info)
            # לא נמצא בוויקיפדיה: ננסה חיפוש כללי
            return web_lookup(heard or query)
        return web_lookup(heard or query)
    except Exception as e:
        print("lookup error:", e, flush=True)
        return ""


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
    email_action = data.get("send_email") if EMAIL_ENABLED else None
    change_voice = bool(data.get("change_voice")) if VOICE_ENABLED else False
    lookup = data.get("lookup")
    if isinstance(lookup, dict) and (lookup.get("query") or heard):
        t = time.time()
        found = do_lookup(lookup, heard)
        print(f"timing: lookup {lookup.get('type')} {time.time() - t:.1f}s", flush=True)
        answer = found or "לא הצלחתי למצוא מידע על זה"

    if heard:  # שומרים בהיסטוריה רק טקסט, כדי לשמור על מהירות
        history.append(types.Content(role="user", parts=[types.Part(text=heard)]))
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        history[:] = history[-12:]
    return answer, email_action, change_voice


def worker(call_id, recorded, phone=""):
    t0 = time.time()
    failed = False
    try:
        audio = download_recording(recorded)
        print(f"timing: download {time.time() - t0:.1f}s, {len(audio)} bytes", flush=True)
        t1 = time.time()
        answer, action, change_voice = ask_gemini(call_id, audio)
        print(f"timing: gemini {time.time() - t1:.1f}s", flush=True)
        if action and isinstance(action, dict):
            if phone not in ALLOWED_PHONES:
                print("email blocked: phone not allowed", flush=True)
                answer = "אין לך הרשאה לשלוח מיילים"
            else:
                try:
                    send_email(action.get("to"), action.get("subject"), action.get("body"))
                    print("email sent to", action.get("to"), flush=True)
                    answer = "המייל נשלח"
                except Exception as e:
                    print("email error:", e, flush=True)
                    answer = "לא הצלחתי לשלוח את המייל"
    except Exception as e:
        print("error:", e, flush=True)
        answer = "אירעה שגיאה נסה שוב"
        change_voice = False
        failed = True

    audio_path = None
    if VOICE_ENABLED and not failed:
        with lock:
            st = calls.get(call_id)
            if st is not None and change_voice:
                st["voice"] = (st["voice"] + 1) % len(VOICES)
            voice = VOICES[st["voice"]] if st is not None else VOICES[0]
            turn = (st["turn"] + 1) if st is not None else 1
        try:
            t2 = time.time()
            full_text = answer + ". לשאלה נוספת דבר, ובסיום הקש סולמית."
            wav = tts_wav(full_text, voice)
            name = "a" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10] + str(turn % 2)
            audio_path = upload_to_yemot(wav, name)
            print(f"timing: voice {voice} {time.time() - t2:.1f}s", flush=True)
        except Exception as e:
            print("voice error:", e, flush=True)
            audio_path = None

    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["answer"] = answer
            st["audio"] = audio_path
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
            call_id, {"turn": 0, "last": None, "status": "idle", "answer": "", "t": time.time(), "started": 0, "voice": 0, "audio": None}
        )
        st["t"] = time.time()

        # הקלטה חדשה שעדיין לא טופלה: מתחילים עיבוד ברקע ומשמיעים המתנה
        if recorded and recorded != st["last"]:
            st["last"] = recorded
            st["status"] = "processing"
            st["started"] = time.time()
            threading.Thread(target=worker, args=(call_id, recorded, p.get("ApiPhone", "")), daemon=True).start()
            return Response(wait_command(), mimetype="text/plain")

        # עיבוד בעיצומו: ממשיכים להמתין, אבל לא יותר מ-45 שניות
        if st["status"] == "processing":
            if time.time() - st["started"] > 70:
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
            if st.get("audio"):
                fname = "q" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10] + str(st["turn"])
                cmd = f"read=f-{st['audio']}=question,no,record,{RECORD_PATH},{fname},no,yes,no"
                return Response(cmd, mimetype="text/plain")
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
