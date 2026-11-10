import os
import io
import re
import wave
import math
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
# מפתח Gemini נפרד לכל מתקשר (לפי מספר טלפון), כדי שלכל אדם תהיה מכסה משלו. למשל {"0501234567": "AIza..."}
try:
    GEMINI_KEYS = json.loads(os.environ.get("GEMINI_KEYS", "{}"))
except Exception:
    GEMINI_KEYS = {}
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
# ALLOWED_PHONES=* מתיר לכל מי שמגיע לשלוחה (למשל מי שהזין את סיסמת השלוחה בימות המשיח)
ALLOW_ALL_PHONES = "*" in ALLOWED_PHONES


def phone_allowed(phone):
    return ALLOW_ALL_PHONES or phone in ALLOWED_PHONES

# שליחה דרך ה-Gmail שלך באמצעות Google Apps Script (בלי שירות צד שלישי)
MAIL_WEBHOOK_URL = os.environ.get("MAIL_WEBHOOK_URL", "").strip()
MAIL_WEBHOOK_SECRET = os.environ.get("MAIL_WEBHOOK_SECRET", "").strip()
WEBHOOK_ENABLED = bool(MAIL_WEBHOOK_URL and MAIL_WEBHOOK_SECRET)
EMAIL_ENABLED = bool(WEBHOOK_ENABLED or (BREVO_API_KEY and MAIL_FROM))

EMAIL_PROMPT = (
    " יש לך גישה לתיבת המייל של המשתמש. בפורמט ה-JSON אפשר להוסיף את השדות send_email, mail, confirm_pending (ברירת מחדל null). "
    "כשמגדירים אחד מהם השאר answer ריק. "
    "לשליחת מייל הגדר send_email={\"to\": \"שם איש קשר או שם אדם או כתובת מייל מלאה באותיות לועזיות\", \"subject\": \"נושא\", \"body\": \"תוכן\"}. "
    "אל תשאל אישור בעצמך: השרת בודק את הכתובת, מקריא אותה ושואל את המשתמש. "
    "אנשי קשר ידועים: " + (", ".join(MAIL_CONTACTS.keys()) or "אין") + ". "
    "כשהמשתמש אומר כתובת מייל בקול, כתוב אותה בצורה תקנית (שטרודל הוא @ ונקודה היא .). "
    "אם יש הערת מערכת שמייל ממתין לאישור: כן או אישור = confirm_pending=\"yes\", לא או ביטול = confirm_pending=\"no\", ובקשת שינוי = send_email חדש. "
    "לקריאת מיילים הגדר mail={\"action\": \"search\", \"query\": \"שאילתת ג'ימייל\"}, למשל is:unread או from:דני או newer_than:1d או subject:חשבונית או in:inbox. "
    "אחר כך אפשר לקרוא מייל מהרשימה האחרונה לפי מספר: mail={\"action\": \"read\", \"index\": 1, \"full\": false}. full=true רק אם ביקש להקריא את כל המייל."
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

# הגדרה חד-פעמית לשלוחה 1.1 בימות, בהפעלה יזומה דרך משתנה סביבה
if os.environ.get("CONFIGURE_YEMOT_EXTENSION") == "1":
    try:
        setup_response = requests.get(
            "https://www.call2all.co.il/ym/api/UpdateExtension",
            params={
                "token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}",
                "path": "ivr2:1/1",
                "type": "api",
                "api_link": "https://yemot-smart-search.onrender.com/ivr",
                "api_hangup_send": "yes",
            },
            timeout=20,
        )
        setup_response.raise_for_status()
        print("Yemot 1.1 extension update:", setup_response.text[:500], flush=True)
    except Exception as setup_error:
        print("Yemot 1.1 extension update failed:", str(setup_error)[:300], flush=True)
client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=20000))
_clients = {}
_tls = threading.local()


def client_for(phone):
    key = GEMINI_KEYS.get(phone)
    if not key:
        return client
    if key not in _clients:
        _clients[key] = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=20000))
    return _clients[key]


def cur_client():
    return getattr(_tls, "client", None) or client


def _bk(name):
    # מכסות נספרות לכל מפתח בנפרד
    return f"{getattr(_tls, 'keyid', 'default')}:{name}"


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


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAIL_READ_PROMPT = "ענה בעברית בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים."


def mail_json(payload):
    r = requests.post(MAIL_WEBHOOK_URL, json=dict(payload, secret=MAIL_WEBHOOK_SECRET), timeout=25)
    r.raise_for_status()
    txt = r.text.strip()
    if txt == "forbidden":
        raise RuntimeError("webhook: forbidden (secret mismatch)")
    try:
        return json.loads(txt)
    except Exception:
        raise RuntimeError(f"webhook bad response: {txt[:100]}")


def send_mail_to(to_addr, subject, body):
    subject = (subject or "הודעה")[:200]
    body = (body or "")[:5000]
    if WEBHOOK_ENABLED:
        r = requests.post(
            MAIL_WEBHOOK_URL,
            json={"secret": MAIL_WEBHOOK_SECRET, "action": "send", "to": to_addr, "subject": subject, "body": body},
            timeout=25,
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
            "subject": subject,
            "textContent": body,
        },
        timeout=15,
    )
    r.raise_for_status()


def speak_email(addr):
    return (
        addr.replace("@", " שטרודל ").replace(".", " נקודה ").replace("-", " מקף ").replace("_", " קו תחתון ")
    )


def resolve_recipient(to):
    # מחזיר רשימת מועמדים: [{"name":..., "email":...}]
    if to in MAIL_CONTACTS:
        return [{"name": to, "email": MAIL_CONTACTS[to]}]
    if EMAIL_RE.match(to):
        return [{"name": "", "email": to}]
    if WEBHOOK_ENABLED:
        res = mail_json({"action": "find_contact", "name": to})
        return [c for c in res.get("contacts", []) if c.get("email")]
    return []


def prepare_email(call_id, action):
    to = (action.get("to") or "").strip()
    subject = (action.get("subject") or "").strip()
    body = (action.get("body") or "").strip()
    if not to or not body:
        return "למי לשלוח ומה לכתוב במייל"
    cands = resolve_recipient(to)
    if not cands:
        return f"לא מצאתי כתובת עבור {to}. אמור את כתובת המייל המלאה"
    if len(cands) > 1:
        names = ", ".join(f"{c.get('name') or ''} {speak_email(c['email'])}" for c in cands[:3])
        return f"מצאתי כמה אפשרויות: {names}. אמור את הכתובת המלאה של מי שאתה מתכוון אליו"
    c = cands[0]
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["pending"] = {"addr": c["email"], "subject": subject or "הודעה", "body": body}
    if c.get("name"):
        target = f"ל{c['name']} בכתובת {speak_email(c['email'])}"
    else:
        target = f"לכתובת {speak_email(c['email'])}"
    return (
        f"אני עומד לשלוח {target} עם הנושא {subject or 'הודעה'} "
        f"והתוכן: {body[:300]}. לשלוח? אמור כן או לא"
    )


def confirm_email(call_id, decision):
    with lock:
        st = calls.get(call_id)
        pend = st.get("pending") if st else None
        if st is not None:
            st["pending"] = None
    if not pend:
        return "אין מייל ממתין לשליחה"
    if str(decision).lower() != "yes":
        return "ביטלתי את המייל"
    send_mail_to(pend["addr"], pend["subject"], pend["body"])
    return "המייל נשלח"


def handle_mail(call_id, mail, heard):
    act = (mail.get("action") or "").lower()
    if act == "search":
        res = mail_json({"action": "search", "query": mail.get("query") or "in:inbox", "max": 5})
        items = res.get("messages", [])
        with lock:
            st = calls.get(call_id)
            if st is not None:
                st["mails"] = [m["id"] for m in items]
        if not items:
            return "לא נמצאו מיילים"
        lines = []
        for i, m in enumerate(items, 1):
            flag = "לא נקרא" if m.get("unread") else "נקרא"
            lines.append(
                f"{i}. מאת {m.get('from', '')} | נושא: {m.get('subject', '')} | {m.get('date', '')} | {flag} | תקציר: {m.get('snippet', '')}"
            )
        out = call_model(
            f"בקשת המשתמש: {heard}\nהמיילים:\n" + "\n".join(lines) +
            "\nהקרא למשתמש בקצרה את המיילים לפי מספר: שולח ונושא, ואמור שאפשר לבקש לקרוא מייל לפי מספר.",
            types.GenerateContentConfig(system_instruction=MAIL_READ_PROMPT, max_output_tokens=600),
        )
        return (out.text or "").strip() or "לא הצלחתי לקרוא את המיילים"
    if act == "read":
        try:
            idx = int(mail.get("index") or 1)
        except Exception:
            idx = 1
        with lock:
            st = calls.get(call_id)
            ids = list(st.get("mails", [])) if st else []
        if not ids or idx < 1 or idx > len(ids):
            return "קודם בקש לראות את רשימת המיילים"
        res = mail_json({"action": "read", "id": ids[idx - 1]})
        body = (res.get("body") or "")[:3000]
        if bool(mail.get("full")):
            instr = "הקרא את תוכן המייל כמעט מילה במילה, בלי להוסיף דברים"
        else:
            instr = "סכם את המייל בשלושה עד ארבעה משפטים"
        out = call_model(
            f"מאת: {res.get('from', '')}\nנושא: {res.get('subject', '')}\nתוכן: {body}\n{instr}.",
            types.GenerateContentConfig(system_instruction=MAIL_READ_PROMPT, max_output_tokens=900),
        )
        return (out.text or "").strip() or "לא הצלחתי לקרוא את המייל"
    return "לא הבנתי מה לעשות עם המיילים"


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
        _bad_models[_bk(name)] = time.time() + 86400  # לא קיים: יום שלם
    elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        _bad_models[_bk(name)] = time.time() + 3600  # מכסה נגמרה: שעה
    else:
        _bad_models[_bk(name)] = time.time() + 30


def _is_bad(name):
    return _bad_models.get(_bk(name), 0) > time.time()


def discover_models():
    # גילוי מודלי flash זמינים נוספים, פעם אחת בלבד
    if not _discovered["done"]:
        _discovered["done"] = True
        try:
            for m in cur_client().models.list():
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
                    return cur_client().models.generate_content(model=name, contents=contents, config=config)
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
TTS_STYLE = "A warm, gentle, calming and reassuring voice, soft and friendly, speaking Hebrew clearly at a relaxed slightly slow pace, with natural pauses and a soothing conversational tone. Never rushed, sharp, or overly theatrical."


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
                res = cur_client().models.generate_content(
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


def make_hold_music():
    # מנגינת המתנה מקורית ועדינה: פעמוני פסנתר רכים ופד אווירתי, ללא מילים
    rate = 22050
    duration = 18
    total = rate * duration
    chords = [
        [261.63, 329.63, 392.00, 523.25],  # C
        [220.00, 261.63, 329.63, 440.00],  # Am
        [174.61, 220.00, 261.63, 349.23],  # F
        [196.00, 246.94, 293.66, 392.00],  # G
    ]
    frames = bytearray()
    for i in range(total):
        t = i / rate
        chord = chords[int(t // 4.5) % len(chords)]
        fade = min(1.0, t / 2.0, (duration - t) / 2.5)
        sample = 0.0
        for j, freq in enumerate(chord):
            # שכבת רקע רכה ומתמשכת
            sample += 0.11 * math.sin(2 * math.pi * freq * t + j * 0.4)
            sample += 0.035 * math.sin(2 * math.pi * freq * 2.002 * t + j)
        # צלילי פסנתר עדינים במרווחים, עם דעיכה טבעית
        beat = t % 1.5
        note_index = int(t / 1.5) % 4
        note_freq = chord[note_index]
        bell = 0.0
        if beat < 1.25:
            bell = 0.16 * math.exp(-3.0 * beat) * (
                math.sin(2 * math.pi * note_freq * beat) +
                0.28 * math.sin(2 * math.pi * note_freq * 2.01 * beat)
            )
        value = max(-0.85, min(0.85, (sample + bell) * fade * 0.62))
        frames.extend(int(value * 32767).to_bytes(2, "little", signed=True))
    out = io.BytesIO()
    with wave.open(out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(frames)
    return out.getvalue()


def upload_hold_music():
    if not MUSIC_FILE:
        return
    path = MUSIC_FILE if MUSIC_FILE.lower().endswith(".wav") else MUSIC_FILE + ".wav"
    if not path.startswith("/"):
        path = "/" + path
    wav_bytes = make_hold_music()
    response = requests.post(
        "https://www.call2all.co.il/ym/api/UploadFile",
        data={
            "token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}",
            "path": f"ivr2:{path}",
            "convertAudio": "1",
        },
        files={"file": ("hold_music.wav", wav_bytes, "audio/wav")},
        timeout=30,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("responseStatus") != "OK":
        raise RuntimeError(f"hold music upload failed: {response.text[:200]}")
    print("Yemot hold music uploaded:", path, flush=True)


# העלאת מנגינת המתנה מקורית לימות בעת הפעלת השירות
if MUSIC_FILE:
    try:
        upload_hold_music()
    except Exception as music_error:
        print("Yemot hold music setup failed:", str(music_error)[:300], flush=True)


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


def ask_gemini(call_id, audio_bytes, phone=""):
    history = histories.setdefault(call_id, [])
    parts = [types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")]
    with lock:
        st0 = calls.get(call_id)
        has_pending = bool(st0 and st0.get("pending"))
    if has_pending and EMAIL_ENABLED:
        parts.append(types.Part(text="הערת מערכת: יש מייל ממתין לאישור המשתמש."))
    contents = list(history) + [types.Content(role="user", parts=parts)]
    res = generate(contents)
    data = json.loads(res.text)
    heard = (data.get("heard") or "").strip()
    answer = (data.get("answer") or "").strip() or "לא הבנתי נסה שוב"
    change_voice = bool(data.get("change_voice")) if VOICE_ENABLED else False
    lookup = data.get("lookup")
    send = data.get("send_email")
    mail = data.get("mail")
    confirm = data.get("confirm_pending")

    try:
        if EMAIL_ENABLED and (confirm or isinstance(send, dict) or isinstance(mail, dict)):
            print(f"mail request from {phone or 'unknown'}", flush=True)
            if not phone_allowed(phone):
                print("mail blocked: phone not allowed", flush=True)
                answer = "אין לך הרשאה לגשת למיילים"
            elif confirm:
                answer = confirm_email(call_id, confirm)
            elif isinstance(send, dict):
                answer = prepare_email(call_id, send)
            elif isinstance(mail, dict) and WEBHOOK_ENABLED:
                t = time.time()
                answer = handle_mail(call_id, mail, heard)
                print(f"timing: mail {mail.get('action')} {time.time() - t:.1f}s", flush=True)
        elif isinstance(lookup, dict) and (lookup.get("query") or heard):
            t = time.time()
            found = do_lookup(lookup, heard)
            print(f"timing: lookup {lookup.get('type')} {time.time() - t:.1f}s", flush=True)
            answer = found or "לא הצלחתי למצוא מידע על זה"
    except Exception as e:
        print("action error:", e, flush=True)
        answer = "לא הצלחתי לבצע את הפעולה"

    if heard:  # שומרים בהיסטוריה רק טקסט, כדי לשמור על מהירות
        history.append(types.Content(role="user", parts=[types.Part(text=heard)]))
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        history[:] = history[-12:]
    return answer, change_voice


def worker(call_id, recorded, phone=""):
    _tls.client = client_for(phone)
    _tls.keyid = phone if phone in GEMINI_KEYS else "default"
    t0 = time.time()
    failed = False
    try:
        audio = download_recording(recorded)
        print(f"timing: download {time.time() - t0:.1f}s, {len(audio)} bytes", flush=True)
        t1 = time.time()
        answer, change_voice = ask_gemini(call_id, audio, phone)
        print(f"timing: gemini {time.time() - t1:.1f}s", flush=True)
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
            full_text = answer + ". לשאלה נוספת, פשוט דבר. אחרי כל שאלה הקש סולמית כדי לסיים את ההקלטה."
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
                    read_command("התשובה מתעכבת. נסה לשאול שוב, ואחרי השאלה הקש סולמית.", call_id, st["turn"]),
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
            text = st["answer"] + " לשאלה נוספת, פשוט דבר. אחרי כל שאלה הקש סולמית כדי לסיים את ההקלטה."
            return Response(read_command(text, call_id, st["turn"]), mimetype="text/plain")

        # כניסה ראשונה לשלוחה
        return Response(
            read_command(
                "שלום, אני העוזר החכם. אפשר לשאול אותי כל שאלה. אחרי כל שאלה הקש סולמית כדי לסיים את ההקלטה.", call_id, st["turn"]
            ),
            mimetype="text/plain",
        )


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
