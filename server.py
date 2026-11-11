import os
import io
import re
import wave
import math
import json
import time
import base64
import hmac
import hashlib
import functools
import threading
import collections
import requests
try:
    import psycopg
except ImportError:
    psycopg = None
from flask import Flask, request, Response, session, jsonify, send_file
from google import genai
from google.genai import types

# ---- הגדרות מתוך משתני סביבה (לא לשמור סיסמאות בקוד) ----
YEMOT_NUMBER = os.environ["YEMOT_NUMBER"]
YEMOT_PASSWORD = os.environ["YEMOT_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
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



# שליחה דרך ה-Gmail שלך באמצעות Google Apps Script (בלי שירות צד שלישי)
MAIL_WEBHOOK_URL = os.environ.get("MAIL_WEBHOOK_URL", "").strip()
MAIL_WEBHOOK_SECRET = os.environ.get("MAIL_WEBHOOK_SECRET", "").strip()
WEBHOOK_ENABLED = bool(MAIL_WEBHOOK_URL and MAIL_WEBHOOK_SECRET)
EMAIL_ENABLED = bool(WEBHOOK_ENABLED or (BREVO_API_KEY and MAIL_FROM))

def build_email_prompt(contacts):
    return (
        " יש לך גישה לתיבת המייל של המשתמש. בפורמט ה-JSON אפשר להוסיף את השדות send_email, mail, confirm_pending (ברירת מחדל null). "
        "כשמגדירים אחד מהם השאר answer ריק. "
        "לשליחת מייל הגדר send_email={\"to\": \"שם איש קשר או שם אדם או כתובת מייל מלאה באותיות לועזיות\", \"subject\": \"נושא\", \"body\": \"תוכן\"}. "
        "אל תשאל אישור בעצמך: השרת בודק את הכתובת, מקריא אותה ושואל את המשתמש. "
        "אנשי קשר ידועים: " + (", ".join(contacts.keys()) or "אין") + ". "
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

BASE_PROMPT = (
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

# ---------------- הגדרות שנשמרות וניתנות לעריכה מממשק הניהול ----------------
CONFIG_FILE = os.environ.get("CONFIG_FILE", "/tmp/yemot_config.json")
DEFAULT_GREETING = "שלום אני העוזר החכם שאל אותי כל שאלה ובסיום השאלה הקש סולמית"
START_TIME = time.time()
_store = {"settings": {}, "callers": {}, "loaded": 0.0, "source": "env", "error": "", "refreshing": False}
_store_lock = threading.Lock()
events = collections.deque(maxlen=100)


def settings_defaults():
    return {
        "model": MODEL,
        "voice_enabled": VOICE_ENABLED,
        "default_voice": VOICES[0],
        "music_file": MUSIC_FILE,
        "mail_mode": "all" if ALLOW_ALL_PHONES else ("listed" if ALLOWED_PHONES else "off"),
        "mail_contacts": MAIL_CONTACTS,
        "greeting": DEFAULT_GREETING,
        "extra_instructions": "",
    }


def cfg(name):
    v = _store["settings"].get(name)
    return settings_defaults()[name] if v is None else v


def voice_on():
    return bool(cfg("voice_enabled"))


def caller(phone):
    return _store["callers"].get(phone) or {}


def all_keys():
    keys = dict(GEMINI_KEYS)
    for ph, c in _store["callers"].items():
        if c.get("key"):
            keys[ph] = c["key"]
    return keys


def phone_allowed(phone):
    mode = cfg("mail_mode")
    if mode == "all":
        return True
    if mode == "listed":
        return phone in ALLOWED_PHONES or bool(caller(phone).get("mail"))
    return False


def default_voice_index():
    try:
        return VOICES.index(cfg("default_voice"))
    except ValueError:
        return 0


def redact(text):
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[מפתח]", str(text))
    for k in list(all_keys().values()) + [GEMINI_API_KEY]:
        if k and len(k) > 8:
            text = text.replace(k, "[מפתח]")
    return text


_prompt_cache = {"key": None, "text": ""}


def system_prompt():
    contacts = cfg("mail_contacts")
    extra = (cfg("extra_instructions") or "").strip()
    key = (json.dumps(contacts, sort_keys=True, ensure_ascii=False), extra, voice_on(), EMAIL_ENABLED)
    if _prompt_cache["key"] != key:
        text = BASE_PROMPT + LOOKUP_PROMPT
        if voice_on():
            text += VOICE_PROMPT
        if EMAIL_ENABLED:
            text += build_email_prompt(contacts)
        if extra:
            text += " הנחיות נוספות מהמשתמש: " + extra
        _prompt_cache["key"] = key
        _prompt_cache["text"] = text
    return _prompt_cache["text"]


def _read_local():
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _write_local():
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"settings": _store["settings"], "callers": _store["callers"]}, f, ensure_ascii=False)
    except Exception as e:
        print("local config write failed:", e, flush=True)


def _store_error(e):
    msg = str(e)
    return "הסקריפט בגוגל עוד לא עודכן" if "unknown" in msg or "bad response" in msg else redact(msg)[:150]


def refresh_store():
    try:
        if WEBHOOK_ENABLED:
            res = mail_json({"action": "config_get"})
            if not isinstance(res, dict) or ("settings" not in res and "callers" not in res):
                raise RuntimeError("unknown")
            data, source = res, "google"
            if not (data.get("settings") or data.get("callers")):
                local = _read_local()  # הועלה זמנית לפני עדכון הסקריפט: מעבירים לגוגל
                if local.get("settings") or local.get("callers"):
                    data = local
                    for sec in ("settings", "callers"):
                        try:
                            mail_json({"action": "config_set", "section": sec, "value": local.get(sec) or {}})
                        except Exception:
                            pass
        else:
            data, source = _read_local(), "local"
        with _store_lock:
            _store["settings"] = data.get("settings") or {}
            _store["callers"] = data.get("callers") or {}
            _store["source"] = source
            _store["error"] = ""
    except Exception as e:
        with _store_lock:
            if not _store["loaded"]:
                data = _read_local()
                _store["settings"] = data.get("settings") or {}
                _store["callers"] = data.get("callers") or {}
            _store["source"] = "local"
            _store["error"] = _store_error(e)
    _store["loaded"] = time.time()


def maybe_refresh():
    if time.time() - _store["loaded"] > 30 and not _store["refreshing"]:
        _store["refreshing"] = True

        def run():
            try:
                refresh_store()
            finally:
                _store["refreshing"] = False

        threading.Thread(target=run, daemon=True).start()


def persist(section, value):
    # שומר בזיכרון, בקובץ מקומי ובחשבון הגוגל. מחזיר אזהרה אם השמירה הקבועה נכשלה
    with _store_lock:
        _store[section] = value
    _write_local()
    if WEBHOOK_ENABLED:
        try:
            res = mail_json({"action": "config_set", "section": section, "value": value})
            if not (isinstance(res, dict) and res.get("ok")):
                raise RuntimeError("unknown")
            _store["source"] = "google"
            _store["error"] = ""
        except Exception as e:
            _store["error"] = _store_error(e)
            return "נשמר זמנית בלבד: השמירה הקבועה בגוגל נכשלה (" + _store["error"] + ")"
    return ""


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
    key = all_keys().get(phone)
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
_history_loaded = set()


def db_connect():
    if not DATABASE_URL or psycopg is None:
        return None
    try:
        return psycopg.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        print("database connection failed:", redact(e), flush=True)
        return None


def db_save_caller(phone, profile):
    conn = db_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO caller_profiles (phone, display_name, enabled, updated_at)
                       VALUES (%s, %s, %s, now())
                       ON CONFLICT (phone) DO UPDATE SET
                         display_name = EXCLUDED.display_name,
                         enabled = EXCLUDED.enabled,
                         updated_at = now()""",
                    (phone, profile.get("name", ""), profile.get("enabled", True)),
                )
        return True
    except Exception as e:
        print("database caller save failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def db_load_caller(phone):
    if not phone:
        return
    conn = db_connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT display_name, enabled FROM caller_profiles WHERE phone = %s",
                (phone,),
            )
            row = cur.fetchone()
        if row:
            current = dict(_store["callers"].get(phone) or {})
            current["name"] = row[0] or current.get("name", "")
            current["enabled"] = bool(row[1])
            _store["callers"][phone] = current
    except Exception as e:
        print("database caller load failed:", redact(e), flush=True)
    finally:
        conn.close()


def db_load_history(phone):
    conn = db_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT heard, answer FROM call_transcripts
                   WHERE phone = %s ORDER BY created_at DESC, id DESC LIMIT 6""",
                (phone,),
            )
            rows = cur.fetchall()
        return list(reversed(rows))
    except Exception as e:
        print("database history load failed:", redact(e), flush=True)
        return []
    finally:
        conn.close()


def db_save_transcript(call_id, phone, heard, answer):
    if not DATABASE_URL or not phone or not heard:
        return False
    conn = db_connect()
    if conn is None:
        return False
    try:
        profile = caller(phone)
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO call_transcripts
                       (call_id, phone, caller_name, heard, answer)
                       VALUES (%s, %s, %s, %s, %s)""",
                    (str(call_id)[:200], phone[:30], profile.get("name", "")[:40],
                     heard[:5000], answer[:5000]),
                )
        return True
    except Exception as e:
        print("database transcript save failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


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
    music = cfg("music_file")
    if music:
        return f"id_list_message=f-{music}&go_to_folder={EXTENSION_PATH}"
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
    contacts = cfg("mail_contacts")
    if to in contacts:
        return [{"name": to, "email": contacts[to]}]
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


_bad_reason = {}


def _mark_bad(name, err):
    msg = str(err)
    if "404" in msg or "NOT_FOUND" in msg:
        until, reason = time.time() + 86400, "missing"  # לא קיים: יום שלם
    elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
        until, reason = time.time() + 3600, "quota"  # מכסה נגמרה: שעה
    else:
        until, reason = time.time() + 30, "transient"
    _bad_models[_bk(name)] = until
    _bad_reason[_bk(name)] = reason


def _is_bad(name):
    return _bad_models.get(_bk(name), 0) > time.time()


def quota_state(keyid):
    now = time.time()
    primary = f"{keyid}:{cfg('model')}"
    if _bad_models.get(primary, 0) > now and _bad_reason.get(primary) == "quota":
        return "quota"
    return "ok"


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
    model = cfg("model")
    base = [model] + [m for m in FALLBACK_MODELS if m != model]
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
            system_instruction=system_prompt(),
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
            item = {"type": "text", "text": text}
            if with_style:
                item["annotations"] = [{"type": "speech_metadata", "style": TTS_STYLE}]
            try:
                res = cur_client().interactions.create(
                    model=name,
                    input=[{"type": "user_input", "content": [item]}],
                    response_format={"type": "audio"},
                    generation_config={"speech_config": [{"voice": voice}]},
                )
                data = base64.b64decode(res.output_audio.data)
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
    filename = MUSIC_FILE.rsplit("/", 1)[-1]
    filename = filename if filename.lower().endswith(".wav") else filename + ".wav"
    folder = EXTENSION_PATH.rstrip("/") or "/"
    path = (folder + "/" + filename) if folder != "/" else "/" + filename
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
WEB_RESEARCH_PROMPT = (
    "בצע מחקר יסודי על סמך תוצאות Google Search. חפש כמה היבטים של השאלה והצלב מידע בין כמה מקורות אמינים ועצמאיים כשאפשר. "
    "העדף מקורות רשמיים, מחקרים וגופי חדשות מוכרים; בדוק תאריכים והבחן בין עובדות, הערכות ודעות. "
    "ענה בעברית בצורה מקיפה אך מתאימה להקראה בטלפון: בדרך כלל חמש עד שמונה משפטים, עם הפרטים החשובים, מסקנה ושמות המקורות המרכזיים. "
    "אם יש מחלוקת או שאין מידע מספיק, אמור זאת במפורש ואל תמציא. בלי כוכביות, אימוג'ים או סימנים מיוחדים."
)


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
            system_instruction=WEB_RESEARCH_PROMPT,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            max_output_tokens=900,
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
    if phone and call_id not in _history_loaded:
        for heard_old, answer_old in db_load_history(phone):
            if heard_old:
                history.append(types.Content(role="user", parts=[types.Part(text=heard_old)]))
                history.append(types.Content(role="model", parts=[types.Part(text=answer_old)]))
        history[:] = history[-12:]
        _history_loaded.add(call_id)
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
    change_voice = bool(data.get("change_voice")) if voice_on() else False
    lookup = data.get("lookup")
    send = data.get("send_email")
    mail = data.get("mail")
    confirm = data.get("confirm_pending")

    try:
        if EMAIL_ENABLED and (confirm or isinstance(send, dict) or isinstance(mail, dict)):
            print(f"mail request from {phone or 'unknown'}", flush=True)
            _tls.kind = "mail"
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
            _tls.kind = "lookup"
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
        if phone:
            db_save_transcript(call_id, phone, heard, answer)
    return answer, change_voice


def worker(call_id, recorded, phone=""):
    keys = all_keys()
    _tls.client = client_for(phone)
    _tls.keyid = phone if phone in keys else "default"
    _tls.kind = "chat"
    t0 = time.time()
    failed = False
    err = ""
    try:
        audio = download_recording(recorded)
        print(f"timing: download {time.time() - t0:.1f}s, {len(audio)} bytes", flush=True)
        t1 = time.time()
        answer, change_voice = ask_gemini(call_id, audio, phone)
        print(f"timing: gemini {time.time() - t1:.1f}s", flush=True)
    except Exception as e:
        print("error:", redact(e), flush=True)
        answer = "אירעה שגיאה נסה שוב"
        change_voice = False
        failed = True
        err = redact(str(e))[:140]

    audio_path = None
    if voice_on() and not failed:
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
            print("voice error:", redact(e), flush=True)
            audio_path = None

    events.appendleft({
        "t": time.time(), "phone": phone or "?", "kind": getattr(_tls, "kind", "chat"),
        "ok": not failed, "sec": round(time.time() - t0, 1), "voice": bool(audio_path), "err": err,
    })
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
    phone = p.get("ApiPhone", "")
    maybe_refresh()
    db_load_caller(phone)

    if p.get("hangup") == "yes":
        with lock:
            calls.pop(call_id, None)
        histories.pop(call_id, None)
        return Response("", mimetype="text/plain")

    if TEST_MODE:
        return Response("id_list_message=t-שלום זו בדיקה של המערכת", mimetype="text/plain")

    if caller(phone).get("enabled") is False:
        return Response("id_list_message=t-אין לך הרשאה להשתמש בשלוחה זו&go_to_folder=hangup", mimetype="text/plain")

    recorded_all = p.getlist("question")
    recorded = recorded_all[-1] if recorded_all else None

    with lock:
        cleanup_old()
        st = calls.setdefault(
            call_id,
            {"turn": 0, "last": None, "status": "idle", "answer": "", "t": time.time(), "started": 0,
             "voice": default_voice_index(), "audio": None},
        )
        st["t"] = time.time()

        # הקלטה חדשה שעדיין לא טופלה: מתחילים עיבוד ברקע ומשמיעים המתנה
        if recorded and recorded != st["last"]:
            st["last"] = recorded
            st["status"] = "processing"
            st["started"] = time.time()
            threading.Thread(target=worker, args=(call_id, recorded, phone), daemon=True).start()
            return Response(wait_command(), mimetype="text/plain")

        # עיבוד בעיצומו: ממשיכים להמתין, אבל לא יותר מ-70 שניות
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
        return Response(read_command(cfg("greeting") or DEFAULT_GREETING, call_id, st["turn"]), mimetype="text/plain")


# ---------------- ממשק ניהול ----------------
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app.secret_key = hashlib.sha256(
    ("sess|" + ADMIN_PASSWORD + "|" + MAIL_WEBHOOK_SECRET + "|" + YEMOT_PASSWORD).encode("utf-8")
).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Strict",
    PERMANENT_SESSION_LIFETIME=12 * 3600,
)
ADMIN_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")
_login_fails = collections.deque()
_ip_fails = {}


@app.after_request
def admin_headers(resp):
    if request.path.startswith("/admin"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'none'; connect-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
    return resp


def client_ip():
    xff = request.headers.get("X-Forwarded-For", "")
    return (xff.split(",")[-1].strip() if xff else "") or (request.remote_addr or "?")


def login_blocked(ip):
    now = time.time()
    while _login_fails and now - _login_fails[0] > 600:
        _login_fails.popleft()
    mine = [t for t in _ip_fails.get(ip, []) if now - t < 600]
    _ip_fails[ip] = mine
    return len(mine) >= 5 or len(_login_fails) >= 20


def admin_required(f):
    @functools.wraps(f)
    def wrapper(*a, **k):
        if not ADMIN_PASSWORD or not session.get("admin"):
            return jsonify({"error": "נדרשת כניסה"}), 401
        if request.method != "GET" and request.headers.get("X-Requested-With") != "admin":
            return jsonify({"error": "בקשה לא תקינה"}), 403
        return f(*a, **k)

    return wrapper


@app.route("/admin")
@app.route("/admin/")
def admin_page():
    return send_file(ADMIN_HTML, mimetype="text/html")


@app.route("/admin/api/whoami")
def admin_whoami():
    return jsonify({"enabled": bool(ADMIN_PASSWORD), "auth": bool(session.get("admin"))})


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if request.headers.get("X-Requested-With") != "admin":
        return jsonify({"error": "בקשה לא תקינה"}), 403
    if not ADMIN_PASSWORD:
        return jsonify({"error": "ממשק הניהול כבוי"}), 403
    ip = client_ip()
    if login_blocked(ip):
        return jsonify({"error": "יותר מדי ניסיונות. נסה שוב בעוד כמה דקות"}), 429
    pw = str((request.get_json(silent=True) or {}).get("password", ""))
    if hmac.compare_digest(pw.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8")):
        session.clear()
        session["admin"] = True
        session.permanent = True
        return jsonify({"ok": True})
    now = time.time()
    _login_fails.append(now)
    _ip_fails.setdefault(ip, []).append(now)
    print("admin login failed", flush=True)
    time.sleep(1)
    return jsonify({"error": "הסיסמה שגויה"}), 401


@app.route("/admin/api/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return jsonify({"ok": True})


def key_mask(k):
    return ("••••" + k[-4:]) if k else ""


@app.route("/admin/api/state")
@admin_required
def admin_state():
    maybe_refresh()
    now = time.time()
    env_keys = dict(GEMINI_KEYS)
    rows = []
    for ph in sorted(set(_store["callers"]) | set(env_keys)):
        c = _store["callers"].get(ph, {})
        key = c.get("key") or env_keys.get(ph, "")
        rows.append({
            "phone": ph, "name": c.get("name", ""), "mask": key_mask(key),
            "has_key": bool(c.get("key")), "from_env": bool(env_keys.get(ph)) and not c.get("key"),
            "mail": bool(c.get("mail")), "enabled": c.get("enabled", True),
            "state": quota_state(ph if key else "default"),
        })
    return jsonify({
        "uptime": now - START_TIME,
        "active_calls": sum(1 for x in calls.values() if now - x["t"] < 120),
        "store": {"source": _store["source"], "error": _store["error"]},
        "features": {"email": EMAIL_ENABLED, "webhook": WEBHOOK_ENABLED},
        "settings": {k: cfg(k) for k in settings_defaults()},
        "voices": VOICES,
        "default_key": {"mask": key_mask(GEMINI_API_KEY), "state": quota_state("default")},
        "callers": rows,
        "events": list(events)[:60],
    })


@app.route("/admin/api/caller", methods=["PUT"])
@admin_required
def admin_caller_put():
    d = request.get_json(silent=True) or {}
    phone = re.sub(r"[\s\-()+]", "", str(d.get("phone", "")))
    if not re.fullmatch(r"\d{5,15}", phone):
        return jsonify({"error": "מספר טלפון לא תקין (ספרות בלבד)"}), 400
    key = re.sub(r"[\s\"'`]", "", str(d.get("key", "")))
    if key and not re.fullmatch(r"[A-Za-z0-9._-]{20,200}", key):
        return jsonify({"error": "המפתח לא נראה תקין. העתק אותו שוב במלואו"}), 400
    callers = {k: dict(v) for k, v in _store["callers"].items()}
    if phone not in callers and len(callers) >= 40:
        return jsonify({"error": "אי אפשר להוסיף יותר מ-40 קווים"}), 400
    cur = callers.get(phone, {"enabled": True, "mail": False})
    if "name" in d:
        cur["name"] = str(d.get("name", "")).strip()[:40]
    if key:
        cur["key"] = key
    if d.get("clear_key"):
        cur.pop("key", None)
    if "mail" in d:
        cur["mail"] = bool(d["mail"])
    if "enabled" in d:
        cur["enabled"] = bool(d["enabled"])
    callers[phone] = cur
    warning = persist("callers", callers)
    if not db_save_caller(phone, cur) and DATABASE_URL:
        warning = (warning + " | " if warning else "") + "פרטי המתקשר לא נשמרו במסד הנתונים"
    return jsonify({"ok": True, "warning": warning})


@app.route("/admin/api/transcripts")
@admin_required
def admin_transcripts():
    phone = re.sub(r"[^0-9]", "", request.args.get("phone", ""))[:15]
    name = request.args.get("name", "").strip()[:40]
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    try:
        limit = min(200, max(1, int(request.args.get("limit", "100"))))
    except ValueError:
        limit = 100
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        clauses, params = [], []
        if phone:
            clauses.append("phone = %s"); params.append(phone)
        if name:
            clauses.append("caller_name ILIKE %s"); params.append("%" + name + "%")
        if date_from:
            clauses.append("created_at >= %s::date"); params.append(date_from)
        if date_to:
            clauses.append("created_at < (%s::date + interval '1 day')"); params.append(date_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, call_id, phone, caller_name, created_at, heard, answer FROM call_transcripts"
                + where + " ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            rows = cur.fetchall()
        return jsonify({"items": [
            {"id": r[0], "call_id": r[1], "phone": r[2], "name": r[3],
             "created_at": r[4].isoformat(), "heard": r[5], "answer": r[6]}
            for r in rows
        ]})
    except Exception as e:
        print("transcript search failed:", redact(e), flush=True)
        return jsonify({"error": "לא ניתן לחפש בארכיון כרגע"}), 500
    finally:
        conn.close()


@app.route("/admin/api/caller/<phone>", methods=["DELETE"])
@admin_required
def admin_caller_delete(phone):
    callers = {k: dict(v) for k, v in _store["callers"].items()}
    if phone not in callers:
        return jsonify({"error": "הקו לא נמצא"}), 404
    callers.pop(phone)
    return jsonify({"ok": True, "warning": persist("callers", callers)})


@app.route("/admin/api/settings", methods=["PUT"])
@admin_required
def admin_settings_put():
    d = request.get_json(silent=True) or {}
    out = {}
    model = str(d.get("model", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{3,60}", model):
        return jsonify({"error": "שם המודל לא תקין"}), 400
    out["model"] = model
    out["voice_enabled"] = bool(d.get("voice_enabled"))
    voice = str(d.get("default_voice", ""))
    if voice not in VOICES:
        return jsonify({"error": "קול לא מוכר"}), 400
    out["default_voice"] = voice
    mode = str(d.get("mail_mode", ""))
    if mode not in ("all", "listed", "off"):
        return jsonify({"error": "הגדרת הגישה למיילים לא תקינה"}), 400
    out["mail_mode"] = mode
    contacts = d.get("mail_contacts") or {}
    if not isinstance(contacts, dict) or len(contacts) > 50:
        return jsonify({"error": "רשימת אנשי הקשר לא תקינה"}), 400
    clean = {}
    for k, v in contacts.items():
        k, v = str(k).strip(), str(v).strip()
        if not k or len(k) > 40 or not EMAIL_RE.match(v):
            return jsonify({"error": "כתובת לא תקינה עבור " + k[:40]}), 400
        clean[k] = v
    out["mail_contacts"] = clean
    music = str(d.get("music_file", "")).strip()
    if music and not re.fullmatch(r"[/A-Za-z0-9._-]{1,100}", music):
        return jsonify({"error": "נתיב המנגינה לא תקין"}), 400
    out["music_file"] = music
    greeting = str(d.get("greeting", "")).strip()
    if len(greeting) > 300:
        return jsonify({"error": "ברכת הפתיחה ארוכה מדי (עד 300 תווים)"}), 400
    out["greeting"] = greeting or DEFAULT_GREETING
    extra = str(d.get("extra_instructions", "")).strip()
    if len(extra) > 600:
        return jsonify({"error": "ההנחיות הנוספות ארוכות מדי (עד 600 תווים)"}), 400
    out["extra_instructions"] = extra
    new = dict(_store["settings"])
    new.update(out)
    return jsonify({"ok": True, "warning": persist("settings", new)})


@app.route("/admin/api/test_key", methods=["POST"])
@admin_required
def admin_test_key():
    d = request.get_json(silent=True) or {}
    key = str(d.get("key", "")).strip()
    phone = str(d.get("phone", "")).strip()
    if not key:
        key = all_keys().get(phone, "") if phone else GEMINI_API_KEY
    if not key:
        return jsonify({"ok": False, "error": "אין מפתח לבדיקה"})
    try:
        c = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15000))
        c.models.generate_content(
            model=cfg("model"), contents="ענה במילה אחת: בדיקה",
            config=types.GenerateContentConfig(max_output_tokens=20),
        )
        return jsonify({"ok": True})
    except Exception as e:
        msg = redact(str(e))
        if "API_KEY_INVALID" in msg or "API key not valid" in msg:
            msg = "המפתח לא תקין"
        elif "429" in msg or "RESOURCE_EXHAUSTED" in msg:
            msg = "המפתח תקין, אבל המכסה שלו נגמרה"
        elif "404" in msg:
            msg = "המפתח כנראה תקין, אבל המודל שהוגדר לא קיים"
        return jsonify({"ok": False, "error": msg[:200]})


@app.route("/admin/api/reset_quota", methods=["POST"])
@admin_required
def admin_reset_quota():
    _bad_models.clear()
    _bad_reason.clear()
    return jsonify({"ok": True})


maybe_refresh()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
