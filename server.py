import os
import io
import zipfile
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
import flask
import html as html_module
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import voicepack
from urllib.parse import urlencode
try:
    import psycopg
except ImportError:
    psycopg = None
from flask import Flask, request, Response, session, jsonify, send_file
from google import genai
from google.genai import types
from cryptography.fernet import Fernet
from assistants import get_prompt as get_assistant_prompt
try:
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport import requests as google_auth_requests
except ImportError:
    google_id_token = None
    google_auth_requests = None

# ---- הגדרות מתוך משתני סביבה (לא לשמור סיסמאות בקוד) ----
YEMOT_NUMBER = os.environ["YEMOT_NUMBER"]
YEMOT_PASSWORD = os.environ["YEMOT_PASSWORD"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
# Maps routing is billable; require an explicit opt-in in addition to the API key.
GOOGLE_MAPS_ROUTES_ENABLED = os.environ.get("GOOGLE_MAPS_ROUTES_ENABLED", "0") == "1"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
AUDIO_PROJECT = os.environ.get("AUDIO_PROJECT", "yemot-ai-1").strip() or "yemot-ai-1"
# מפתח Gemini נפרד לכל מתקשר (לפי מספר טלפון), כדי שלכל אדם תהיה מכסה משלו. למשל {"0501234567": "AIza..."}
try:
    GEMINI_KEYS = json.loads(os.environ.get("GEMINI_KEYS", "{}"))
except Exception:
    GEMINI_KEYS = {}
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
YIDDISH_MODE = os.environ.get("YIDDISH_MODE", "0") == "1"

def yiddish_mode():
    return getattr(_tls, "yiddish", YIDDISH_MODE)
# תיקייה לשמירת ההקלטות בימות המשיח. שלוחה 8 נשארת תיקיית הקלטות בלבד.
RECORD_PATH = os.environ.get("RECORD_PATH", "/8")
# במהלך המתנה חוזרים לכניסה הראשית של הקו, ולא לשלוחה 8.
EXTENSION_PATH = os.environ.get("EXTENSION_PATH", "/").strip() or "/"
if EXTENSION_PATH.strip("/") == "8":
    EXTENSION_PATH = "/"
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
# קוד כניסה לניהול טלפוני; ניתן לשנות דרך משתנה סביבה ב-Render.
PHONE_ADMIN_CODE = os.environ.get("PHONE_ADMIN_CODE", "333223410").strip()



# שליחה דרך ה-Gmail שלך באמצעות Google Apps Script (בלי שירות צד שלישי)
MAIL_WEBHOOK_URL = os.environ.get("MAIL_WEBHOOK_URL", "").strip()
MAIL_WEBHOOK_SECRET = os.environ.get("MAIL_WEBHOOK_SECRET", "").strip()
WEBHOOK_ENABLED = bool(MAIL_WEBHOOK_URL and MAIL_WEBHOOK_SECRET)
EMAIL_ENABLED = bool(WEBHOOK_ENABLED or (BREVO_API_KEY and MAIL_FROM))

def _fernet():
    k = os.environ.get("GEMINI_KEY_ENCRYPTION_KEY", "").strip()
    return Fernet(k.encode("ascii")) if k else None


def enc_secret(text):
    f = _fernet()
    return ("enc:" + f.encrypt(text.encode("utf-8")).decode("ascii")) if f else ("raw:" + text)


def dec_secret(text):
    text = str(text or "")
    if text.startswith("enc:"):
        f = _fernet()
        return f.decrypt(text[4:].encode("ascii")).decode("utf-8") if f else ""
    return text[4:] if text.startswith("raw:") else ""


def own_mail(phone):
    m = (_store["callers"].get(phone) or {}).get("mail_own") or {}
    return m if m.get("url") and m.get("secret") else None


def mail_target(owner=False):
    # תיבה אישית של המתקשר אם הוגדרה, אחרת התיבה הראשית
    if not owner:
        own = own_mail(getattr(_tls, "phone", ""))
        if own:
            return own["url"], dec_secret(own["secret"])
    return MAIL_WEBHOOK_URL, MAIL_WEBHOOK_SECRET


def mail_enabled_for(phone):
    if own_mail(phone):
        return True
    prof = _store["callers"].get(phone) or {}
    return bool(EMAIL_ENABLED and phone_allowed(phone) and prof.get("email_enabled", prof.get("mail", True)))


def build_email_prompt(contacts):
    common = (
        " בפורמט ה-JSON אפשר להוסיף את השדות send_email, confirm_pending (ברירת מחדל null). "
        "כשמגדירים אחד מהם השאר answer ריק. "
        "לשליחת מייל הגדר send_email={\"to\": \"שם איש קשר או שם אדם או כתובת מייל מלאה באותיות לועזיות\", \"subject\": \"נושא\", \"body\": \"תוכן\"}. "
        "אל תשאל אישור בעצמך: השרת בודק את הכתובת, מקריא אותה ושואל את המשתמש. "
        "אנשי קשר ידועים: " + (", ".join(contacts.keys()) or "אין") + ". "
        "כשהמשתמש אומר כתובת מייל בקול, כתוב אותה בצורה תקנית (שטרודל הוא @ ונקודה היא .). "
        "אם יש הערת מערכת שמייל ממתין לאישור: כן או אישור = confirm_pending=\"yes\", לא או ביטול = confirm_pending=\"no\", ובקשת שינוי = send_email חדש. "
    )
    if mail_target()[0]:
        return (
            " יש חיבור לתיבת המייל דרך שירות הדואר המחובר. " + common +
            "לקריאת מיילים הגדר mail={\"action\": \"search\", \"query\": \"שאילתת ג'ימייל\"}, למשל is:unread או from:דני או newer_than:1d או subject:חשבונית או in:inbox. "
            "אחר כך אפשר לקרוא מייל מהרשימה האחרונה לפי מספר: mail={\"action\": \"read\", \"index\": 1, \"full\": false}. full=true רק אם ביקש להקריא את כל המייל."
        )
    return " יש אפשרות לשלוח מיילים דרך שירות שליחה מחובר, אך אין גישה לקריאת תיבת הדואר. " + common


# ---- קול טבעי (Gemini TTS) ----
VOICE_ENABLED = os.environ.get("VOICE_ENABLED", "1") == "1"
TTS_MODEL = os.environ.get("TTS_MODEL", "").strip()
# קולות חמים ורגועים קודם. מחליפים בהאמירה "החלף קול"
VOICES = ["Sulafat", "Vindemiatrix", "Achernar", "Achird", "Algieba", "Gacrux", "Despina", "Schedar", "Puck", "Charon", "Kore"]
VOICE_PROMPT = (
    " כשהמשתמש מבקש להחליף קול (למשל החלף קול), הגדר בפורמט ה-JSON את השדה change_voice=true, "
    "וב-answer אמור שהחלפת קול. בכל מקרה אחר השדה change_voice הוא false."
)

YIDDISH_BASE_PROMPT = (
    "רעד בלויז אין נאטירלעכער היינטצייטיקער גערעדטער יידיש. "
    "ניץ פשוטע, קלארע יידישע ווערטער און געוויינטלעכע ישיבה'שע אויסדרוקן; ניץ נישט קיין ענגליש, לעז אדער אויסלענדישע אויסדרוקן. "
    "ענטפער קורץ און קלאר, געווענליך צוויי ביז דריי זאצן. "
    "גיב בלויז JSON אין דעם פארמאט: "
    "{\\\"heard\\\": \\\"די ווערטער פונעם באנוצער אין יידיש\\\", \\\"answer\\\": \\\"דיין ענטפער אין יידיש\\\"}. "
    "אויב די רעקארדירונג איז נישט קלאר, לאז heard ליידיג און בעט דעם באנוצער איבערצוזאגן. "
    "ביי ערנסטע סכנה אדער קריזיס רעקאמענדיר גלייך מענטשלעכע הילף."
)

YIDDISH_CHAT_PROMPT = (
    "דו ביסט א ישיבישער משפיע מיט א טיפער פסיכאלאגישער פארשטאנד — ווארעם, רואיג, קלוג און עמפאטיש. "
    "רעד ווי איינער וואס הערט אויס דעם מענטש אן צו משפט'ן, פארשטייט זיין inner world, און העלפט אים טרעפן א קלארע perspective און א קליינעם next step. "
    "רעד אין נאטירלעכער היינטצייטיקער גערעדטער יידיש, מיט געוויינטלעכע ישיבה'שע אויסדרוקן. "
    "ניץ נישט קיין ענגלישע ווערטער, קיין לעז און קיין אויסלענדישע אויסדרוקן. "
    "דערקלער אויך טיפע געפילן און ענינים פון נפש אין פשוטער, קלארער יידיש. נישט קיין ספרותדיקע יידיש, נישט קיין טרוקענע קלינישע שפראך און נישט קיין לאנגע דרשות. "
    "פרעג אמאל א גוטע, אפענע שאלה, וואַלידיר געפילן, און געב עצה אן צו נעמען דעם מענטש'ס אחריות אוועק. "
    "זיי נישט שיפוטיש און נישט איבערטריבן מיט לויב. דו ביסט נישט קיין לייסענסד פסיכאלאג: שטעל נישט קיין דיאגנאזעס און זאג נישט אז דו ביסט א טעראפיסט."
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
START_TIME = time.time()
_store = {"settings": {}, "callers": {}, "loaded": 0.0, "source": "env", "error": "", "refreshing": False}
_store_lock = threading.Lock()
events = collections.deque(maxlen=100)
_forum_memory = collections.deque(maxlen=50)
_forum_content_memory = collections.deque(maxlen=100)


def settings_defaults():
    return {
        "model": MODEL,
        "voice_enabled": VOICE_ENABLED,
        "system_enabled": True,
        "default_voice": VOICES[0],
        "music_file": MUSIC_FILE,
        "music_file_he": "",
        "music_file_yi": "",
        "mail_mode": "all" if ALLOW_ALL_PHONES else ("listed" if ALLOWED_PHONES else "off"),
        "mail_contacts": MAIL_CONTACTS,
        "extra_instructions": "",
        "assistant_mode": "smart",
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
    # Redact credentials before anything reaches Render logs or admin errors.
    text = re.sub(r"AIza[0-9A-Za-z_-]{20,}", "[מפתח]", str(text))
    secrets = [
        YEMOT_PASSWORD,
        MAIL_WEBHOOK_SECRET,
        BREVO_API_KEY,
        GOOGLE_CLIENT_SECRET if "GOOGLE_CLIENT_SECRET" in globals() else "",
        ADMIN_PASSWORD if "ADMIN_PASSWORD" in globals() else "",
        GEMINI_KEY_ENCRYPTION_KEY if "GEMINI_KEY_ENCRYPTION_KEY" in globals() else "",
        GEMINI_API_KEY,
        GOOGLE_MAPS_API_KEY if "GOOGLE_MAPS_API_KEY" in globals() else "",
    ]
    try:
        secrets.extend(all_keys().values())
    except Exception:
        pass
    for secret in secrets:
        if secret and len(secret) >= 6:
            text = text.replace(secret, "[סוד]")
    # Hide credentials that appear as query/form parameters in HTTP exceptions.
    text = re.sub(r'(?i)(token|password|secret|api[_-]?key)=([^&\s]+)', r'\1=[מוסתר]', text)
    return text


_prompt_cache = {}


def system_prompt():
    contacts = cfg("mail_contacts")
    extra = (cfg("extra_instructions") or "").strip()
    profile = caller(getattr(_tls, "phone", ""))
    # Fixed assistant personality per Yemot extension: 1/1 = Yiddish rosh yeshiva; 8 = Hebrew smart Rudy.
    selected_mode = getattr(_tls, "assistant_mode", "yeshiva" if yiddish_mode() else "smart")
    tool_action = getattr(_tls, "tool_action", "") or ""
    tools_allowed = profile.get("tools_enabled", True) is not False
    mail_ok = selected_mode in ("smart", "tools", "central") and tools_allowed and mail_enabled_for(getattr(_tls, "phone", ""))
    clarification_done = bool(getattr(_tls, "tool_clarifications_done", False))
    key = (json.dumps(contacts, sort_keys=True, ensure_ascii=False), extra, voice_on(), mail_ok, bool(mail_target()[0]), yiddish_mode(), selected_mode, tools_allowed, tool_action, clarification_done)
    if key not in _prompt_cache:
        text = (YIDDISH_BASE_PROMPT if yiddish_mode() else BASE_PROMPT)
        if tools_allowed:
            text += LOOKUP_PROMPT
        text += " " + get_assistant_prompt(selected_mode, yiddish=yiddish_mode())
        if voice_on():
            text += VOICE_PROMPT
        if mail_ok:
            text += build_email_prompt(contacts)
        if selected_mode in ("tools", "central") and tools_allowed:
            action_prompts = {
                "mail": "המשתמש נמצא בתפריט המיילים. בצע רק פעולות מייל שהתבקשו במפורש. לשליחה השתמש ב-send_email, והשרת יבקש אישור לפני שליחה.",
                "podcast": "המשתמש בחר יצירת הסכת. צור הסכת עברי קצר באורך כשתי דקות, בשיחה טבעית בין שני מגישים. החזר create_podcast={\"title\":\"כותרת\",\"dialogue\":[{\"speaker\":\"Host1\",\"text\":\"...\"},{\"speaker\":\"Host2\",\"text\":\"...\"}]}. צור 8 עד 12 חילופי דברים, עם מידע מדויק וללא המצאת עובדות.",
                "image": "המשתמש בחר יצירת תמונה ושליחתה למייל. לאחר שאלת המיקוד, החזר create_image={\"prompt\":\"תיאור מדויק של התמונה\",\"to\":\"כתובת או שם איש קשר\",\"subject\":\"נושא המייל\",\"body\":\"טקסט קצר למייל\",\"publish_to_forum\":true,\"public_title\":\"כותרת ציבורית ללא פרטים אישיים\",\"public_description\":\"תיאור ציבורי קצר ללא פרטים אישיים\"}. פרסם בפורום כברירת מחדל, אך אל תפרסם פרטים אישיים, תוכן פרטי או מזהים. אל תטען שהתמונה נשלחה; השרת יפיק אותה ויבקש אישור שליחה.",
                "file": "המשתמש בחר קריאת קבצים באמצעות AI. חפש תחילה מיילים עם קובץ מצורף באמצעות mail={\"action\":\"search\",\"query\":\"has:attachment\"}. לאחר בחירת מייל, השתמש ב-mail={\"action\":\"read_attachment\",\"index\":1,\"attachment_index\":1}. סכם בעברית את הקובץ לפי בקשת המשתמש. אם אין גישה למייל או לקובץ, אמור זאת במפורש.",
                "writing": "המשתמש בחר כתיבה וניסוח. עזור בכתיבת טקסטים, עריכה וניסוח לפי המטרה והטון שביקש.",
                "research": "המשתמש בחר חיפוש ומחקר. השתמש בכלי lookup הקיים כשנדרש מידע עובדתי או עדכני.",
                "planning": "המשתמש בחר תכנון וחישובים. בצע תכנון וחישובים בזהירות והצג את התוצאה בצורה קצרה המתאימה להקראה.",
                "utility_weather": "המשתמש בחר בדיקת מזג אוויר. בקש עיר אם לא צוינה, השתמש בחיפוש עדכני, וציין את מועד המידע.",
                "utility_calculator": "המשתמש בחר מחשבון. חשב בזהירות, בדוק את התוצאה פעם נוספת והקרא אותה בקצרה.",
                "utility_conversion": "המשתמש בחר המרת יחידות או מטבע. הבהר יחידות ומטבעות, ובשערי מטבע השתמש במידע עדכני.",
                "utility_translation": "המשתמש בחר תרגום. תרגם נאמנה לשפה שהתבקשה ושמור על המשמעות והטון.",
                "utility_research": "המשתמש בחר חיפוש מהיר. השתמש בחיפוש כאשר נדרש מידע עובדתי או עדכני, וענה בתמצית.",
                "utility_map": "המשתמש בחר כלי מפות חד-פעמי, לא צ'אט. לאחר בקשה במשפט אחד, חלץ origin, destination, travel_mode (TRANSIT או WALK), time_kind (departure או arrival) ו-time_value בפורמט ISO 8601 עם אזור הזמן Asia/Jerusalem. אם פרט כלשהו חסר או לא ברור, החזר clarification_question במשפט אחד שמבקש את כל הפרטים החסרים יחד. כשכל הפרטים ברורים, החזר map_request עם השדות האלו והשאר answer ריק. לעולם אל תמציא מסלול, תחנה, קו או הוראה; השרת יחזיר הוראות רק משירות המפות."
            }
            text += " " + action_prompts.get(tool_action, "")
        if selected_mode == "tools" and tool_action in CREATION_ACTIONS:
            if clarification_done:
                text += " המשתמש כבר ענה על שאלת המיקוד. השתמש בכל התשובות שכבר ניתנו והתחל ליצור; אל תשאל שוב על פרטים שכבר נענו. שאל רק אם פרט חיוני לביצוע חסר באמת."
            else:
                text += " לפני יצירת התוצר, אחרי שהמשתמש תיאר מה הוא רוצה, שאל שאלה אחת בלבד במשפט אחד שמרכז כמה שאלות מיקוד רלוונטיות: קהל יעד, מטרה, אורך, סגנון, פרטים שחובה לכלול ומגבלות. החזר את השאלה בשדה clarification_question והשאר שדות יצירה ריקים."
            text += " אם התוצר מתאים לפרסום כללי ואינו אישי או פרטי, החזר גם forum_content עם title, content ותוכן מסוג אחד מתוך text, guide, plan או research. אל תכלול בפורום כתובות מייל, מספרי טלפון, פרטים אישיים, תוכן הודעות דואר או סיכומי קבצים פרטיים; אם התוצר פרטי או רגיש, השאר forum_content ריק."
        if selected_mode == "tools" and tool_action == "image":
            text += " השתמש בתשובות המיקוד כדי להכין create_image והוסף public_title ו-public_description שאינם חושפים פרטים אישיים. אם כתובת המייל עדיין חסרה, שאל רק מהי כתובת הנמען."
        if selected_mode == "tools" and tool_action == "writing":
            text += " התוצר הכתוב שביקש המשתמש הוא התוכן לפרסום בפורום, בתנאי שאינו אישי או פרטי. החזר forum_content מלא עם כותרת ותוכן."
        if selected_mode == "tools" and tool_action == "utility_map":
            text += " החזר map_request={\"origin\":\"כתובת מוצא\",\"destination\":\"כתובת יעד\",\"travel_mode\":\"TRANSIT או WALK\",\"time_kind\":\"departure או arrival\",\"time_value\":\"YYYY-MM-DDTHH:MM:SS+02:00 או היסט ישראל מתאים\"}. חייבים את כל חמשת הפרטים; אין להשלים מקום, שעה או אמצעי נסיעה בניחוש. שאל במשפט אחד על כל הפרטים החסרים. זהו כלי מפות ולא שיחה חופשית."
        text += ' אם המשתמש אומר במפורש "חזרה לתפריט" או "תפריט ראשי", הוסף ל-JSON את השדה return_to_menu=true וענה בקצרה שחוזרים לתפריט.'
        if extra:
            text += " הנחיות נוספות מהמשתמש: " + extra
        _prompt_cache[key] = text
    return _prompt_cache[key]


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
        print("local config write failed:", redact(e), flush=True)


def _store_error(e):
    msg = str(e)
    return "הסקריפט בגוגל עוד לא עודכן" if "unknown" in msg or "bad response" in msg else redact(msg)[:150]


def refresh_store():
    try:
        if WEBHOOK_ENABLED:
            res = mail_json({"action": "config_get"}, owner=True)
            if not isinstance(res, dict) or ("settings" not in res and "callers" not in res):
                raise RuntimeError("unknown")
            data, source = res, "google"
            if not (data.get("settings") or data.get("callers")):
                local = _read_local()  # הועלה זמנית לפני עדכון הסקריפט: מעבירים לגוגל
                if local.get("settings") or local.get("callers"):
                    data = local
                    for sec in ("settings", "callers"):
                        try:
                            mail_json({"action": "config_set", "section": sec, "value": local.get(sec) or {}}, owner=True)
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
            res = mail_json({"action": "config_set", "section": section, "value": value}, owner=True)
            if not (isinstance(res, dict) and res.get("ok")):
                raise RuntimeError("unknown")
            _store["source"] = "google"
            _store["error"] = ""
        except Exception as e:
            _store["error"] = _store_error(e)
            return "נשמר זמנית בלבד: השמירה הקבועה בגוגל נכשלה (" + _store["error"] + ")"
    return ""


app = Flask(__name__)

# מגדירים כניסה ראשית אחת בלבד בשורש הקו. שלוחות ישנות מפנות אליה,
# ושלוחה 8 נשארת תיקיית ההקלטות ואינה עוד תפריט ראשי.
# מפעילים באמצעות CONFIGURE_YEMOT_ASSISTANT_EXTENSIONS=1 ב-Render.
if os.environ.get("CONFIGURE_YEMOT_ASSISTANT_EXTENSIONS") == "1":
    yemot_extensions = (
        ("ivr2:", "he", "menu", "התפריט הראשי"),
        ("ivr2:0", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:1", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:2", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:3", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:4", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:5", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:6", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:7", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:8", "he", "redirect", "מעבר לתפריט הראשי"),
        ("ivr2:1/1", "yi", "redirect", "מעבר לתפריט הראשי"),
    )
    for extension_path, language, mode, title in yemot_extensions:
        try:
            setup_response = requests.get(
                "https://www.call2all.co.il/ym/api/UpdateExtension",
                params={
                    "token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}",
                    "path": extension_path,
                    "type": "api",
                    "title": title,
                    "api_link": f"https://yemot-smart-search.onrender.com/ivr?lang={language}&mode={mode}",
                    "api_hangup_send": "yes",
                },
                timeout=20,
            )
            setup_response.raise_for_status()
            setup_result = setup_response.json()
            if setup_result.get("responseStatus") != "OK":
                raise RuntimeError("Yemot returned a non-OK response")
            print(f"Yemot {extension_path} mode={mode} language={language} update: OK", flush=True)
        except Exception as setup_error:
            print(f"Yemot {extension_path} update failed:", redact(setup_error)[:300], flush=True)
client = genai.Client(api_key=GEMINI_API_KEY, http_options=types.HttpOptions(timeout=20000))
_clients = {}
_tls = threading.local()


def client_for(phone):
    key = all_keys().get(phone)
    if not key:
        try:
            key = key_pool_assign(phone)
        except Exception as e:
            print("key pool assignment failed:", redact(e), flush=True)
            key = ""
        if not key:
            db_notify_key_pool_empty(phone)
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


@app.route("/")
def home():
    # Render's base URL should open the admin UI instead of returning a 404.
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html"), mimetype="text/html")


# היסטוריית שיחה (טקסט בלבד) ומצב עיבוד, לפי מזהה שיחה
histories = {}
calls = {}  # call_id -> {"turn": int, "last": str, "status": "idle|processing", "answer": str, "t": float}
lock = threading.Lock()
_history_loaded = set()
_caller_loaded = set()


_db_ready = False
_db_down_until = 0.0  # אחרי כשל חיבור לא מנסים שוב כמה שניות, כדי שלא לתקוע שיחות
_made_dirs = set()    # תיקיות ימות שכבר נוצרו בתהליך הזה


def db_init(conn):
    # יוצר את הטבלאות הדרושות אם אינן קיימות, כדי שחיבור מסד נתונים ריק יעבוד מיד
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS caller_profiles (
                phone VARCHAR(30) PRIMARY KEY,
                display_name TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT TRUE,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("""CREATE TABLE IF NOT EXISTS call_transcripts (
                id BIGSERIAL PRIMARY KEY,
                call_id TEXT NOT NULL DEFAULT '',
                phone VARCHAR(30) NOT NULL DEFAULT '',
                caller_name TEXT NOT NULL DEFAULT '',
                heard TEXT NOT NULL DEFAULT '',
                answer TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("ALTER TABLE call_transcripts ADD COLUMN IF NOT EXISTS assistant_mode TEXT NOT NULL DEFAULT 'smart'")
            cur.execute("CREATE INDEX IF NOT EXISTS call_transcripts_phone_idx ON call_transcripts(phone, created_at DESC)")
            cur.execute("""CREATE TABLE IF NOT EXISTS gemini_key_pool (
                id BIGSERIAL PRIMARY KEY,
                label TEXT NOT NULL DEFAULT '',
                encrypted_key TEXT NOT NULL,
                key_fingerprint TEXT NOT NULL UNIQUE,
                assigned_phone VARCHAR(30),
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                assigned_at TIMESTAMPTZ,
                disabled BOOLEAN NOT NULL DEFAULT FALSE)""")
            cur.execute("""CREATE TABLE IF NOT EXISTS admin_notifications (
                id BIGSERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                phone VARCHAR(30) NOT NULL DEFAULT '',
                message TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                resolved_at TIMESTAMPTZ)""")
            cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS admin_notifications_open_unique
                ON admin_notifications(kind, phone) WHERE resolved_at IS NULL""")
            cur.execute("""CREATE TABLE IF NOT EXISTS voice_forum_posts (
                id BIGSERIAL PRIMARY KEY,
                phone VARCHAR(30) NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT 'משתמש',
                audio_path TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("CREATE INDEX IF NOT EXISTS voice_forum_posts_created_idx ON voice_forum_posts(created_at DESC)")
            cur.execute("""CREATE TABLE IF NOT EXISTS forum_content_posts (
                id BIGSERIAL PRIMARY KEY,
                source_call_id TEXT NOT NULL DEFAULT '',
                phone VARCHAR(30) NOT NULL DEFAULT '',
                display_name TEXT NOT NULL DEFAULT 'משתמש',
                title TEXT NOT NULL DEFAULT 'תוכן חדש',
                content_type TEXT NOT NULL DEFAULT 'text',
                content_text TEXT NOT NULL DEFAULT '',
                audio_path TEXT NOT NULL DEFAULT '',
                image_data BYTEA,
                image_mime TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())""")
            cur.execute("CREATE INDEX IF NOT EXISTS forum_content_posts_created_idx ON forum_content_posts(created_at DESC)")
            cur.execute("""CREATE TABLE IF NOT EXISTS admin_audio_files (
                project_key TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL DEFAULT 'audio/mpeg',
                data BYTEA NOT NULL,
                size_bytes BIGINT NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                PRIMARY KEY (project_key, filename))""")
            cur.execute("CREATE INDEX IF NOT EXISTS admin_audio_files_project_created_idx ON admin_audio_files(project_key, created_at DESC)")
    db_ensure_profile_columns(conn)
    conn.commit()


def db_connect():
    global _db_ready
    global _db_down_until
    if not DATABASE_URL or psycopg is None:
        return None
    if time.time() < _db_down_until:
        return None
    try:
        conn = psycopg.connect(DATABASE_URL, connect_timeout=5)
    except Exception as e:
        _db_down_until = time.time() + 20
        print("database connection failed:", redact(e), flush=True)
        return None
    if not _db_ready:
        try:
            db_init(conn)
            _db_ready = True
            print("database tables ready", flush=True)
        except Exception as e:
            print("database init failed:", redact(e), flush=True)
    return conn


def validate_gemini_key_pool(conn):
    """Read-only check that the configured Fernet key can decrypt every stored Gemini key."""
    try:
        cipher = _fernet()
    except Exception as e:
        print(
            f"Gemini key pool decryption check: invalid encryption key format ({type(e).__name__})",
            flush=True,
        )
        return
    if cipher is None:
        print("Gemini key pool decryption check: skipped (encryption key missing)", flush=True)
        return

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT encrypted_key FROM gemini_key_pool ORDER BY id")
            rows = cur.fetchall()
    except Exception as e:
        print(f"Gemini key pool decryption check: database read failed ({type(e).__name__})", flush=True)
        return

    passed = 0
    failed = 0
    invalid_tokens = 0
    for (encrypted,) in rows:
        try:
            cipher.decrypt(str(encrypted).encode("ascii"))
            passed += 1
        except Exception as e:
            failed += 1
            if type(e).__name__ == "InvalidToken":
                invalid_tokens += 1

    print(
        f"Gemini key pool decryption check: {passed}/{len(rows)} passed; "
        f"{failed} failed (InvalidToken={invalid_tokens})",
        flush=True,
    )


def db_notify_key_pool_empty(phone):
    conn = db_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS admin_notifications (
                    id BIGSERIAL PRIMARY KEY,
                    kind TEXT NOT NULL,
                    phone VARCHAR(30) NOT NULL DEFAULT '',
                    message TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                    resolved_at TIMESTAMPTZ
                )""")
                cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS admin_notifications_open_unique
                    ON admin_notifications(kind, phone) WHERE resolved_at IS NULL""")
                cur.execute("""INSERT INTO admin_notifications(kind, phone, message)
                    VALUES ('key_pool_empty', %s, %s)
                    ON CONFLICT (kind, phone) WHERE resolved_at IS NULL DO NOTHING""",
                    (str(phone or '')[:30], 'אין מפתח Gemini פנוי להקצאה. יש להוסיף מפתח למאגר כדי להעניק למתקשר מכסה אישית.'))
        return True
    except Exception as e:
        print("admin notification save failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def db_notify_runtime_error(phone, message):
    conn = db_connect()
    if conn is None:
        return False
    try:
        clean_phone = re.sub(r"[^0-9]", "", str(phone or ""))[:30]
        clean_message = redact(message or "שגיאת מערכת")[:500]
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO admin_notifications(kind, phone, message)
                       VALUES ('runtime_error', %s, %s)
                       ON CONFLICT (kind, phone) WHERE resolved_at IS NULL DO UPDATE
                       SET message = EXCLUDED.message, created_at = now()""",
                    (clean_phone, clean_message))
        return True
    except Exception as e:
        print("runtime error notification failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def db_list_notifications():
    conn = db_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute("""SELECT id, kind, phone, message, created_at
                FROM admin_notifications WHERE resolved_at IS NULL
                ORDER BY created_at DESC LIMIT 50""")
            return [{"id": r[0], "kind": r[1], "phone": r[2], "message": r[3],
                     "created_at": r[4].isoformat()} for r in cur.fetchall()]
    except Exception as e:
        print("admin notification list failed:", redact(e), flush=True)
        return []
    finally:
        conn.close()


def db_resolve_notification(notification_id):
    conn = db_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE admin_notifications SET resolved_at=now() WHERE id=%s AND resolved_at IS NULL",
                            (notification_id,))
                return cur.rowcount > 0
    except Exception as e:
        print("admin notification resolve failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def db_ensure_profile_columns(conn):
    """Additive profile migration; safe to run against existing installations."""
    if _db_ready:
        return  # כבר בוצע בהפעלה הראשונה של החיבור
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS assigned_address TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS email_address TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS email_enabled BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS chat_enabled BOOLEAN NOT NULL DEFAULT FALSE")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS assistant_mode TEXT NOT NULL DEFAULT 'smart'")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS tools_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS history_enabled BOOLEAN NOT NULL DEFAULT TRUE")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS pin_hash TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS password_hash TEXT NOT NULL DEFAULT ''")
        cur.execute("ALTER TABLE caller_profiles ADD COLUMN IF NOT EXISTS password_services TEXT NOT NULL DEFAULT 'website'")


def db_save_caller(phone, profile):
    conn = db_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                db_ensure_profile_columns(conn)
                cur.execute(
                    """INSERT INTO caller_profiles
                       (phone, display_name, enabled, assigned_address, email_address, email_enabled, chat_enabled, assistant_mode, tools_enabled, history_enabled, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                       ON CONFLICT (phone) DO UPDATE SET
                         display_name = EXCLUDED.display_name,
                         enabled = EXCLUDED.enabled,
                         assigned_address = EXCLUDED.assigned_address,
                         email_address = EXCLUDED.email_address,
                         email_enabled = EXCLUDED.email_enabled,
                         chat_enabled = EXCLUDED.chat_enabled,
                         assistant_mode = EXCLUDED.assistant_mode,
                         tools_enabled = EXCLUDED.tools_enabled,
                         history_enabled = EXCLUDED.history_enabled,
                         updated_at = now()""",
                    (phone, profile.get("name", ""), profile.get("enabled", True),
                     profile.get("assigned_address", ""), profile.get("email_address", ""),
                     bool(profile.get("email_enabled")), bool(profile.get("chat_enabled")),
                     profile.get("assistant_mode", "smart"), bool(profile.get("tools_enabled", True)),
                     bool(profile.get("history_enabled", True))),
                )
        return True
    except Exception as e:
        print("database caller save failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def db_delete_caller(phone):
    _caller_loaded.discard(phone)
    conn = db_connect()
    if conn is None:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM caller_profiles WHERE phone = %s", (phone,))
        return True
    except Exception as e:
        print("database caller delete failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def db_load_caller(phone):
    if not phone or phone in _caller_loaded:
        return
    conn = db_connect()
    if conn is None:
        return
    try:
        with conn.cursor() as cur:
            db_ensure_profile_columns(conn)
            cur.execute(
                """SELECT display_name, enabled, assigned_address, email_address, email_enabled, chat_enabled, assistant_mode, tools_enabled, history_enabled
                   FROM caller_profiles WHERE phone = %s""",
                (phone,),
            )
            row = cur.fetchone()
        conn.commit()  # Persist additive schema migration after successful read.
        if row:
            current = dict(_store["callers"].get(phone) or {})
            current["name"] = row[0] or current.get("name", "")
            current["enabled"] = bool(row[1])
            current["assigned_address"] = row[2] or ""
            current["email_address"] = row[3] or ""
            current["email_enabled"] = bool(row[4]) or bool(current.get("mail"))
            current["mail"] = bool(row[4]) or bool(current.get("mail"))
            current["chat_enabled"] = bool(row[5])
            current["assistant_mode"] = row[6] or "smart"
            current["tools_enabled"] = bool(row[7])
            current["history_enabled"] = bool(row[8])
            _store["callers"][phone] = current
        else:
            # Migrate an existing locally/webhook-stored caller profile on first contact.
            current = _store["callers"].get(phone)
            if current:
                db_save_caller(phone, current)
        _caller_loaded.add(phone)
    except Exception as e:
        print("database caller load failed:", redact(e), flush=True)
    finally:
        conn.close()


def db_ensure_transcript_columns(conn):
    """Add assistant separation without deleting or rewriting existing transcripts."""
    if _db_ready:
        return
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE call_transcripts ADD COLUMN IF NOT EXISTS assistant_mode TEXT NOT NULL DEFAULT 'smart'")


def db_load_history(phone, assistant_mode="smart"):
    if caller(phone).get("history_enabled", True) is False:
        return []
    conn = db_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            db_ensure_transcript_columns(conn)
            cur.execute(
                """SELECT heard, answer FROM call_transcripts
                   WHERE phone = %s AND assistant_mode = %s
                   ORDER BY created_at DESC, id DESC LIMIT 6""",
                (phone, assistant_mode),
            )
            rows = cur.fetchall()
        conn.commit()
        return list(reversed(rows))
    except Exception as e:
        print("database history load failed:", redact(e), flush=True)
        return []
    finally:
        conn.close()


def db_load_recent_history(phone, limit=6):
    """Load the latest conversation turns across assistants for a returning caller."""
    if not phone or caller(phone).get("history_enabled", True) is False:
        return []
    conn = db_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            db_ensure_transcript_columns(conn)
            cur.execute(
                """SELECT heard, answer FROM call_transcripts
                   WHERE phone = %s
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (phone, max(1, min(int(limit), 12))),
            )
            rows = cur.fetchall()
        conn.commit()
        return list(reversed(rows))
    except Exception as e:
        print("recent conversation history load failed:", redact(e), flush=True)
        return []
    finally:
        conn.close()


def db_save_transcript(call_id, phone, heard, answer, assistant_mode="smart"):
    if not DATABASE_URL or not phone or not heard or caller(phone).get("history_enabled", True) is False:
        return False
    conn = db_connect()
    if conn is None:
        return False
    try:
        profile = caller(phone)
        with conn:
            with conn.cursor() as cur:
                db_ensure_transcript_columns(conn)
                cur.execute(
                    """INSERT INTO call_transcripts
                       (call_id, phone, caller_name, heard, answer, assistant_mode)
                       VALUES (%s, %s, %s, %s, %s, %s)""",
                    (str(call_id)[:200], phone[:30], profile.get("name", "")[:40],
                     heard[:5000], answer[:5000], assistant_mode[:40]),
                )
        return True
    except Exception as e:
        print("database transcript save failed:", redact(e), flush=True)
        return False
    finally:
        conn.close()


def yemot_history_folder(assistant_mode):
    mode = assistant_mode if assistant_mode in {"smart", "edgy", "psychologist", "yeshiva", "yiddish", "tools", "astrology", "central"} else "smart"
    base = "/1/1" if getattr(_tls, "yiddish", False) else "/8"
    phone = re.sub(r"[^0-9]", "", str(getattr(_tls, "phone", "") or ""))[:15]
    return f"{base}/history/{mode}/{phone}"


def archive_yemot_history(phone, assistant_mode, call_id, heard, answer, use_yiddish=False):
    """Write one UTF-8 transcript file to a per-assistant/per-caller Yemot folder."""
    try:
        mode = assistant_mode if assistant_mode in {"smart", "edgy", "psychologist", "yeshiva", "yiddish", "tools", "astrology", "central"} else "smart"
        base = "/1/1" if use_yiddish else "/8"
        clean_phone = re.sub(r"[^0-9]", "", str(phone or ""))[:15]
        if not clean_phone:
            return False
        folder = f"{base}/history/{mode}/{clean_phone}"
        token = f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}"
        # Create each level explicitly; Ym_MakeDir is idempotent for existing folders.
        for part_count in range(1, len(folder.strip("/").split("/")) + 1):
            partial = "/" + "/".join(folder.strip("/").split("/")[:part_count])
            if partial in _made_dirs:
                continue
            _made_dirs.add(partial)
            try:
                requests.get("https://www.call2all.co.il/ym/api/Ym_MakeDir",
                             params={"token": token, "path": "ivr2:" + partial}, timeout=12)
            except Exception:
                pass
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        safe_call = re.sub(r"[^0-9A-Za-z_-]", "", str(call_id))[:24] or "call"
        filename = f"{stamp}-{int(time.time() * 1000) % 1000000:06d}-{safe_call}.txt"
        content = (f"assistant: {mode}\nphone: {clean_phone}\ncall: {safe_call}\n\n"
                   f"מתקשר: {heard[:5000]}\nעוזר: {answer[:5000]}\n")
        response = requests.post(
            "https://www.call2all.co.il/ym/api/UploadFile",
            data={"token": token, "path": "ivr2:" + folder + "/" + filename, "convertAudio": "0"},
            files={"file": (filename, content.encode("utf-8"), "text/plain; charset=utf-8")},
            timeout=25,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("responseStatus") != "OK":
            raise RuntimeError(result.get("message", result.get("responseStatus", "Yemot upload failed")))
        return True
    except Exception as e:
        print("Yemot transcript archive failed:", redact(e)[:180], flush=True)
        return False


def db_admin_recent_transcripts(limit=5):
    conn = db_connect()
    if conn is None:
        return []
    try:
        with conn.cursor() as cur:
            db_ensure_transcript_columns(conn)
            cur.execute(
                """SELECT phone, caller_name, heard, answer, created_at, assistant_mode
                   FROM call_transcripts ORDER BY created_at DESC, id DESC LIMIT %s""",
                (max(1, min(int(limit), 10)),),
            )
            rows = cur.fetchall()
        conn.commit()
        return [{"phone": r[0] or "", "name": r[1] or "", "heard": r[2] or "",
                 "answer": r[3] or "", "created_at": r[4].strftime("%d/%m %H:%M") if r[4] else "",
                 "assistant_mode": r[5] or "smart"}
                for r in rows]
    except Exception as e:
        print("admin transcript query failed:", redact(e), flush=True)
        return []
    finally:
        conn.close()


def clean_for_tts(text):
    # משאירים רק אותיות, ספרות ורווחים. סימנים מיוחדים עלולים לשבש את הפקודה
    text = re.sub(r"[^0-9A-Za-z֐-׿ ]", " ", text)
    return " ".join(text.split())[:700]


def active_record_path():
    return getattr(_tls, "record_path", RECORD_PATH)


def active_extension_path():
    return getattr(_tls, "extension_path", EXTENSION_PATH)


def read_command(prompt_text, call_id, turn):
    # מקריא טקסט ואז מקליט. ההקלטה מסתיימת בהקשת סולמית (#). שם הקובץ שונה בכל סבב.
    fname = "q" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10] + str(turn)
    return f"read=t-{clean_for_tts(prompt_text)}=question,no,record,{active_record_path()},{fname},no,yes,no"


def wait_command():
    # מנגינת המתנה (או הודעה) ואז חזרה לשלוחה, שתבדוק אם התשובה מוכנה
    music = cfg("music_file_yi" if yiddish_mode() else "music_file_he") or cfg("music_file")
    if music:
        return f"id_list_message=f-{music}&go_to_folder={active_extension_path()}"
    return f"id_list_message=t-נא להמתין&go_to_folder={active_extension_path()}"


def admin_digits_command(prompt, field, maximum=10, minimum=0):
    # Yemot read action: variable name, max digits, min digits, keypad mode.
    return f"read=t={clean_for_tts(prompt)}={field},,{maximum},{minimum},Digits,yes"


def admin_menu_text(message=""):
    prefix = (message + ". ") if message else ""
    enabled = "פעיל" if cfg("system_enabled") else "כבוי"
    voice = "מופעל" if voice_on() else "כבוי"
    return (prefix + f"תפריט ניהול. מצב העוזר: {enabled}. קול: {voice}. "
            "לבדיקת מצב הקש 1. להפעלה או כיבוי של העוזר הקש 2. למענה קולי הקש 3. "
            "להחלפת קול ברירת המחדל הקש 4. להתראות ושגיאות הקש 5. לתמלולי שיחות אחרונות הקש 6. "
            "לחסימת משתמש הקש 7. ולפתיחת חסימה הקש 8. לחזרה לאזור האישי הקש 0. "
            "לחזרה לתפריט הקודם הקש כוכבית. לחזרה לתפריט הראשי הקש סולמית.")


def admin_status_text():
    active = sum(1 for item in calls.values() if time.time() - item.get("t", 0) < 120)
    state = "פעיל" if cfg("system_enabled") else "כבוי"
    voice = "מופעל" if voice_on() else "כבוי"
    return f"מצב העוזר {state}. המענה הקולי {voice}. מספר שיחות פעילות כרגע {active}."


EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
MAIL_READ_PROMPT = "ענה בעברית בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים."


def mail_json(payload, owner=False):
    url, secret = mail_target(owner)
    r = requests.post(url, json=dict(payload, secret=secret), timeout=25)
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
    hook_url, hook_secret = mail_target()
    if hook_url and hook_secret:
        r = requests.post(
            hook_url,
            json={"secret": hook_secret, "action": "send", "to": to_addr, "subject": subject, "body": body},
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
    if mail_target()[0]:
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


def send_mail_attachment(to_addr, subject, body, filename, mime_type, content_b64):
    subject = (subject or "הודעה")[:200]
    body = (body or "")[:5000]
    filename = re.sub(r"[^0-9A-Za-z._-א-ת]", "_", str(filename or "attachment.bin"))[:120]
    content_b64 = str(content_b64 or "")
    if not content_b64 or len(content_b64) > 12_000_000:
        raise ValueError("קובץ מצורף ריק או גדול מדי")
    hook_url, hook_secret = mail_target()
    if hook_url and hook_secret:
        r = requests.post(
            hook_url,
            json={"secret": hook_secret, "action": "send_attachment", "to": to_addr,
                  "subject": subject, "body": body, "filename": filename,
                  "mimeType": mime_type or "application/octet-stream", "content": content_b64},
            timeout=45,
        )
        r.raise_for_status()
        try:
            result = r.json()
        except Exception:
            result = {"ok": r.text.strip() == "ok"}
        if not result.get("ok"):
            raise RuntimeError("שירות המייל לא אישר שליחת קובץ מצורף")
        return
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": BREVO_API_KEY, "content-type": "application/json"},
        json={
            "sender": {"email": MAIL_FROM, "name": "העוזר החכם"},
            "to": [{"email": to_addr}],
            "subject": subject,
            "textContent": body,
            "attachment": [{"name": filename, "content": content_b64}],
        },
        timeout=30,
    )
    r.raise_for_status()


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
    attachment = pend.get("attachment")
    if attachment:
        send_mail_attachment(pend["addr"], pend["subject"], pend["body"],
                             attachment.get("filename"), attachment.get("mime_type"),
                             attachment.get("content"))
    else:
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
    if act == "read_attachment":
        try:
            idx = int(mail.get("index") or 1)
            attachment_idx = int(mail.get("attachment_index") or 1)
        except Exception:
            idx, attachment_idx = 1, 1
        with lock:
            st = calls.get(call_id)
            ids = list(st.get("mails", [])) if st else []
        if not ids or idx < 1 or idx > len(ids):
            return "קודם בקש רשימת מיילים עם קבצים מצורפים"
        result = mail_json({"action": "attachments", "id": ids[idx - 1]})
        attachments = result.get("attachments", [])
        if not attachments:
            return "לא נמצא קובץ מצורף במייל הזה"
        if attachment_idx < 1 or attachment_idx > len(attachments):
            return "מספר הקובץ המצורף אינו תקין"
        attachment = attachments[attachment_idx - 1]
        try:
            raw = base64.b64decode(attachment.get("data") or "", validate=True)
        except Exception:
            return "לא הצלחתי לפתוח את הקובץ המצורף"
        if not raw or len(raw) > 6 * 1024 * 1024:
            return "הקובץ ריק או גדול מדי לקריאה דרך השלוחה"
        mime_type = (attachment.get("mimeType") or "application/octet-stream").split(";")[0]
        filename = attachment.get("filename") or "קובץ מצורף"
        prompt = (
            "קרא את הקובץ המצורף בשם " + filename + ". בקשת המשתמש: " + (heard or "סכם את הקובץ") +
            ". ענה בעברית, הבדל בין מה שכתוב בקובץ לבין מסקנות שלך, ואל תמציא פרטים."
        )
        out = call_model(
            [types.Part.from_bytes(data=raw, mime_type=mime_type), types.Part(text=prompt)],
            types.GenerateContentConfig(
                system_instruction="אתה קורא מסמכים. הסתמך רק על תוכן הקובץ, ציין אם חלק ממנו אינו קריא, וענה בעברית ברורה המתאימה להקראה.",
                max_output_tokens=900,
            ),
        )
        return (out.text or "").strip() or "לא הצלחתי לקרוא את הקובץ"
    return "לא הבנתי מה לעשות עם המיילים"


def normalize_recording_path(path):
    # Yemot may return a filename or a path that already includes the extension folder.
    # Strip repeated copies to prevent /8/8 and /1/1/1/1.
    folder = "/" + "/".join(part for part in str(active_record_path()).split("/") if part)
    relative = str(path or "").strip().replace("\\", "/")
    if relative.startswith("ivr2:"):
        relative = relative[5:]
    relative = "/" + relative.lstrip("/")
    while relative.startswith(folder + "/"):
        relative = relative[len(folder):]
    relative = relative.lstrip("/")
    if not relative:
        raise ValueError("recording filename is empty")
    if not relative.lower().endswith(".wav"):
        relative += ".wav"
    return "ivr2:" + folder + "/" + relative


def download_recording(path):
    yemot_path = normalize_recording_path(path)
    r = requests.get(
        "https://www.call2all.co.il/ym/api/DownloadFile",
        params={"token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}", "path": yemot_path},
        timeout=20,
    )
    r.raise_for_status()
    if not r.content:
        raise RuntimeError("Yemot returned an empty recording")
    return r.content

# Keep only known current fallback IDs; the old gemini-3.8-flash-lite ID
# returned 404 and must not be retried, even if it remains in an env override.
_UNAVAILABLE_FALLBACK_MODELS = {"gemini-3.8-flash-lite"}
FALLBACK_MODELS = [
    x.strip()
    for x in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.5-flash").split(",")
    if x.strip() and x.strip() not in _UNAVAILABLE_FALLBACK_MODELS
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
            print("list models failed:", redact(e), flush=True)
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
                    print(f"gemini {name} attempt {attempt} failed: {redact(msg)[:200]}", flush=True)
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


# Prefer current stable TTS models, then fall back to supported legacy TTS endpoints
# for API projects where the newest model has not yet propagated.
TTS_MODELS = list(dict.fromkeys(
    ([TTS_MODEL] if TTS_MODEL else [])
    + [
        "gemini-3.8-flash-tts",
        "gemini-3.8-flash-lite-tts",
    ]
))
TTS_STYLE = "A warm, gentle, calming and reassuring voice, soft and friendly, speaking Hebrew clearly at a relaxed slightly slow pace, with natural pauses and a soothing conversational tone. Never rushed, sharp, or overly theatrical."
YIDDISH_TTS_STYLE = "A warm, wise, reassuring Hasidic yeshiva mentor voice: calm, deeply empathetic, thoughtful and grounded, like a trusted spiritual mentor with psychological insight. Speak only natural everyday Yiddish, with authentic pronunciation, a gentle measured pace, thoughtful pauses, and sincere warmth. Do not use English, foreign loanwords, or mixed-language expressions. Avoid theatrical preaching or exaggerated emotion."


MODE_TTS_STYLES = {
    "edgy": "A dry, witty, sarcastic voice with a playful smirk, quick and a little mocking, natural conversational Hebrew.",
    "psychologist": "A warm, soft, gentle female therapist voice: slow, calm, reassuring, with empathic pauses and a caring tone, speaking Hebrew.",
    "yeshiva": "An elderly, wise, fatherly rosh yeshiva voice: deep, calm, thoughtful and warm, measured pace, with traditional yeshiva intonation, speaking Hebrew.",
    "tools": "A clear, efficient, friendly and neutral voice at a natural pace, speaking Hebrew.",
    "astrology": "A calm, intriguing, thoughtful Hebrew voice with a measured pace, clear pronunciation, and a subtle sense of wonder; grounded and informative, never mystical or theatrical.",
    "personal": "A warm, clear, respectful Hebrew voice, calm and welcoming, with a natural conversational pace.",
}
MODE_VOICES = {"smart": "Sulafat", "edgy": "Puck", "psychologist": "Vindemiatrix", "yeshiva": "Charon", "yiddish": "Schedar", "tools": "Kore", "astrology": "Algieba", "personal": "Achird"}


def voice_index_for(phone, mode=""):
    v = caller(phone).get("voice")
    if v in VOICES:
        return VOICES.index(v)
    mv = MODE_VOICES.get(mode)
    if mv in VOICES:
        return VOICES.index(mv)
    return default_voice_index()


def tts_wav(text, voice):
    last_err = None
    for name in (getattr(_tls, "tts_models_override", None) or TTS_MODELS):
        if _is_bad(name):
            continue
        try:
            style = (
                "Speak Hebrew in a clear, warm, natural adult male voice, at a measured pace, with crisp pronunciation and short pauses. This is a telephone system announcement."
                if getattr(_tls, "voicepack_tts", False)
                else MODE_TTS_STYLES.get(getattr(_tls, "assistant_mode", ""), "") or (YIDDISH_TTS_STYLE if yiddish_mode() else TTS_STYLE)
            )
            interaction = cur_client().interactions.create(
                model=name,
                input=[{
                    "type": "user_input",
                    "content": [{
                        "type": "text",
                        "text": str(text or "").strip(),
                        "annotations": [{
                            "type": "speech_metadata",
                            "style": style,
                        }],
                    }],
                }],
                response_format={"type": "audio"},
                generation_config={
                    "speech_config": [
                        {"voice": voice},
                    ]
                },
            )
            audio = getattr(interaction, "output_audio", None)
            encoded = getattr(audio, "data", None) if audio else None
            if not encoded:
                raise RuntimeError("Gemini TTS returned no audio data")
            data = base64.b64decode(encoded) if isinstance(encoded, str) else encoded
            # Gemini 3.8 unary TTS returns a complete WAV (RIFF) file.
            if data[:4] == b"RIFF":
                return data
            raise RuntimeError("Gemini TTS returned audio without a WAV header")
        except Exception as e:
            last_err = e
            msg = str(e)
            print(f"tts {name} failed: {redact(msg)[:200]}", flush=True)
            if any(x in msg for x in ("429", "RESOURCE_EXHAUSTED", "404", "NOT_FOUND")):
                _mark_bad(name, e)
    raise last_err or RuntimeError("no tts model available")

def upload_to_yemot(wav_bytes, name):
    r = requests.post(
        "https://www.call2all.co.il/ym/api/UploadFile",
        data={
            "token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}",
            "path": f"ivr2:{active_record_path().rstrip('/')}/{name}.wav",
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
    return f"{active_record_path().rstrip('/')}/{name}"


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
    # Keep hold music beside the recordings, independently of the return-to-menu path.
    folder = RECORD_PATH.rstrip("/") or "/"
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
        print("Yemot hold music setup failed:", redact(music_error)[:300], flush=True)


WIKI_HEADERS = {"User-Agent": "YemotAIAssistant/1.0 (personal phone assistant)"}
SHORT_PROMPT = "ענה בעברית בשניים עד שלושה משפטים קצרים, בלי רשימות, כוכביות, אימוג'ים או סימנים מיוחדים."
YIDDISH_SHORT_PROMPT = (
    "ענטפער אין צוויי ביז דריי קורצע זאצן אין נאטירלעכער, גערעדטער יידיש, ווי א ישיבישער משפיע מיט א פסיכאלאגישער פארשטאנד. "
    "ניץ בלויז פשוטע יידישע ווערטער, אן ענגליש און אן לעז. "
    "זיי ווארעם, קלאר און געב א קליינעם מעגליכן שריט ווען עס איז נוצלעך. ניץ יידישע אותיות."
)
YIDDISH_WEB_RESEARCH_PROMPT = (
    "פיר א גוטן פארשונג באזירט אויף Google Search רעזולטאטן. פארגלייך פארלעסלעכע מקורות. "
    "ענטפער אין נאטירלעכער גערעדטער יידיש, מיט א ישיביש-משפיע'דיגן טאן: קלאר, ווארעם און פארשטענדליך. "
    "ביים מסביר זיין מענטשלעכע אדער פסיכאלאגישע ענינים, דערקלער אלעס אין פשוטער, נאטירלעכער יידיש, אן אויסלענדישע ווערטער. "
    "האלט זיך צו די מקורות, אונטערשייד צווישן פאקטן און מיינונגען, און ערפינד נישט."
)

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
        types.GenerateContentConfig(system_instruction=(YIDDISH_SHORT_PROMPT if yiddish_mode() else SHORT_PROMPT), max_output_tokens=300),
    )
    return (res.text or "").strip()


def web_lookup(question):
    res = call_model(
        question,
        types.GenerateContentConfig(
            system_instruction=(YIDDISH_WEB_RESEARCH_PROMPT if yiddish_mode() else WEB_RESEARCH_PROMPT),
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
        print("lookup error:", redact(e), flush=True)
        return ""


def _map_local_datetime(value):
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing map time")
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo("Asia/Jerusalem"))
    return parsed.astimezone(ZoneInfo("Asia/Jerusalem"))


def compute_map_route(map_request):
    """Return verified, provider-calculated route steps; never improvise directions."""
    if not GOOGLE_MAPS_ROUTES_ENABLED or not GOOGLE_MAPS_API_KEY:
        return (
            "כלי המפות עדיין לא הופעל. חישוב מסלולים משתמש ב-Google Maps Routes API ועלול להיות כרוך בתשלום. "
            "כדי להפעיל אותו, מנהל המערכת צריך להגדיר GOOGLE_MAPS_API_KEY ולהפעיל במפורש "
            "את GOOGLE_MAPS_ROUTES_ENABLED."
        )
    origin = str(map_request.get("origin") or "").strip()[:300]
    destination = str(map_request.get("destination") or "").strip()[:300]
    mode = str(map_request.get("travel_mode") or "").strip().upper()
    time_kind = str(map_request.get("time_kind") or "").strip().lower()
    if not origin or not destination or mode not in ("TRANSIT", "WALK") or time_kind not in ("departure", "arrival"):
        return "חסרים פרטי מסלול. אמור במשפט אחד מוצא, יעד, שעת יציאה או הגעה, והאם לנסוע בתחבורה ציבורית או ללכת ברגל."
    try:
        requested_time = _map_local_datetime(map_request.get("time_value"))
    except Exception:
        return "לא הצלחתי להבין את מועד הנסיעה. אמור תאריך ושעה ברורים ליציאה או להגעה."
    body = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": mode,
        "languageCode": "he-IL",
        "regionCode": "IL",
        "units": "METRIC",
        "computeAlternativeRoutes": False,
    }
    # Google transit routing supports either arrivalTime or departureTime.
    # Walking duration is returned as a route estimate; arrival-time requests are
    # converted to an estimated departure after the route duration is known.
    if mode == "TRANSIT":
        body["arrivalTime" if time_kind == "arrival" else "departureTime"] = requested_time.isoformat(timespec="seconds")
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": (
            "routes.duration,routes.distanceMeters,routes.localizedValues,"
            "routes.legs.steps.navigationInstruction.instructions,"
            "routes.legs.steps.travelMode,routes.legs.steps.localizedValues,"
            "routes.legs.steps.transitDetails"
        ),
    }
    try:
        response = requests.post(
            "https://routes.googleapis.com/directions/v2:computeRoutes",
            json=body,
            headers=headers,
            timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        routes = payload.get("routes") or []
        if not routes:
            return "לא נמצא מסלול מתאים בין המוצא ליעד בזמן שביקשת. בדוק את הכתובות או נסה שעה אחרת."
        route = routes[0]
        duration_raw = str(route.get("duration") or "")
        duration_match = re.match(r"^(\d+(?:\.\d+)?)s$", duration_raw)
        duration_seconds = int(float(duration_match.group(1))) if duration_match else 0
        minutes = max(1, round(duration_seconds / 60)) if duration_seconds else 0
        distance_m = int(route.get("distanceMeters") or 0)
        distance_label = (f"{distance_m / 1000:.1f} קילומטרים" if distance_m >= 1000
                          else (f"{distance_m} מטרים" if distance_m else "מרחק לא זמין"))
        localized = route.get("localizedValues") or {}
        duration_label = ((localized.get("duration") or {}).get("text") or
                          (f"כ-{minutes} דקות" if minutes else "משך זמן לא זמין"))
        steps = (route.get("legs") or [{}])[0].get("steps") or []
        instructions = []
        for step in steps:
            transit = step.get("transitDetails") or {}
            if transit:
                line = transit.get("transitLine") or {}
                stops = transit.get("stopDetails") or {}
                departure_stop = (stops.get("departureStop") or {}).get("name") or "התחנה הקרובה"
                arrival_stop = (stops.get("arrivalStop") or {}).get("name") or "תחנת היעד"
                line_name = line.get("nameShort") or line.get("shortName") or line.get("name") or "תחבורה ציבורית"
                vehicle = (line.get("vehicle") or {}).get("name") or ""
                headsign = transit.get("headsign") or ""
                detail = f"עלה על {vehicle + ' ' if vehicle else ''}{line_name} בתחנת {departure_stop}"
                if headsign:
                    detail += f", בכיוון {headsign}"
                detail += f", ורד בתחנת {arrival_stop}"
                instructions.append(detail)
                continue
            instruction = ((step.get("navigationInstruction") or {}).get("instructions") or "").strip()
            if instruction:
                instruction = re.sub(r"<[^>]*>", " ", instruction)
                instruction = html_module.unescape(" ".join(instruction.split()))
                localized_step = step.get("localizedValues") or {}
                step_distance = (localized_step.get("distance") or {}).get("text")
                if step_distance:
                    instruction += f" ({step_distance})"
                instructions.append(instruction)
        if not instructions:
            return "שירות המפות מצא מסלול אך לא החזיר הוראות מפורטות. נסה שוב מאוחר יותר."
        if mode == "WALK" and duration_seconds:
            if time_kind == "arrival":
                departure = requested_time - timedelta(seconds=duration_seconds)
                time_line = f"כדי להגיע בסביבות {requested_time.strftime('%H:%M')}, צא להליכה בסביבות {departure.strftime('%H:%M')}."
            else:
                arrival = requested_time + timedelta(seconds=duration_seconds)
                time_line = f"צא להליכה בשעה {requested_time.strftime('%H:%M')}; ההגעה המשוערת היא בסביבות {arrival.strftime('%H:%M')}."
        elif time_kind == "departure":
            time_line = f"שעת היציאה שביקשת היא {requested_time.strftime('%d/%m/%Y בשעה %H:%M')}."
        else:
            time_line = f"שעת ההגעה שביקשת היא {requested_time.strftime('%d/%m/%Y בשעה %H:%M')}."
        mode_label = "תחבורה ציבורית" if mode == "TRANSIT" else "הליכה ברגל"
        steps = instructions[:14]
        numbered = " ".join(f"שלב {i + 1}: {item}." for i, item in enumerate(steps))
        return (
            f"מצאתי מסלול מ{origin} אל {destination}, באמצעות {mode_label}. "
            f"{time_line} משך משוער {duration_label}, מרחק {distance_label}. "
            f"ההדרכה לפי הסדר: {numbered}"
        )
    except requests.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", 0)
        print("Google Maps route request failed:", status, flush=True)
        if status in (400, 404):
            return "לא הצלחתי לחשב את המסלול. בדוק את שמות המקומות, התאריך והשעה."
        if status in (401, 403):
            return "שירות המפות אינו מאושר לשימוש במפתח שהוגדר. מנהל המערכת צריך לבדוק את הרשאת Routes API."
        return "שירות המפות אינו זמין כרגע. נסה שוב מאוחר יותר."
    except Exception as e:
        print("map route calculation failed:", redact(e)[:180], flush=True)
        return "לא הצלחתי לחשב את המסלול כרגע. נסה שוב מאוחר יותר."


def podcast_tts_wav(dialogue):
    last_err = None
    contents = []
    for item in dialogue:
        speaker = item["speaker"]
        style = "מגיש פודקאסט טבעי, ברור ומעניין; קצב דיבור נעים עם הפסקות טבעיות"
        contents.append({
            "type": "text",
            "text": item["text"],
            "annotations": [{"type": "speech_metadata", "speaker": speaker, "style": style}],
        })
    request_input = [{"type": "user_input", "content": contents}]
    for model_name in TTS_MODELS:
        try:
            interaction = cur_client().interactions.create(
                model=model_name,
                input=request_input,
                response_format={"type": "audio"},
                generation_config={
                    "speech_config": {
                        "speakers": [
                            {"speaker": "Host1", "voice": "Kore"},
                            {"speaker": "Host2", "voice": "Puck"},
                        ]
                    }
                },
            )
            audio = getattr(interaction, "output_audio", None)
            encoded = getattr(audio, "data", None) if audio else None
            if not encoded:
                raise RuntimeError("TTS returned no podcast audio")
            data = base64.b64decode(encoded) if isinstance(encoded, str) else encoded
            if data[:4] != b"RIFF":
                raise RuntimeError("TTS returned audio without WAV header")
            return data
        except Exception as e:
            last_err = e
            print("podcast tts failed:", redact(e)[:200], flush=True)
    raise last_err or RuntimeError("no podcast TTS model available")


def create_podcast_audio(call_id, payload):
    dialogue = payload.get("dialogue") if isinstance(payload, dict) else None
    if not isinstance(dialogue, list) or len(dialogue) < 4:
        return "לא הצלחתי להכין תסריט הסכת תקין. נסה לתאר שוב את הנושא."
    clean = []
    for item in dialogue[:16]:
        if not isinstance(item, dict):
            continue
        speaker = str(item.get("speaker") or "").strip()
        text_value = str(item.get("text") or "").strip()
        if speaker not in ("Host1", "Host2") or not text_value:
            continue
        clean.append({"speaker": speaker, "text": text_value[:900]})
    if len(clean) < 4:
        return "תסריט ההסכת קצר מדי. נסה שוב."
    full_chars = sum(len(x["text"]) for x in clean)
    if full_chars > 9000:
        return "ההסכת ארוך מדי ליצירה אחת. בקש פרק קצר יותר."
    wav = podcast_tts_wav(clean)
    title = str(payload.get("title") or "הסכת חדש")[:100]
    dialogue_text = "\n".join(
        ("מגיש ראשון: " if item["speaker"] == "Host1" else "מגיש שני: ") + item["text"]
        for item in clean
    )
    public_safe = forum_public_content_safe(title, dialogue_text)
    path = forum_upload_audio(wav, call_id) if public_safe else upload_to_yemot(
        wav, "pod" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10]
    )
    published = None
    if public_safe:
        published = save_generated_forum_post(
            call_id, getattr(_tls, "phone", ""), title, dialogue_text,
            content_type="podcast", audio_path=path,
        )
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["generated_audio"] = path
            st["podcast_title"] = title
    if published:
        return "ההסכת נוצר ופורסם בפורום שבאתר ובקו. הקובץ מוכן להאזנה."
    return "ההסכת נוצר. בגלל פרטי קשר או מידע פרטי בתוכן, הוא לא פורסם בפורום, אך הקובץ מוכן להאזנה."


def generate_image_bytes(prompt):
    api_key = all_keys().get(getattr(_tls, "phone", "")) or GEMINI_API_KEY
    image_client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=90000))
    last_err = None
    for model_name in ("gemini-3.1-flash-image-preview", "gemini-2.5-flash-image"):
        try:
            response = image_client.models.generate_content(model=model_name, contents=str(prompt)[:4000])
            for part in (getattr(response, "parts", None) or []):
                inline = getattr(part, "inline_data", None)
                data = getattr(inline, "data", None) if inline else None
                if data:
                    return (base64.b64decode(data) if isinstance(data, str) else data,
                            getattr(inline, "mime_type", None) or "image/png")
            raise RuntimeError("image model returned no image")
        except Exception as e:
            last_err = e
            print("image generation failed:", model_name, redact(e)[:180], flush=True)
    raise last_err or RuntimeError("no image model available")


def prepare_image_email(call_id, phone, action):
    prompt = (action.get("prompt") or "").strip()
    if not prompt:
        return "תאר איזו תמונה ליצור."
    try:
        raw, mime_type = generate_image_bytes(prompt)
    except Exception as e:
        print("image generation failed:", redact(e)[:180], flush=True)
        return "לא הצלחתי ליצור את התמונה כרגע. נסה שוב עם תיאור מעט קצר יותר."
    if len(raw) > 8 * 1024 * 1024:
        return "התמונה שנוצרה גדולה מדי לשמירה ולפרסום."
    published = None
    if action.get("publish_to_forum", True) is not False:
        public_title = str(action.get("public_title") or "תמונה חדשה")[:120]
        public_description = str(
            action.get("public_description") or "תמונה שנוצרה באמצעות בינה מלאכותית."
        )[:3000]
        published = save_generated_forum_post(
            call_id, phone, public_title, public_description,
            content_type="image", image_data=raw, image_mime=mime_type,
        )
    to = (action.get("to") or caller(phone).get("email_address") or "").strip()
    if not to:
        return "התמונה נוצרה" + (" ופורסמה בפורום באתר ובקו." if published else ".") + " כדי לשלוח אותה למייל, אמור את כתובת הנמען."
    if not mail_enabled_for(phone) or not EMAIL_ENABLED:
        return "התמונה נוצרה" + (" ופורסמה בפורום באתר ובקו." if published else ".") + " אבל חיבור המייל אינו פעיל כרגע, ולכן לא נשלחה."
    candidates = resolve_recipient(to)
    if not candidates:
        return "התמונה נוצרה" + (" ופורסמה בפורום." if published else ".") + " לא מצאתי כתובת מייל מתאימה. אמור את כתובת המייל המלאה."
    if len(candidates) > 1:
        options = ", ".join((c.get("name") or "") + " " + speak_email(c["email"]) for c in candidates[:3])
        return "מצאתי כמה כתובות אפשריות: " + options + ". אמור כתובת מלאה."
    filename = "ai-image-" + re.sub(r"[^0-9A-Za-z]", "", str(call_id))[:10] + ".png"
    attachment = {
        "filename": filename,
        "mime_type": mime_type,
        "content": base64.b64encode(raw).decode("ascii"),
    }
    c = candidates[0]
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["pending"] = {
                "addr": c["email"],
                "subject": (action.get("subject") or "תמונה שנוצרה באמצעות בינה מלאכותית")[:200],
                "body": (action.get("body") or "מצורפת התמונה שביקשת ליצור.")[:2000],
                "attachment": attachment,
            }
    target = (c.get("name") + " בכתובת ") if c.get("name") else "לכתובת "
    return "יצרתי את התמונה" + (" ופרסמתי אותה בפורום." if published else ".") + " אני עומד לשלוח אותה " + target + speak_email(c["email"]) + ". האם לשלוח? אמור כן או לא."


def ask_gemini(call_id, audio_bytes, phone=""):
    assistant_mode = getattr(_tls, "assistant_mode", "smart")
    history_key = (call_id, assistant_mode)
    history = histories.setdefault(history_key, [])
    history_enabled = caller(phone).get("history_enabled", True) is not False
    # Load context separately for each assistant. A menu call may start in "menu"
    # and select a different assistant later in the same phone call.
    if phone and history_enabled and history_key not in _history_loaded:
        for heard_old, answer_old in db_load_history(phone, assistant_mode):
            if heard_old:
                history.append(types.Content(role="user", parts=[types.Part(text=heard_old)]))
                history.append(types.Content(role="model", parts=[types.Part(text=answer_old)]))
        history[:] = history[-12:]
        _history_loaded.add(history_key)
    parts = [types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")]
    with lock:
        st0 = calls.get(call_id)
        has_pending = bool(st0 and st0.get("pending"))
    mail_ok = getattr(_tls, "assistant_mode", "smart") in ("smart", "tools", "central") and mail_enabled_for(phone)
    if has_pending and mail_ok:
        parts.append(types.Part(text="הערת מערכת: יש מייל ממתין לאישור המשתמש."))
    contents = list(history) + [types.Content(role="user", parts=parts)]
    res = generate(contents)
    data = json.loads(res.text)
    heard = (data.get("heard") or "").strip()
    answer = (data.get("answer") or "").strip() or "לא הבנתי נסה שוב"
    change_voice = bool(data.get("change_voice")) if voice_on() else False
    lookup = data.get("lookup")
    tools_allowed = caller(phone).get("tools_enabled", True) is not False
    send = data.get("send_email")
    mail = data.get("mail")
    confirm = data.get("confirm_pending")
    create_podcast = data.get("create_podcast")
    create_image = data.get("create_image")
    map_request = data.get("map_request")
    clarification_question = (data.get("clarification_question") or "").strip()
    forum_content = data.get("forum_content")
    return_to_menu = bool(data.get("return_to_menu"))
    tool_action = getattr(_tls, "tool_action", "") or ""
    suppress_actions = False
    with lock:
        tool_state = calls.get(call_id)
        clarification_already_asked = bool(tool_state and tool_state.get("tool_clarifications_done"))
    if assistant_mode == "tools" and tool_action in CREATION_ACTIONS and not clarification_already_asked:
        # Always collect focused details once before making a creative artifact.
        answer = clarification_question or default_tool_clarification(tool_action)
        suppress_actions = True
        with lock:
            tool_state = calls.get(call_id)
            if tool_state is not None:
                tool_state["tool_clarifications_done"] = True
                tool_state["tool_clarification_waiting"] = True
    elif clarification_question and assistant_mode == "tools" and tool_action:
        answer = clarification_question
        suppress_actions = True
        with lock:
            tool_state = calls.get(call_id)
            if tool_state is not None:
                tool_state["tool_clarification_waiting"] = True

    try:
        if suppress_actions:
            pass
        elif mail_ok and (confirm or isinstance(send, dict) or isinstance(mail, dict)):
            print(f"mail request from {phone or 'unknown'}", flush=True)
            _tls.kind = "mail"
            if not mail_enabled_for(phone):
                print("mail blocked: phone not allowed", flush=True)
                answer = "אין לך הרשאה לגשת למיילים"
            elif confirm:
                answer = confirm_email(call_id, confirm)
            elif isinstance(send, dict):
                answer = prepare_email(call_id, send)
            elif isinstance(mail, dict) and mail_target()[0]:
                t = time.time()
                answer = handle_mail(call_id, mail, heard)
                print(f"timing: mail {mail.get('action')} {time.time() - t:.1f}s", flush=True)
        elif isinstance(create_podcast, dict):
            if not tools_allowed or assistant_mode not in ("tools", "central"):
                answer = "אין לך הרשאה להשתמש בכלי יצירת הסכתים"
            else:
                _tls.kind = "podcast"
                answer = create_podcast_audio(call_id, create_podcast)
        elif isinstance(create_image, dict):
            if not tools_allowed or assistant_mode not in ("tools", "central"):
                answer = "אין לך הרשאה להשתמש בכלי יצירת תמונות"
            else:
                _tls.kind = "image"
                answer = prepare_image_email(call_id, phone, create_image)
        elif tool_action == "utility_map":
            if not tools_allowed:
                answer = "אין לך הרשאה להשתמש בכלי מפות"
            elif not isinstance(map_request, dict):
                answer = clarification_question or "כדי לחשב מסלול, אמור במשפט אחד מוצא, יעד, מועד יציאה או הגעה, והאם מדובר בתחבורה ציבורית או הליכה."
                with lock:
                    tool_state = calls.get(call_id)
                    if tool_state is not None:
                        tool_state["tool_clarification_waiting"] = True
            else:
                _tls.kind = "map"
                answer = compute_map_route(map_request)
                if answer.startswith("חסרים פרטי מסלול") or answer.startswith("לא הצלחתי להבין את מועד"):
                    with lock:
                        tool_state = calls.get(call_id)
                        if tool_state is not None:
                            tool_state["tool_clarification_waiting"] = True
        elif isinstance(forum_content, dict) and tool_action in CREATION_ACTIONS:
            if not tools_allowed:
                answer = "אין לך הרשאה להשתמש בכלי יצירת תוכן"
            else:
                title = str(forum_content.get("title") or "תוכן חדש")[:120]
                content_text = str(forum_content.get("content") or "").strip()[:20000]
                content_type = str(forum_content.get("content_type") or "text")[:30]
                if content_text:
                    published = save_generated_forum_post(
                        call_id, phone, title, content_text, content_type=content_type
                    )
                    answer = (str(data.get("answer") or "").strip() or
                              ("התוכן נוצר ופורסם בפורום." if published else "התוכן נוצר, אך לא הצלחתי לפרסם אותו בפורום."))
        elif tool_action == "writing" and assistant_mode == "tools" and answer and not forum_content:
            # The writing tool creates a public-ready draft unless the model marked it private.
            published = save_generated_forum_post(
                call_id, phone, "טקסט חדש", answer, content_type="text"
            )
            if published:
                answer = "הטקסט נוצר ונוסף לפורום באתר ובקו."
        elif isinstance(lookup, dict) and (lookup.get("query") or heard):
            if not tools_allowed:
                answer = "אין לך הרשאה להשתמש בכלי AI"
            else:
                _tls.kind = "lookup"
                t = time.time()
                found = do_lookup(lookup, heard)
                print(f"timing: lookup {lookup.get('type')} {time.time() - t:.1f}s", flush=True)
                answer = found or "לא הצלחתי למצוא מידע על זה"
    except Exception as e:
        print("action error:", redact(e), flush=True)
        answer = "לא הצלחתי לבצע את הפעולה"

    if return_to_menu:
        with lock:
            st = calls.get(call_id)
            if st is not None:
                if st.get("main_menu"):
                    st["return_main_menu"] = True
                elif assistant_mode == "tools":
                    st["return_tools_menu"] = True
                elif assistant_mode == "central":
                    st["main_menu"] = True
                    st["return_main_menu"] = True
                    st["menu_stage"] = "root"

    if heard:  # שומרים בהיסטוריה רק טקסט, כדי לשמור על מהירות
        history.append(types.Content(role="user", parts=[types.Part(text=heard)]))
        history.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        history[:] = history[-12:]
        if phone and history_enabled:
            db_save_transcript(call_id, phone, heard, answer, assistant_mode)
            threading.Thread(target=archive_yemot_history,
                             args=(phone, assistant_mode, call_id, heard, answer, bool(getattr(_tls, "yiddish", False))),
                             daemon=True).start()
    return answer, change_voice


def transcribe_caller_name(recorded, phone, use_yiddish):
    _tls.yiddish = bool(use_yiddish)
    _tls.phone = phone
    try:
        audio = download_recording(recorded)
        language_instruction = (
            "האזן להקלטה והחזר רק את השם הפרטי ושם המשפחה של הדובר ביידיש, באותיות יידיש. "
            "אם לא נאמר שם ברור, החזר בדיוק: לא ברור"
            if use_yiddish else
            "האזן להקלטה והחזר רק את השם הפרטי ושם המשפחה של הדובר בעברית. "
            "אם לא נאמר שם ברור, החזר בדיוק: לא ברור"
        )
        result = call_model(
            [types.Part.from_bytes(data=audio, mime_type="audio/wav"),
             types.Part(text=language_instruction)],
            types.GenerateContentConfig(max_output_tokens=60),
        )
        name = re.sub(r"[^0-9A-Za-z֐-׿'’ -]", "", (result.text or "")).strip()
        name = " ".join(name.split())[:40]
        if name and name.lower() != "לא ברור" and name != "לא ברור":
            callers = {k: dict(v) for k, v in _store["callers"].items()}
            profile = callers.get(phone, {"enabled": True, "mail": False})
            profile["name"] = name
            callers[phone] = profile
            _caller_loaded.add(phone)
            warning = persist("callers", callers)
            if not db_save_caller(phone, profile):
                print("caller name saved to profile store but not database", flush=True)
            print("caller name captured for phone ending", phone[-4:], flush=True)
            return name
        return ""
    except Exception as e:
        print("caller name transcription failed:", redact(e), flush=True)
        return ""


def worker_name(call_id, recorded, phone, use_yiddish):
    state = calls.get(call_id) or {}
    fallback = "/1/1" if use_yiddish else "/8"
    _tls.record_path = state.get("record_path") or state.get("ext_path") or fallback
    _tls.extension_path = state.get("extension_path") or state.get("ext_path") or "/"
    _tls.client = client_for(phone)
    _tls.keyid = phone if phone in all_keys() else "default"
    name = transcribe_caller_name(recorded, phone, use_yiddish)
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["name_status"] = "done"
            st["name_recording"] = recorded
            st["name_captured"] = bool(name)
            st["last"] = recorded


def worker(call_id, recorded, phone="", use_yiddish=None, assistant_mode=None, tool_action=None):
    keys = all_keys()
    _tls.client = client_for(phone)
    _tls.keyid = phone if phone in keys else "default"
    _tls.kind = "chat"
    _tls.phone = phone
    _tls.yiddish = YIDDISH_MODE if use_yiddish is None else bool(use_yiddish)
    _tls.assistant_mode = assistant_mode or ("yiddish" if _tls.yiddish else "smart")
    _tls.tool_action = tool_action or ""
    _tls.phone = phone
    state = calls.get(call_id) or {}
    _tls.tool_clarifications_done = bool(state.get("tool_clarifications_done"))
    fallback = "/1/1" if _tls.yiddish else "/8"
    _tls.record_path = state.get("record_path") or state.get("ext_path") or fallback
    _tls.extension_path = state.get("extension_path") or state.get("ext_path") or "/"
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
        db_notify_runtime_error(phone, err)

    audio_path = None
    with lock:
        current_state = calls.get(call_id)
        if current_state is not None:
            audio_path = current_state.pop("generated_audio", None)
    if not audio_path and voice_on() and not failed:
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

    if failed and phone:
        db_notify_runtime_error(phone, "שגיאה בשיחה: " + (err or "לא ידועה"))
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
        # Conversation histories are isolated by call and assistant.
        for history_key in [key for key in histories if key[0] == cid]:
            histories.pop(history_key, None)
        for history_key in [key for key in _history_loaded if key[0] == cid]:
            _history_loaded.discard(history_key)


ADMIN_PHONES = [re.sub(r"\D", "", x) for x in os.environ.get("ADMIN_PHONES", "").split(",") if re.sub(r"\D", "", x)]
_admin_fails = {}


def admin_phone_ok(phone):
    return (not ADMIN_PHONES) or phone in ADMIN_PHONES


def admin_locked(phone):
    now = time.time()
    fails = [t for t in _admin_fails.get(phone, []) if now - t < 900]
    _admin_fails[phone] = fails
    return len(fails) >= 5


def _digits(st, text, maxd=1, mind=1):
    # שם שדה חדש בכל שאלה, כדי שערך ישן מהשיחה לא יתפס כבחירה חדשה
    st["pc"] = st.get("pc", 0) + 1
    st["field"] = "k%d" % st["pc"]
    st["awaiting"] = True
    return Response(admin_digits_command(text, st["field"], maxd, mind), mimetype="text/plain")


def _speak_back(text):
    return Response("id_list_message=t-" + clean_for_tts(text) + "&go_to_folder=" + active_extension_path(), mimetype="text/plain")


def key_pool_has(phone):
    try:
        conn = db_connect()
        if conn is None:
            return False
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM gemini_key_pool WHERE assigned_phone=%s AND NOT disabled LIMIT 1", (phone,))
                return cur.fetchone() is not None
        finally:
            conn.close()
    except Exception:
        return False


TOOLS_MENU_ACTIONS = {
    "1": ("mail", "מיילים", "אמור מה תרצה לעשות במיילים. למשל לקרוא הודעות, לחפש מייל או לנסח ולשלוח הודעה."),
    "2": ("podcast", "יצירת הסכת", "אמור את נושא ההסכת, למי הוא מיועד ומה חשוב לכלול בו."),
    "3": ("image", "יצירת תמונה למייל", "תאר את התמונה הרצויה וציין לאיזו כתובת לשלוח אותה."),
    "4": ("file", "קריאת קובץ", "שלח קובץ כקובץ מצורף לתיבת המייל המחוברת, ואז בקש לקרוא ולסכם אותו."),
    "5": ("writing", "כתיבה וניסוח", "אמור איזה טקסט תרצה לכתוב או לשפר."),
    "6": ("research", "חיפוש ומחקר", "אמור מה תרצה לחקור או לבדוק."),
    "7": ("planning", "תכנון וחישובים", "אמור מה תרצה לתכנן או לחשב."),
}


def tools_menu_text():
    return (
        "ברוכים הבאים למרכז כלי הבינה המלאכותית. "
        "לשימוש חופשי בבינה עם כל הכלים הקש אפס. "
        "למיילים הקש אחת. ליצירת הסכת הקש שתיים. "
        "ליצירת תמונה ושליחתה למייל הקש שלוש. "
        "לקריאת קובץ באמצעות בינה מלאכותית הקש ארבע. "
        "לכתיבה וניסוח הקש חמש. לחיפוש ומחקר הקש שש. "
        "לתכנון וחישובים הקש שבע. לשמיעת התפריט מחדש הקש תשע. "
        "לחזרה לתפריט הקודם הקש כוכבית. לחזרה לתפריט הראשי הקש סולמית."
    )


def return_to_main_menu(st):
    st["menu_stage"] = "root"
    st["main_menu"] = True
    st["pa_mode"] = "menu"
    st["is_admin"] = False
    st["awaiting"] = False
    st["tools_chat"] = False
    st["tools_busy"] = False
    st["tools_recording"] = False
    st["forum_busy"] = False
    st["forum_recording"] = False
    st["status"] = "idle"
    st["last"] = None
    st.pop("tools_action", None)
    st.pop("tools_prompt", None)
    st.pop("utility_action", None)
    st.pop("tool_clarifications_done", None)
    st.pop("tool_clarification_waiting", None)
    st["turn"] += 1
    return _digits(st, main_menu_text())


def tools_area(st, p, call_id, phone, recorded, use_yiddish):
    if st.get("tools_chat"):
        return None

    if st.get("tools_recording"):
        if recorded and recorded != st.get("last"):
            st["last"] = recorded
            st["status"] = "processing"
            st["started"] = time.time()
            st["tools_recording"] = False
            action = st.get("tools_action") or ""
            threading.Thread(
                target=worker,
                args=(call_id, recorded, phone, use_yiddish, "tools", action),
                daemon=True,
            ).start()
            return Response(wait_command(), mimetype="text/plain")
        return Response(
            read_command(st.get("tools_prompt") or "אמור את בקשתך ובסיום הקש סולמית", call_id, st["turn"]),
            mimetype="text/plain",
        )

    if st.get("tools_busy"):
        if st.get("status") == "processing":
            return Response(wait_command(), mimetype="text/plain")
        if st.get("status") == "done":
            audio = st.get("audio")
            st["status"] = "idle"
            if st.get("tool_clarification_waiting"):
                st["tool_clarification_waiting"] = False
                st["tools_recording"] = True
                st["last"] = None
                st["turn"] += 1
                return Response(
                    read_command(st.get("answer") or "ענה בבקשה על שאלת המיקוד.", call_id, st["turn"]),
                    mimetype="text/plain",
                )
            if st.get("tools_action") == "utility_map":
                st["menu_stage"] = "utilities"
                st["tools_busy"] = False
                st["tools_recording"] = False
                st.pop("tools_action", None)
                st.pop("tools_prompt", None)
                st.pop("tool_clarifications_done", None)
                if audio:
                    return Response(
                        f"id_list_message=f-{audio}&go_to_folder={active_extension_path()}",
                        mimetype="text/plain",
                    )
                return _digits(st, utilities_menu_text())
            st["tools_busy"] = False
            st.pop("tools_action", None)
            st.pop("tools_prompt", None)
            st.pop("tool_clarifications_done", None)
            if audio:
                return Response(
                    f"id_list_message=f-{audio}&go_to_folder={active_extension_path()}",
                    mimetype="text/plain",
                )

    if st.get("awaiting"):
        st["awaiting"] = False
        val = (p.get(st.get("field", "")) or "").strip()
        if val in ("*", "#"):
            return return_to_main_menu(st)
        if val == "0":
            st["tools_chat"] = True
            st["tools_action"] = ""
            st["turn"] += 1
            return Response(
                read_command(
                    "בחרת שימוש חופשי בבינה. אפשר לבקש כל פעולה. כדי לחזור לתפריט אמור חזרה לתפריט.",
                    call_id, st["turn"],
                ),
                mimetype="text/plain",
            )
        if val == "9":
            return _digits(st, tools_menu_text())
        selected = TOOLS_MENU_ACTIONS.get(val)
        if not selected:
            return _digits(st, tools_menu_text() + " לא זיהיתי את הבחירה.")
        action, label, prompt = selected
        st["tools_action"] = action
        st["tools_prompt"] = prompt
        st["tools_busy"] = True
        st["tools_recording"] = True
        st["tool_clarifications_done"] = False
        st["tool_clarification_waiting"] = False
        st["turn"] += 1
        return Response(read_command(prompt, call_id, st["turn"]), mimetype="text/plain")

    return _digits(st, tools_menu_text())


MAIN_ASSISTANT_OPTIONS = {
    "1": ("smart", "הבינה החכמה"),
    "2": ("edgy", "הבינה הישירה"),
    "3": ("psychologist", "הבינה לשיחה והקשבה"),
    "4": ("yeshiva", "בינת הישיבה"),
    "5": ("yiddish", "הבינה ביידיש"),
    "6": ("astrology", "מפת האישיות"),
}


def main_menu_text(message=""):
    prefix = (message + ". ") if message else ""
    return (
        prefix + "ברוכים הבאים למערכת הבינה. "
        "לתפריט העוזרים הקש אחת. "
        "לתפריט כלי הבינה המלאכותית הקש שתיים. "
        "לכלים שימושיים הקש שלוש. "
        "לאזור האישי הקש ארבע. "
        "לניהול המערכת הקש חמש. "
        "לפורום הקולי בעיוות קול הקש שש. "
        "להמשך השיחה האחרונה שלך הקש שבע. "
        "לעוזר המרכזי עם גישה לכל הכלים הקש אפס. "
        "לשמיעת התפריט מחדש הקש תשע. "
        "למעבר לתפריט הקודם הקש כוכבית. לחזרה לתפריט הראשי הקש סולמית."
    )


def db_last_assistant_mode(phone):
    """סוג העוזר של ההודעה האחרונה שנשמרה למספר הזה, או ריק."""
    if not phone or caller(phone).get("history_enabled", True) is False:
        return ""
    conn = db_connect()
    if conn is None:
        return ""
    try:
        with conn.cursor() as cur:
            db_ensure_transcript_columns(conn)
            cur.execute("SELECT assistant_mode FROM call_transcripts WHERE phone=%s ORDER BY created_at DESC, id DESC LIMIT 1", (phone,))
            row = cur.fetchone()
        conn.commit()
        return (row[0] or "") if row else ""
    except Exception as e:
        print("last assistant lookup failed:", redact(e), flush=True)
        return ""
    finally:
        conn.close()


def assistants_menu_text(message=""):
    prefix = (message + ". ") if message else ""
    return (
        prefix + "תפריט סוגי הבינות. "
        "לבינה החכמה הקש אחת. לבינה הישירה הקש שתיים. "
        "לבינה לשיחה והקשבה הקש שלוש. לבינת הישיבה הקש ארבע. "
        "לבינה ביידיש הקש חמש. למפת האישיות הקש שש. "
        "לחזרה לתפריט הראשי הקש אפס. לחזרה לתפריט הקודם הקש כוכבית. לסולמית לחזרה לתפריט הראשי."
    )


def main_menu_area(st, p, call_id, phone, recorded):
    if st.get("awaiting"):
        st["awaiting"] = False
        val = (p.get(st.get("field", "")) or "").strip()
        stage = st.get("menu_stage", "root")
        if val in ("*", "#"):
            return return_to_main_menu(st)
        if stage == "root":
            if val == "1":
                st["menu_stage"] = "assistants"
                return _digits(st, assistants_menu_text())
            if val == "2":
                st["menu_stage"] = "tools"
                st["tools_chat"] = False
                st["tools_busy"] = False
                st.pop("tools_action", None)
                return _digits(st, tools_menu_text())
            if val == "3":
                st["menu_stage"] = "utilities"
                return _digits(st, utilities_menu_text())
            if val == "4":
                st["menu_stage"] = "personal"
                st["pa_mode"] = "menu"
                st["is_admin"] = False
                return phone_area(st, p, call_id, phone, recorded)
            if val == "5":
                st["menu_stage"] = "personal"
                st["is_admin"] = False
                if not admin_phone_ok(phone):
                    st["menu_stage"] = "root"
                    return _digits(st, main_menu_text("אין הרשאה להיכנס לניהול"))
                if admin_locked(phone):
                    st["menu_stage"] = "root"
                    return _digits(st, main_menu_text("הגישה לניהול נעולה זמנית לאחר ניסיונות כושלים"))
                st["pa_mode"] = "code"
                return phone_area(st, p, call_id, phone, recorded)
            if val == "6":
                st["menu_stage"] = "forum"
                st["forum_stage"] = "menu"
                return _digits(st, forum_menu_text())
            if val == "7":
                last_mode = db_last_assistant_mode(phone)
                labels = {m: l for m, l in MAIN_ASSISTANT_OPTIONS.values()}
                labels["central"] = "העוזר המרכזי"
                if last_mode == "tools":
                    # Resume the previous tools conversation directly instead of
                    # sending the caller back to the tools selection menu.
                    st["menu_stage"] = "tools"
                    st["tools_chat"] = True
                    st["tools_busy"] = False
                    st.pop("tools_action", None)
                    st["main_menu"] = True
                    st["status"] = "idle"
                    st["last"] = None
                    st["turn"] += 1
                    return Response(
                        read_command(
                            "ממשיכים מהשיחה האחרונה שלך בכלי הבינה. אמור את הבקשה הבאה שלך. כדי לחזור לתפריט הראשי אמור חזרה לתפריט.",
                            call_id, st["turn"],
                        ),
                        mimetype="text/plain",
                    )
                if last_mode not in labels:
                    return _digits(st, main_menu_text("אין שיחה קודמת להמשיך"))
                st["selected_assistant_mode"] = last_mode
                st["menu_stage"] = "assistant_chat"
                st["main_menu"] = True
                st["status"] = "idle"
                st["last"] = None
                st["tools_chat"] = False
                st.pop("tools_action", None)
                st["turn"] += 1
                if last_mode == "yiddish":
                    prompt = "מיר גייען ווייטער פון דיין לעצטן שמועס. זאג דיין בקשה אויף יידיש. צו צוריקגיין צום הויפט מעניו זאג צוריק צום מעניו."
                else:
                    prompt = ("ממשיכים מהשיחה האחרונה שלך עם " + labels[last_mode] +
                              ". אמור את בקשתך. כדי לחזור לתפריט הראשי אמור חזרה לתפריט.")
                return Response(read_command(prompt, call_id, st["turn"]), mimetype="text/plain")
            if val == "0":
                st["selected_assistant_mode"] = "central"
                st["menu_stage"] = "assistant_chat"
                st["main_menu"] = True
                st["status"] = "idle"
                st["last"] = None
                st["tools_chat"] = False
                st.pop("tools_action", None)
                st["turn"] += 1
                return Response(
                    read_command(
                        "ברוכים הבאים לעוזר הבינה המרכזי, עם גישה לכלי הבינה הזמינים עבורך. אפשר לשאול כל שאלה או לבקש לבצע משימה. " + ("בשיחה הקודמת שאלת: " + clean_for_tts(st.get("previous_topic", ""))[:100] + ". " if st.get("previous_topic") else "") + "כדי לחזור לתפריט הראשי אמור חזרה לתפריט.",
                        call_id, st["turn"],
                    ),
                    mimetype="text/plain",
                )
            if val in ("9", ""):
                return _digits(st, main_menu_text())
            return _digits(st, main_menu_text("לא זיהיתי את הבחירה"))
        if stage == "assistants":
            if val in ("0", "9", ""):
                st["menu_stage"] = "root"
                return _digits(st, main_menu_text())
            selected = MAIN_ASSISTANT_OPTIONS.get(val)
            if not selected:
                return _digits(st, assistants_menu_text("לא זיהיתי את הבחירה"))
            mode, label = selected
            st["selected_assistant_mode"] = mode
            st["menu_stage"] = "assistant_chat"
            st["status"] = "idle"
            st["last"] = None
            st["tools_chat"] = False
            st.pop("tools_action", None)
            st["turn"] += 1
            context = ("בשיחה הקודמת שאלת: " + clean_for_tts(st.get("previous_topic", ""))[:100] + ". אפשר להמשיך מאותו עניין או לשאול משהו חדש. ") if st.get("previous_topic") else ""
            prompt = "בחרת " + label + ". " + context + "אמור את בקשתך. כדי לחזור לתפריט הראשי אמור חזרה לתפריט."
            if mode == "yiddish":
                prompt = "האסט אויסגעקליבן די יידישע בינה. זאג דיין בקשה אויף יידיש. צו צוריקגיין צום הויפט מעניו זאג צוריק צום מעניו."
            return Response(read_command(prompt, call_id, st["turn"]), mimetype="text/plain")
    stage = st.get("menu_stage", "root")
    return _digits(st, assistants_menu_text() if stage == "assistants" else main_menu_text())


def utilities_menu_text(message=""):
    prefix = (message + ". ") if message else ""
    return (
        prefix + "תפריט כלים שימושיים. "
        "לבדיקת מזג אוויר הקש אחת. לחישוב הקש שתיים. "
        "להמרת יחידות ומטבעות הקש שלוש. לתרגום הקש ארבע. "
        "לחיפוש מהיר הקש חמש. למפות ולתכנון מסלול הקש שש. "
        "לחזרה לתפריט הראשי הקש אפס. "
        "לחזרה לתפריט הקודם הקש כוכבית. לחזרה לתפריט הראשי הקש סולמית."
    )


UTILITY_ACTIONS = {
    "1": ("utility_weather", "בדיקת מזג אוויר", "אמור באיזו עיר לבדוק את מזג האוויר או שאל שאלה על מזג האוויר."),
    "2": ("utility_calculator", "מחשבון", "אמור את התרגיל או החישוב שברצונך לבצע."),
    "3": ("utility_conversion", "המרת יחידות ומטבעות", "אמור מה להמיר, מאיזו יחידה או מטבע ולאיזו יחידה או מטבע."),
    "4": ("utility_translation", "תרגום", "אמור את הטקסט ואת שפת היעד לתרגום."),
    "5": ("utility_research", "חיפוש מהיר", "אמור מה תרצה לחפש ולבדוק."),
    "6": ("utility_map", "מפות", "במשפט אחד אמור את המוצא, היעד, תאריך ושעת היציאה או ההגעה, והאם אתה מעדיף תחבורה ציבורית או הליכה ברגל."),
}


def utilities_area(st, p, call_id):
    if st.get("awaiting"):
        st["awaiting"] = False
        val = (p.get(st.get("field", "")) or "").strip()
        if val in ("*", "#"):
            return return_to_main_menu(st)
        if val in ("0", "9", ""):
            st["menu_stage"] = "root"
            return _digits(st, main_menu_text())
        selected = UTILITY_ACTIONS.get(val)
        if not selected:
            return _digits(st, utilities_menu_text("לא זיהיתי את הבחירה"))
        action, label, prompt = selected
        if action == "utility_map":
            # A one-shot route request, not a free-form tools chat.
            st["menu_stage"] = "utility_map"
            st["tools_chat"] = False
            st["tools_busy"] = True
            st["tools_recording"] = True
            st["tools_action"] = action
            st["tools_prompt"] = prompt
            st["tool_clarifications_done"] = True
            st["tool_clarification_waiting"] = False
        else:
            st["menu_stage"] = "utility_chat"
            st["tools_chat"] = True
            st["tools_action"] = action
            st["tool_clarifications_done"] = False
            st["tool_clarification_waiting"] = False
        st["last"] = None
        st["status"] = "idle"
        st["turn"] += 1
        return Response(read_command("בחרת " + label + ". " + prompt + " כדי לחזור לתפריט הראשי אמור חזרה לתפריט.", call_id, st["turn"]), mimetype="text/plain")
    return _digits(st, utilities_menu_text())


def forum_menu_text(message=""):
    prefix = (message + ". ") if message else ""
    return (
        prefix + "הפורום הקולי והתכנים שנוצרו במערכת. "
        "להאזנה להודעות ולתכנים של משתמשים אחרים, כולל הסכתים, הקש אחת. "
        "לפרסום הודעה קולית חדשה בעיוות קול הקש שתיים. "
        "לחזרה לתפריט הראשי הקש אפס. לחזרה לתפריט הקודם הקש כוכבית. לסולמית לחזרה לתפריט הראשי."
    )


def forum_distort_voice(raw_audio):
    """Change playback pitch so the original speaker is less recognizable."""
    source = io.BytesIO(raw_audio)
    output = io.BytesIO()
    with wave.open(source, "rb") as inp:
        channels = inp.getnchannels()
        sample_width = inp.getsampwidth()
        frame_rate = inp.getframerate()
        frames = inp.readframes(inp.getnframes())
    if sample_width != 2 or channels not in (1, 2) or not frame_rate:
        raise ValueError("פורמט ההקלטה אינו נתמך לעיוות קול")
    # Lower playback sample rate: recognizable speech remains intelligible, with a lower altered voice.
    changed_rate = max(6000, int(frame_rate * 0.78))
    with wave.open(output, "wb") as out:
        out.setnchannels(channels)
        out.setsampwidth(sample_width)
        out.setframerate(changed_rate)
        out.writeframes(frames)
    return output.getvalue()


def forum_upload_audio(raw_audio, call_id):
    token = f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}"
    folder = "/forum"
    try:
        requests.get(
            "https://www.call2all.co.il/ym/api/Ym_MakeDir",
            params={"token": token, "path": "ivr2:" + folder},
            timeout=12,
        )
    except Exception:
        pass
    suffix = re.sub(r"[^0-9A-Za-z]", "", str(call_id))[:16] or "call"
    filename = "post" + str(time.time_ns()) + suffix
    response = requests.post(
        "https://www.call2all.co.il/ym/api/UploadFile",
        data={"token": token, "path": "ivr2:" + folder + "/" + filename + ".wav", "convertAudio": "1"},
        files={"file": (filename + ".wav", raw_audio, "audio/wav")},
        timeout=40,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("responseStatus") != "OK":
        raise RuntimeError(result.get("message", "forum upload failed"))
    return folder + "/" + filename


def forum_public_content_safe(title, content_text):
    combined = str(title or "") + "\n" + str(content_text or "")
    if re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", combined):
        return False
    # Avoid auto-publishing obvious Israeli mobile/landline phone numbers.
    if re.search(r"(?<!\d)(?:\+972[- .]?|0)(?:5\d|[2-9])[- .]?\d{3}[- .]?\d{4}(?!\d)", combined):
        return False
    return True


def save_generated_forum_post(call_id, phone, title, content_text, content_type="text",
                                audio_path="", image_data=None, image_mime=""):
    """Publish a non-private generated artifact to the shared website and phone forum."""
    title = " ".join(str(title or "תוכן חדש").split())[:120] or "תוכן חדש"
    content_text = str(content_text or "").strip()[:20000]
    content_type = str(content_type or "text")[:30]
    if not content_text and not image_data:
        return None
    if not forum_public_content_safe(title, content_text):
        print("generated forum post skipped: contact details detected", flush=True)
        return None
    if not audio_path:
        speech_text = (title + ". " + content_text)[:1800]
        try:
            wav = tts_wav(speech_text, cfg("default_voice"))
            audio_path = forum_upload_audio(wav, call_id)
        except Exception as e:
            print("generated forum audio failed:", redact(e)[:180], flush=True)
            audio_path = ""
    display_name = (caller(phone).get("name") or "משתמש")[:50]
    record = {
        "id": int(time.time() * 1000),
        "source_call_id": str(call_id or "")[:200],
        "phone": str(phone or "")[:30],
        "name": display_name,
        "title": title,
        "content_type": content_type,
        "content_text": content_text,
        "audio_path": audio_path,
        "image_data": image_data,
        "image_mime": image_mime or "",
        "created_at": time.strftime("%d/%m %H:%M"),
    }
    conn = db_connect()
    if conn is None:
        _forum_content_memory.appendleft(record)
        return record
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO forum_content_posts
                       (source_call_id, phone, display_name, title, content_type,
                        content_text, audio_path, image_data, image_mime)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                       RETURNING id, created_at""",
                    (record["source_call_id"], record["phone"], display_name, title,
                     content_type, content_text, audio_path, image_data, record["image_mime"]),
                )
                row = cur.fetchone()
        record["id"] = row[0]
        record["created_at"] = row[1].strftime("%d/%m %H:%M")
        return record
    except Exception as e:
        print("generated forum post save failed:", redact(e)[:180], flush=True)
        _forum_content_memory.appendleft(record)
        return record
    finally:
        conn.close()


def forum_save_post(phone, audio_path):
    display_name = (caller(phone).get("name") or "משתמש")[:50]
    record = {
        "id": int(time.time() * 1000),
        "phone": phone,
        "name": display_name,
        "audio_path": audio_path,
        "created_at": time.strftime("%d/%m %H:%M"),
    }
    conn = db_connect()
    if conn is None:
        _forum_memory.appendleft(record)
        return record
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO voice_forum_posts (phone, display_name, audio_path) VALUES (%s, %s, %s) RETURNING id, created_at",
                    (phone[:30], display_name, audio_path),
                )
                row = cur.fetchone()
        record["id"] = row[0]
        record["created_at"] = row[1].strftime("%d/%m %H:%M")
        return record
    except Exception as e:
        print("forum post save failed:", redact(e)[:160], flush=True)
        _forum_memory.appendleft(record)
        return record
    finally:
        conn.close()


def forum_recent_posts(limit=8):
    limit = max(1, min(int(limit), 12))
    conn = db_connect()
    if conn is None:
        combined = list(_forum_memory) + list(_forum_content_memory)
        return sorted(combined, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, display_name, audio_path, created_at, 'voice' AS post_kind, '' AS title, '' AS content_text
                   FROM voice_forum_posts
                   UNION ALL
                   SELECT id, display_name, audio_path, created_at, 'generated' AS post_kind, title, content_text
                   FROM forum_content_posts
                   ORDER BY created_at DESC, id DESC LIMIT %s""",
                (limit,),
            )
            rows = cur.fetchall()
        conn.commit()
        return [
            {"id": row[0], "name": ((row[5] + " מאת ") if row[5] else "") + (row[1] or "משתמש"),
             "audio_path": row[2], "created_at": row[3].strftime("%d/%m %H:%M"),
             "kind": row[4], "title": row[5] or "", "content_text": row[6] or ""}
            for row in rows
        ]
    except Exception as e:
        print("forum list failed:", redact(e)[:160], flush=True)
        combined = list(_forum_memory) + list(_forum_content_memory)
        return sorted(combined, key=lambda x: x.get("created_at", ""), reverse=True)[:limit]
    finally:
        conn.close()


def forum_worker(call_id, recorded, phone):
    state = calls.get(call_id)
    _tls.phone = phone
    state = state or {}
    _tls.record_path = state.get("record_path") or state.get("ext_path") or "/8"
    _tls.extension_path = state.get("extension_path") or state.get("ext_path") or "/"
    try:
        original = download_recording(recorded)
        altered = forum_distort_voice(original)
        audio_path = forum_upload_audio(altered, call_id)
        forum_save_post(phone, audio_path)
        message = "ההודעה פורסמה בפורום"
        ok = True
    except Exception as e:
        print("forum audio failed:", redact(e)[:180], flush=True)
        message = "לא הצלחתי לפרסם את ההודעה. נסה להקליט שוב"
        ok = False
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["forum_result"] = message
            st["forum_ok"] = ok
            st["status"] = "done"


def forum_area(st, p, call_id, phone, recorded):
    if st.get("forum_busy"):
        if st.get("status") == "processing":
            return Response(wait_command(), mimetype="text/plain")
        if st.get("status") == "done":
            message = st.pop("forum_result", "הפעולה הסתיימה")
            st["forum_busy"] = False
            st["status"] = "idle"
            st["forum_recording"] = False
            st["last"] = None
            return _digits(st, forum_menu_text(message))

    if st.get("forum_recording"):
        if recorded and recorded != st.get("last"):
            st["last"] = recorded
            st["forum_recording"] = False
            st["forum_busy"] = True
            st["status"] = "processing"
            st["started"] = time.time()
            threading.Thread(target=forum_worker, args=(call_id, recorded, phone), daemon=True).start()
            return Response(wait_command(), mimetype="text/plain")
        return Response(read_command("אמור את ההודעה לפורום ובסיום הקש סולמית.", call_id, st["turn"]), mimetype="text/plain")

    if st.get("awaiting"):
        st["awaiting"] = False
        val = (p.get(st.get("field", "")) or "").strip()
        stage = st.get("forum_stage", "menu")
        if stage == "menu":
            if val in ("*", "#"):
                return return_to_main_menu(st)
            if val in ("0", "9", ""):
                st["menu_stage"] = "root"
                return _digits(st, main_menu_text())
            if val == "1":
                posts = forum_recent_posts()
                if not posts:
                    return _digits(st, forum_menu_text("אין עדיין הודעות בפורום"))
                st["forum_posts"] = posts
                st["forum_stage"] = "pick"
                options = " ".join(
                    f"{i + 1}. {clean_for_tts(post['name'])} {post['created_at']}"
                    for i, post in enumerate(posts)
                )
                return _digits(st, "ההודעות האחרונות בפורום. " + options + ". להאזנה להודעה הקש את המספר שלה. לחזרה הקש אפס")
            if val == "2":
                st["forum_stage"] = "menu"
                st["forum_recording"] = True
                st["last"] = None
                st["turn"] += 1
                return Response(read_command("אמור את ההודעה לפורום ובסיום הקש סולמית.", call_id, st["turn"]), mimetype="text/plain")
            return _digits(st, forum_menu_text("לא זיהיתי את הבחירה"))
        if stage == "pick":
            if val == "#":
                return return_to_main_menu(st)
            if val == "*":
                st["forum_stage"] = "menu"
                return _digits(st, forum_menu_text())
            if val in ("0", "9", ""):
                st["forum_stage"] = "menu"
                return _digits(st, forum_menu_text())
            posts = st.get("forum_posts") or []
            if val.isdigit() and 1 <= int(val) <= len(posts):
                post = posts[int(val) - 1]
                st["forum_stage"] = "menu"
                if post.get("audio_path"):
                    return Response(
                        f"id_list_message=f-{post['audio_path']}&go_to_folder={active_extension_path()}",
                        mimetype="text/plain",
                    )
                spoken = clean_for_tts((post.get("title") or "תוכן בפורום") + ". " + (post.get("content_text") or ""))
                st["turn"] += 1
                return Response(read_command(spoken or "התוכן הזה אינו כולל הקלטה.", call_id, st["turn"]), mimetype="text/plain")
            return _digits(st, "הבחירה אינה תקינה. " + forum_menu_text())
    return _digits(st, forum_menu_text())


def personal_menu_text(phone, message=""):
    prefix = (message + ". ") if message else ""
    name = caller(phone).get("name", "")
    text = (prefix + "האזור האישי" + (" של " + name if name else "") + ". "
            "לשמיעת פרטי הפרופיל שלך הקש 1. להחלפת הקול שלך הקש 2. לעדכון השם שלך הקש 3. "
            "להפעלה או כיבוי של שמירת היסטוריית השיחות הקש 4. לשמיעת השיחה האחרונה שלך הקש 5. ")
    return text + ("לחזרה לתפריט הראשי הקש 0. " if calls.get(getattr(_tls, "call_id", ""), {}).get("main_menu") else "לשמיעה חוזרת הקש 0. ") + "לחזרה לתפריט הקודם הקש כוכבית. לחזרה לתפריט הראשי הקש סולמית."


def profile_text(phone):
    c = caller(phone)
    if all_keys().get(phone):
        key = "יש לך מפתח אישי"
    elif key_pool_has(phone):
        key = "הוקצה לך מפתח מהמאגר"
    else:
        key = "אין לך מפתח אישי כרגע ואתה משתמש במפתח המשותף"
    mail = "מחוברת תיבת מייל אישית" if own_mail(phone) else ("יש לך גישה לתיבת המייל הראשית" if mail_enabled_for(phone) else "אין לך חיבור למייל")
    hist = "מופעלת" if c.get("history_enabled", True) else "כבויה"
    digits = " ".join(phone)
    return f"שמך {c.get('name') or 'לא הוגדר'}. מספר הטלפון שלך {digits}. {key}. {mail}. שמירת ההיסטוריה {hist}"


def _save_profile(phone, **changes):
    callers = {k: dict(v) for k, v in _store["callers"].items()}
    prof = callers.get(phone, {"enabled": True, "mail": False})
    prof.update(changes)
    callers[phone] = prof
    _caller_loaded.add(phone)
    warning = persist("callers", callers)
    db_save_caller(phone, prof)
    return warning


def worker_rename(call_id, recorded, phone):
    _tls.phone = phone
    state = calls.get(call_id) or {}
    _tls.record_path = state.get("record_path") or state.get("ext_path") or "/7"
    _tls.extension_path = state.get("extension_path") or state.get("ext_path") or "/"
    _tls.client = client_for(phone)
    _tls.keyid = phone if phone in all_keys() else "default"
    name = transcribe_caller_name(recorded, phone, False)
    with lock:
        st = calls.get(call_id)
        if st is not None:
            st["rename_status"] = "done"
            st["rename_result"] = name
            st["last"] = recorded


def alerts_text():
    now = time.time()
    recent = [e for e in events if not e.get("ok") and now - e.get("t", 0) < 86400]
    parts = [f"בעשרים וארבע השעות האחרונות היו {len(recent)} שגיאות"]
    if recent:
        e = recent[0]
        parts.append("האחרונה ממספר " + " ".join(str(e.get("phone", ""))))
    for n in db_list_notifications()[:3]:
        parts.append(n["message"])
    if not DATABASE_URL:
        parts.append("מסד הנתונים אינו מחובר")
    return ". ".join(parts)


def phone_area(st, p, call_id, phone, recorded):
    mode = st.get("pa_mode") or "menu"

    if mode == "rename_wait":
        if st.get("rename_status") == "processing":
            return Response(wait_command(), mimetype="text/plain")
        if st.get("rename_status") == "done":
            new = st.pop("rename_result", "")
            st["rename_status"] = None
            st["pa_mode"] = "menu"
            msg = ("שמך עודכן ל" + new) if new else "לא הצלחתי לשמוע את השם. נסה שוב"
            return _digits(st, personal_menu_text(phone, msg))
    if mode == "rename_record":
        if recorded and recorded != st.get("last"):
            st["last"] = recorded
            st["pa_mode"] = "rename_wait"
            st["rename_status"] = "processing"
            threading.Thread(target=worker_rename, args=(call_id, recorded, phone), daemon=True).start()
            return Response(wait_command(), mimetype="text/plain")
        st["turn"] += 1
        fname = "n" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10] + str(st["turn"])
        return Response(f"read=t-{clean_for_tts('אמור את שמך הפרטי ובסיום הקש סולמית')}=question,no,record,{active_record_path()},{fname},no,yes,no", mimetype="text/plain")

    if st.get("awaiting"):
        st["awaiting"] = False
        val = (p.get(st.get("field", "")) or "").strip()
        return _pa_handle(st, mode, val, call_id, phone)
    return _pa_show(st, mode, phone)


def _pa_show(st, mode, phone):
    if mode == "admin" and st.get("is_admin"):
        return _digits(st, admin_menu_text())
    if mode == "code":
        return _digits(st, "הקש את קוד הניהול", 12, 1)
    if mode in ("block", "unblock"):
        return _digits(st, "הקש את מספר הטלפון", 15, 9)
    st["pa_mode"] = "menu"
    return _digits(st, personal_menu_text(phone))


def _pa_handle(st, mode, val, call_id, phone):
    if val == "#":
        return return_to_main_menu(st)
    if val == "*":
        if mode in ("tpick", "block", "unblock") and st.get("is_admin"):
            st["pa_mode"] = "admin"
            return _digits(st, admin_menu_text())
        if mode == "menu" and not st.get("main_menu"):
            st["pa_mode"] = "menu"
            return _digits(st, personal_menu_text(phone))
        return return_to_main_menu(st)
    if mode == "menu":
        if val == "0" and st.get("main_menu"):
            st["menu_stage"] = "root"
            st["pa_mode"] = "menu"
            return _digits(st, main_menu_text())
        msg = ""
        if val == "1":
            msg = profile_text(phone)
        elif val == "2":
            nxt = (voice_index_for(phone, "") + 1) % len(VOICES)
            _save_profile(phone, voice=VOICES[nxt])
            st["voice"] = nxt
            msg = f"הקול שלך הוחלף לקול מספר {nxt + 1}"
        elif val == "3":
            st["pa_mode"] = "rename_record"
            return phone_area(st, request.values, call_id, phone, None)
        elif val == "4":
            on = not caller(phone).get("history_enabled", True)
            _save_profile(phone, history_enabled=on)
            msg = "שמירת ההיסטוריה הופעלה" if on else "שמירת ההיסטוריה כובתה. שיחות חדשות לא יישמרו"
        elif val == "5":
            turns = db_load_history(phone) if caller(phone).get("history_enabled", True) else []
            if turns:
                heard, answer = turns[-1]
                msg = f"בשיחה האחרונה אמרת: {clean_for_tts(heard)[:200]}. ועניתי: {clean_for_tts(answer)[:250]}"
            else:
                msg = "אין שיחות שמורות"
        elif val not in ("", "0"):
            msg = "לא זיהיתי את הבחירה"
        return _digits(st, personal_menu_text(phone, msg))

    if mode == "code":
        ok = bool(val) and not admin_locked(phone) and hmac.compare_digest(val.encode("utf-8"), PHONE_ADMIN_CODE.encode("utf-8"))
        if ok:
            st["is_admin"] = True
            st["pa_mode"] = "admin"
            return _digits(st, admin_menu_text("הקוד אושר"))
        _admin_fails.setdefault(phone, []).append(time.time())
        print("phone admin code rejected for ending", phone[-4:], flush=True)
        st["pa_mode"] = "menu"
        return _digits(st, personal_menu_text(phone, "הקוד שגוי"))

    if not st.get("is_admin"):
        st["pa_mode"] = "menu"
        return _digits(st, personal_menu_text(phone))

    if mode == "admin":
        msg = ""
        if val == "1":
            msg = admin_status_text()
        elif val == "2":
            settings = dict(_store["settings"])
            settings["system_enabled"] = not bool(cfg("system_enabled"))
            warn = persist("settings", settings)
            msg = ("העוזר הופעל" if settings["system_enabled"] else "העוזר כובה") + ((". " + warn) if warn else "")
        elif val == "3":
            settings = dict(_store["settings"])
            settings["voice_enabled"] = not voice_on()
            persist("settings", settings)
            msg = "המענה הקולי הופעל" if settings["voice_enabled"] else "המענה הקולי כובה"
        elif val == "4":
            settings = dict(_store["settings"])
            settings["default_voice"] = VOICES[(default_voice_index() + 1) % len(VOICES)]
            persist("settings", settings)
            msg = "קול ברירת המחדל הוחלף"
        elif val == "5":
            msg = alerts_text()
        elif val == "6":
            items = db_admin_recent_transcripts(5)
            if not items:
                msg = "לא נמצאו תמלולי שיחות שמורים"
            else:
                st["pa_tr"] = items
                st["pa_mode"] = "tpick"
                options = " ".join(f"{i + 1}. {clean_for_tts(it['name'] or it['phone'])} {it['created_at']}" for i, it in enumerate(items))
                return _digits(st, "חמש השיחות האחרונות. " + options + ". להאזנה לתמלול הקש את מספר השיחה")
        elif val == "7":
            st["pa_mode"] = "block"
            return _digits(st, "הקש את מספר הטלפון שברצונך לחסום", 15, 9)
        elif val == "8":
            st["pa_mode"] = "unblock"
            return _digits(st, "הקש את מספר הטלפון שברצונך לפתוח מחסימה", 15, 9)
        elif val in ("0", ""):
            if st.get("main_menu"):
                st["menu_stage"] = "root"
                st["pa_mode"] = "menu"
                return _digits(st, main_menu_text())
            st["pa_mode"] = "menu"
            return _digits(st, personal_menu_text(phone))
        else:
            msg = "לא זיהיתי את הבחירה"
        return _digits(st, admin_menu_text(msg))

    if mode == "tpick":
        items = st.pop("pa_tr", []) or []
        st["pa_mode"] = "admin"
        if val.isdigit() and 1 <= int(val) <= len(items):
            it = items[int(val) - 1]
            return _speak_back(f"שיחה מאת {it['name'] or it['phone']}. בתאריך {it['created_at']}. המתקשר אמר: {it['heard']}. תשובת העוזר: {it['answer']}")
        return _digits(st, admin_menu_text("בחירה לא תקינה"))

    if mode in ("block", "unblock"):
        target = re.sub(r"\D", "", val)
        st["pa_mode"] = "admin"
        if not 9 <= len(target) <= 15:
            return _digits(st, admin_menu_text("מספר הטלפון אינו תקין"))
        enable = mode == "unblock"
        _save_profile(target, enabled=enable)
        return _digits(st, admin_menu_text(("החסימה הוסרה עבור המספר " if enable else "המספר נחסם: ") + " ".join(target)))

    st["pa_mode"] = "menu"
    return _digits(st, personal_menu_text(phone))


@app.route("/ivr", methods=["GET", "POST"])
def ivr():
    p = request.values
    call_id = p.get("ApiCallId", "unknown")
    _tls.call_id = call_id
    phone = p.get("ApiPhone", "")
    requested_lang = (p.get("lang") or "").strip().lower()
    requested_mode = (p.get("mode") or "").strip().lower()
    # ימות מוחקת לפעמים את ה-& מכתובת השלוחה, ואז מגיע lang=hemode=smart. מפרקים בחזרה.
    if "mode=" in requested_lang:
        requested_lang, _, glued_mode = requested_lang.partition("mode=")
        requested_mode = requested_mode or glued_mode.split("&")[0].strip()
    allowed_modes = {"smart", "edgy", "psychologist", "yeshiva", "yiddish", "tools", "astrology", "central", "personal", "menu"}
    use_yiddish = requested_lang in ("yi", "yiddish", "1") if requested_lang else YIDDISH_MODE
    _tls.yiddish = use_yiddish
    api_ext = re.sub(r"[^0-9/]", "", str(p.get("ApiExtension", ""))).strip("/")
    if api_ext:
        ext_path = "/" + api_ext
        _tls.record_path = ext_path
        _tls.extension_path = ext_path
    else:
        # בכניסה הראשית מקליטים בתיקייה 8, אך חוזרים לשורש התפריט לאחר המתנה.
        ext_path = EXTENSION_PATH
        _tls.record_path = "/1/1" if use_yiddish else RECORD_PATH
        _tls.extension_path = EXTENSION_PATH
    maybe_refresh()
    db_load_caller(phone)
    assistant_mode = requested_mode if requested_mode in allowed_modes else (caller(phone).get("assistant_mode") or ("yiddish" if use_yiddish else "smart"))
    # All public entry points converge on the single main menu at the system root.
    # Its internal keypad choices route to assistants, AI tools, utilities, personal area,
    # management, voice forum, and conversation resume. Extension 8 is only for recordings.
    # Keep the explicit personal mode for the authenticated admin web/phone flow.
    if requested_mode == "personal":
        assistant_mode = "personal"
    else:
        assistant_mode = "menu"

    if p.get("hangup") == "yes":
        with lock:
            calls.pop(call_id, None)
        for history_key in [key for key in histories if key[0] == call_id]:
            histories.pop(history_key, None)
        for history_key in [key for key in _history_loaded if key[0] == call_id]:
            _history_loaded.discard(history_key)
        return Response("", mimetype="text/plain")

    # כניסה ישנה (כולל שלוחה 8) רק מעבירה לשורש; היא אינה מציגה תפריט נוסף.
    if requested_mode == "redirect":
        return Response(
            "id_list_message=t-מעבירים אותך לתפריט הראשי&go_to_folder=/",
            mimetype="text/plain",
        )

    if TEST_MODE:
        return Response("id_list_message=t-שלום זו בדיקה של המערכת", mimetype="text/plain")

    if caller(phone).get("enabled") is False:
        return Response("id_list_message=t-אין לך הרשאה להשתמש בשלוחה זו&go_to_folder=hangup", mimetype="text/plain")

    # רק מתקשרים שהרשאת המייל שלהם פתוחה יכולים לראות את בקשת קוד הניהול.
    keypad = (p.get("admin_code") or p.get("admin_action") or p.get("admin_phone") or
              p.get("transcript_choice") or p.get("Digits") or p.get("digits") or
              p.get("ApiDigits") or "").strip()
    recorded_all = p.getlist("question")
    recorded = recorded_all[-1] if recorded_all else None

    # Load caller history once at the start of each new call, outside the global lock.
    is_new_call = call_id not in calls
    previous_turns = db_load_recent_history(phone) if is_new_call and phone and caller(phone).get("history_enabled", True) is not False else []
    previous_topic = previous_turns[-1][0].strip() if previous_turns else ""

    with lock:
        cleanup_old()
        st = calls.setdefault(
            call_id,
            {"turn": 0, "last": None, "status": "idle", "answer": "", "t": time.time(), "started": 0,
             "voice": voice_index_for(phone, assistant_mode), "audio": None,
             "ext_path": ext_path, "record_path": active_record_path(),
             "extension_path": active_extension_path()},
        )
        # The greeting uses the latest saved turn; ask_gemini loads the full
        # assistant-specific context lazily after the caller chooses an assistant.
        previous_answer = previous_turns[-1][1].strip() if previous_turns else ""
        topic_for_greeting = clean_for_tts(previous_topic)[:110]
        answer_for_greeting = clean_for_tts(previous_answer)[:110]
        if use_yiddish:
            if topic_for_greeting:
                context = " און מיר האבן גערעדט וועגן " + answer_for_greeting if answer_for_greeting else ""
                greeting = "שלום! לעצטע מאל האסטו געפרעגט וועגן " + topic_for_greeting + context + ". ווילסטו ממשיך זיין פון דארט, אדער רעדן וועגן עפעס ניי?"
            else:
                greeting = "שלום! איך בין דא דיר אויסצוהערן און העלפן. וועגן וואס ווילסטו היינט רעדן?"
        else:
            if topic_for_greeting:
                context = " ודיברנו גם על " + answer_for_greeting if answer_for_greeting else ""
                greeting = "שלום, בפעם הקודמת שאלת על " + topic_for_greeting + context + ". נרצה להמשיך מאותה נקודה, או שיש משהו חדש שתרצה לדבר עליו?"
            else:
                greeting = "שלום, אני כאן להקשיב ולעזור. על מה תרצה לדבר היום?"
        if assistant_mode == "tools":
            greeting = (
                "ברוכים הבאים למרכז כלי הבינה המלאכותית. "
                "אפשר לבקש מיילים, צ'אט חופשי, כתיבה, תרגום, סיכום, חיפוש מידע, לימוד או תכנון משימות. "
                "מה תרצה לעשות?"
            )
        elif assistant_mode == "astrology":
            greeting = (
                "ברוכים הבאים למפת האישיות האסטרולוגית שלך. "
                "כדי להתחיל, אמור את שמך הפרטי ואת יום, חודש ושנת הלידה שלך."
            )
        who = caller(phone).get("name", "")
        if assistant_mode == "edgy":
            greeting = ("אה, " + who + ". שוב אתה. טוב, מה רצית הפעם?") if who else "אה, שוב מישהו. טוב, מה רצית?"
        elif assistant_mode == "psychologist":
            greeting = ("שלום " + who + ", ") + "אני כאן ומקשיבה. מה עובר עליך היום?" if who else "שלום, אני כאן ומקשיבה. מה עובר עליך היום?"
        elif assistant_mode == "yeshiva" and not use_yiddish:
            greeting = ("בעזרת השם, שלום " + who + ". ") if who else "בעזרת השם, שלום. "
            greeting += "שב בני, ספר לי, מה על לבך?"
        st.setdefault("opening_greeting", greeting)
        st.setdefault("previous_topic", previous_topic)
        st.setdefault("previous_answer", previous_turns[-1][1].strip() if previous_turns else "")
        st["t"] = time.time()

        main_menu_active = bool(st.get("main_menu") or assistant_mode == "menu")
        if main_menu_active:
            st["main_menu"] = True
            if st.get("return_main_menu") and st.get("status") == "done":
                st["return_main_menu"] = False
                st["menu_stage"] = "root"
                st["tools_chat"] = False
                st["tools_busy"] = False
                st["tools_recording"] = False
                st["status"] = "idle"
                st.pop("tools_action", None)
                st.pop("utility_action", None)
                st["turn"] += 1
                return _digits(st, main_menu_text())
            menu_stage = st.get("menu_stage", "root")
            if menu_stage in ("root", "assistants"):
                return main_menu_area(st, p, call_id, phone, recorded)
            if menu_stage == "tools":
                assistant_mode = "tools"
            elif menu_stage == "utilities":
                return utilities_area(st, p, call_id)
            elif menu_stage == "utility_chat":
                assistant_mode = "tools"
                st["tools_chat"] = True
            elif menu_stage == "utility_map":
                assistant_mode = "tools"
                st["tools_chat"] = False
            elif menu_stage == "personal":
                assistant_mode = "personal"
            elif menu_stage == "forum":
                return forum_area(st, p, call_id, phone, recorded)
            elif menu_stage == "assistant_chat":
                assistant_mode = st.get("selected_assistant_mode") or "smart"
                if assistant_mode == "yiddish":
                    use_yiddish = True
                    _tls.yiddish = True
            else:
                st["menu_stage"] = "root"
                return _digits(st, main_menu_text())

        if assistant_mode == "personal":
            return phone_area(st, p, call_id, phone, recorded)

        if not cfg("system_enabled"):
            return Response("id_list_message=t-העוזר אינו זמין כרגע&go_to_folder=hangup", mimetype="text/plain")

        # First-time callers record their name once, before the first question.
        if phone and not caller(phone).get("name"):
            if not st.get("name_prompted"):
                st["name_prompted"] = True
                prompt = ("כדי איך זאל דיך קענען דערקענען ווייטער, זאג ביטע דיין נאמען." if use_yiddish
                          else "כדי שאוכל לזהות אותך בשיחות הבאות, אמור בבקשה את שמך הפרטי.")
                name_file = "n" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10]
                return Response(
                    f"read=t-{clean_for_tts(prompt)}=question,no,record,{active_record_path()},{name_file},no,yes,no",
                    mimetype="text/plain",
                )
            if st.get("name_status") == "processing":
                return Response(wait_command(), mimetype="text/plain")
            if recorded and not st.get("name_status"):
                st["name_status"] = "processing"
                threading.Thread(target=worker_name, args=(call_id, recorded, phone, use_yiddish), daemon=True).start()
                return Response(wait_command(), mimetype="text/plain")
            if st.get("name_status") == "done" and not st.get("name_question_prompted"):
                st["name_question_prompted"] = True
                prompt = ("א דאנק. וואס ווילסטו פרעגן?" if use_yiddish else "תודה. עכשיו אפשר לשאול את השאלה שלך.")
                return Response(read_command(prompt, call_id, st["turn"]), mimetype="text/plain")

        if assistant_mode == "tools" and not st.get("tools_chat"):
            tools_response = tools_area(st, p, call_id, phone, recorded, use_yiddish)
            if tools_response is not None:
                return tools_response

        if assistant_mode == "tools" and st.get("tools_chat") and st.get("return_tools_menu") and st.get("status") == "done":
            st["return_tools_menu"] = False
            if st.get("main_menu"):
                st["return_main_menu"] = True
                st["status"] = "idle"
                st["menu_stage"] = "root"
                st["turn"] += 1
                return _digits(st, main_menu_text())
            st["tools_chat"] = False
            st["status"] = "idle"
            st["turn"] += 1
            audio = st.get("audio")
            if audio:
                return Response(
                    f"id_list_message=f-{audio}&go_to_folder={active_extension_path()}",
                    mimetype="text/plain",
                )
            return _digits(st, tools_menu_text())

        # הקלטה חדשה של שאלה: מתחילים עיבוד ברקע ומשמיעים המתנה
        if recorded and recorded != st["last"]:
            st["last"] = recorded
            st["status"] = "processing"
            st["started"] = time.time()
            threading.Thread(
                target=worker,
                args=(call_id, recorded, phone, use_yiddish or assistant_mode == "yiddish",
                      assistant_mode, st.get("tools_action")),
                daemon=True,
            ).start()
            return Response(wait_command(), mimetype="text/plain")

        # עיבוד בעיצומו: פעולות יצירה עשויות לדרוש יותר זמן מהתשובה הקולית הרגילה.
        if st["status"] == "processing":
            processing_limit = 150 if assistant_mode == "tools" else 70
            if time.time() - st["started"] > processing_limit:
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
                cmd = f"read=f-{st['audio']}=question,no,record,{active_record_path()},{fname},no,yes,no"
                return Response(cmd, mimetype="text/plain")
            text = st["answer"] + " לשאלה נוספת דבר ובסיום הקש סולמית"
            return Response(read_command(text, call_id, st["turn"]), mimetype="text/plain")

            # First-time callers record their name once; it is saved against caller ID.
        if phone and not caller(phone).get("name"):
            if not st.get("name_prompted"):
                st["name_prompted"] = True
                prompt = ("כדי איך זאל דיך קענען דערקענען ווייטער, זאג ביטע דיין נאמען." if use_yiddish
                          else "כדי שאוכל לזהות אותך בשיחות הבאות, אמור בבקשה את שמך המלא.")
                name_file = "n" + re.sub(r"[^0-9a-zA-Z]", "", call_id)[:10]
                return Response(
                    f"read=t-{clean_for_tts(prompt)}=question,no,record,{active_record_path()},{name_file},no,yes,no",
                    mimetype="text/plain",
                )
            if st.get("name_status") == "processing":
                return Response(wait_command(), mimetype="text/plain")
            if st.get("name_status") == "done" and not st.get("name_question_prompted"):
                st["last"] = st.get("name_recording")
                st["name_question_prompted"] = True
                prompt = ("א דאנק. וואס ווילסטו פרעגן?" if use_yiddish else "תודה. עכשיו אפשר לשאול את השאלה שלך.")
                return Response(read_command(prompt, call_id, st["turn"]), mimetype="text/plain")
            if st.get("name_prompted") and recorded and recorded != st.get("name_recording") and not st.get("name_status"):
                st["name_status"] = "processing"
                threading.Thread(target=worker_name, args=(call_id, recorded, phone, use_yiddish), daemon=True).start()
                return Response(wait_command(), mimetype="text/plain")

        # Personalized opening for returning callers, general invitation for first-time callers.
        return Response(read_command(st.get("opening_greeting") or "שלום, אני כאן להמשיך מהמקום שבו עצרנו. מה תרצה להעלות היום?", call_id, st["turn"]), mimetype="text/plain")


# ---------------- ממשק ניהול ----------------
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ADMIN_EMAIL = os.environ.get("GOOGLE_ADMIN_EMAIL", "").strip().lower()  # כתובת המנהל מוגדרת במשתנה סביבה, לא בקוד
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
GOOGLE_LOGIN_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET and GOOGLE_REDIRECT_URI and google_id_token and google_auth_requests)
GEMINI_KEY_ENCRYPTION_KEY = os.environ.get("GEMINI_KEY_ENCRYPTION_KEY", "").strip()


def admin_auth_enabled():
    return bool(ADMIN_PASSWORD or GOOGLE_LOGIN_ENABLED)
app.secret_key = hashlib.sha256(
    ("sess|" + ADMIN_PASSWORD + "|" + MAIL_WEBHOOK_SECRET + "|" + YEMOT_PASSWORD).encode("utf-8")
).hexdigest()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=12 * 3600,
)
ADMIN_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "admin.html")
_login_fails = collections.deque()
_ip_fails = {}
_personal_ip_fails = {}
_personal_phone_fails = {}


@app.after_request
def admin_headers(resp):
    if request.path.startswith("/personal"):
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
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
        if not admin_auth_enabled() or not session.get("admin"):
            return jsonify({"error": "נדרשת כניסה"}), 401
        if request.method != "GET" and request.headers.get("X-Requested-With") != "admin":
            return jsonify({"error": "בקשה לא תקינה"}), 403
        return f(*a, **k)

    return wrapper


def pin_hash_value(pin, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 310000)
    return base64.urlsafe_b64encode(salt).decode("ascii") + "$" + base64.urlsafe_b64encode(digest).decode("ascii")


def verify_personal_pin(pin, stored):
    try:
        salt_b64, digest_b64 = stored.split("$", 1)
        salt = base64.urlsafe_b64decode(salt_b64.encode("ascii"))
        expected = base64.urlsafe_b64decode(digest_b64.encode("ascii"))
        actual = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, 310000)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


@app.route("/personal")
@app.route("/personal/")
def personal_page():
    return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "personal.html"), mimetype="text/html")


@app.route("/personal/login", methods=["POST"])
def personal_login():
    if request.headers.get("X-Requested-With") != "personal":
        return jsonify({"error": "בקשה לא תקינה"}), 403
    d = request.get_json(silent=True) or {}
    phone = re.sub(r"[^0-9]", "", str(d.get("phone", "")))[:15]
    pin = str(d.get("pin", ""))
    password = str(d.get("password", ""))
    if not re.fullmatch(r"\d{5,15}", phone):
        return jsonify({"error": "יש להזין מספר טלפון תקין"}), 400
    if password and (len(password) < 10 or len(password) > 128):
        return jsonify({"error": "הסיסמה אינה תקינה"}), 400
    if not password and not re.fullmatch(r"\d{6,12}", pin):
        return jsonify({"error": "יש להזין סיסמה או קוד אישי תקין"}), 400
    ip = client_ip()
    now = time.time()
    attempts = [t for t in _personal_ip_fails.get(ip, []) if now - t < 600]
    _personal_ip_fails[ip] = attempts
    if len(attempts) >= 8:
        return jsonify({"error": "יותר מדי ניסיונות כניסה. נסה שוב בעוד כמה דקות"}), 429
    pf = [t for t in _personal_phone_fails.get(phone, []) if now - t < 900]
    _personal_phone_fails[phone] = pf
    if len(pf) >= 6:
        return jsonify({"error": "יותר מדי ניסיונות כניסה. נסה שוב בעוד כמה דקות"}), 429
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "המערכת אינה זמינה כרגע"}), 503
    try:
        with conn.cursor() as cur:
            db_ensure_profile_columns(conn)
            cur.execute("SELECT pin_hash, enabled, password_hash, password_services FROM caller_profiles WHERE phone=%s", (phone,))
            row = cur.fetchone()
        conn.commit()  # Persist the additive profile migration.
        if not row or not row[1]:
            _personal_ip_fails[ip].append(now)
            _personal_phone_fails[phone].append(now)
            return jsonify({"error": "מספר הטלפון או פרטי הכניסה שגויים"}), 401
        password_hash = row[2] or ""
        if password_hash:
            services = [x.strip() for x in (row[3] or "website").split(",") if x.strip()]
            valid = bool(password) and "website" in services and verify_personal_pin(password, password_hash)
        else:
            valid = bool(pin) and bool(row[0]) and verify_personal_pin(pin, row[0])
        if not valid:
            _personal_ip_fails[ip].append(now)
            _personal_phone_fails[phone].append(now)
            return jsonify({"error": "מספר הטלפון או הסיסמה שגויים"}), 401
        _personal_ip_fails.pop(ip, None)
        _personal_phone_fails.pop(phone, None)
        session.clear()
        session["personal_phone"] = phone
        session.permanent = True
        return jsonify({"ok": True, "needs_password_setup": not bool(password_hash)})
    finally:
        conn.close()


@app.route("/personal/logout", methods=["POST"])
def personal_logout():
    session.pop("personal_phone", None)
    return jsonify({"ok": True})


@app.route("/personal/api/profile")
def personal_profile():
    phone = session.get("personal_phone")
    if not phone:
        return jsonify({"error": "נדרשת כניסה"}), 401
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "המערכת אינה זמינה כרגע"}), 503
    try:
        with conn.cursor() as cur:
            db_ensure_profile_columns(conn)
            cur.execute("SELECT phone, display_name, assigned_address, assistant_mode, tools_enabled, history_enabled, password_hash, password_services FROM caller_profiles WHERE phone=%s AND enabled=TRUE", (phone,))
            row = cur.fetchone()
            if not row:
                session.pop("personal_phone", None)
                return jsonify({"error": "החשבון אינו פעיל"}), 403
            transcripts = []
            if row[5]:
                cur.execute("SELECT created_at, heard, answer, assistant_mode FROM call_transcripts WHERE phone=%s ORDER BY created_at DESC, id DESC LIMIT 100", (phone,))
                transcripts = [{"created_at": x[0].isoformat(), "heard": x[1] or "", "answer": x[2] or "", "assistant_mode": x[3] or "smart"} for x in cur.fetchall()]
        conn.commit()  # Persist additive profile migration.
        return jsonify({"profile": {"phone": row[0], "name": row[1] or "", "assigned_address": row[2] or "", "assistant_mode": row[3] or "smart", "tools_enabled": bool(row[4]), "history_enabled": bool(row[5]), "has_password": bool(row[6]), "password_services": [x.strip() for x in (row[7] or "website").split(",") if x.strip()]}, "transcripts": transcripts})
    finally:
        conn.close()


@app.route("/personal/api/password", methods=["POST"])
def personal_set_password():
    phone = session.get("personal_phone")
    if not phone:
        return jsonify({"error": "נדרשת כניסה"}), 401
    if request.headers.get("X-Requested-With") != "personal":
        return jsonify({"error": "בקשה לא תקינה"}), 403
    d = request.get_json(silent=True) or {}
    old_password = str(d.get("old_password", ""))
    new_password = str(d.get("new_password", ""))
    services = d.get("services", [])
    allowed = {"yemot-ai-1", "yemot-smart-search", "website"}
    if not isinstance(services, list) or not services or any(x not in allowed for x in services):
        return jsonify({"error": "יש לבחור לפחות שירות אחד"}), 400
    if len(new_password) < 10 or len(new_password) > 128:
        return jsonify({"error": "הסיסמה חייבת להכיל 10 עד 128 תווים"}), 400
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn:
            with conn.cursor() as cur:
                db_ensure_profile_columns(conn)
                cur.execute("SELECT password_hash FROM caller_profiles WHERE phone=%s AND enabled=TRUE", (phone,))
                row = cur.fetchone()
                if not row:
                    return jsonify({"error": "החשבון אינו פעיל"}), 403
                if row[0] and not verify_personal_pin(old_password, row[0]):
                    return jsonify({"error": "הסיסמה הנוכחית שגויה"}), 401
                cur.execute(
                    "UPDATE caller_profiles SET password_hash=%s, password_services=%s, updated_at=now() WHERE phone=%s",
                    (pin_hash_value(new_password), ",".join(sorted(set(services))), phone),
                )
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/personal/api/pin", methods=["POST"])
def personal_change_pin():
    phone = session.get("personal_phone")
    if not phone:
        return jsonify({"error": "נדרשת כניסה"}), 401
    if request.headers.get("X-Requested-With") != "personal":
        return jsonify({"error": "בקשה לא תקינה"}), 403
    d = request.get_json(silent=True) or {}
    old_pin = str(d.get("old_pin", ""))
    new_pin = str(d.get("new_pin", ""))
    pf = [t for t in _personal_phone_fails.get(phone, []) if time.time() - t < 900]
    _personal_phone_fails[phone] = pf
    if len(pf) >= 6:
        return jsonify({"error": "יותר מדי ניסיונות. נסה שוב בעוד כמה דקות"}), 429
    if not re.fullmatch(r"\d{6,12}", new_pin):
        return jsonify({"error": "הקוד החדש חייב להכיל 6 עד 12 ספרות"}), 400
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn:
            with conn.cursor() as cur:
                db_ensure_profile_columns(conn)
                cur.execute("SELECT pin_hash FROM caller_profiles WHERE phone=%s AND enabled=TRUE", (phone,))
                row = cur.fetchone()
                if not row or not row[0] or not verify_personal_pin(old_pin, row[0]):
                    _personal_phone_fails[phone].append(time.time())
                    return jsonify({"error": "הקוד הנוכחי שגוי"}), 401
                cur.execute("UPDATE caller_profiles SET pin_hash=%s, updated_at=now() WHERE phone=%s", (pin_hash_value(new_pin), phone))
        return jsonify({"ok": True})
    finally:
        conn.close()


@app.route("/admin/api/caller/<phone>/pin", methods=["POST"])
@admin_required
def admin_set_personal_pin(phone):
    phone = re.sub(r"[^0-9]", "", str(phone))[:15]
    pin = str((request.get_json(silent=True) or {}).get("pin", ""))
    if not re.fullmatch(r"\d{6,12}", pin):
        return jsonify({"error": "הקוד חייב להכיל 6 עד 12 ספרות"}), 400
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn:
            with conn.cursor() as cur:
                db_ensure_profile_columns(conn)
                cur.execute("UPDATE caller_profiles SET pin_hash=%s, updated_at=now() WHERE phone=%s", (pin_hash_value(pin), phone))
                if not cur.rowcount:
                    return jsonify({"error": "המשתמש לא נמצא במסד הנתונים"}), 404
        return jsonify({"ok": True})
    finally:
        conn.close()


FORUM_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "forum.html")


@app.route("/forum")
@app.route("/forum/")
def public_forum_page():
    return send_file(FORUM_HTML, mimetype="text/html")


@app.route("/forum/api/posts")
def public_forum_posts():
    conn = db_connect()
    if conn is None:
        voice = list(_forum_memory)
        generated = list(_forum_content_memory)
        combined = []
        for post in voice:
            combined.append({
                "id": post.get("id"), "kind": "voice", "title": "הודעה קולית",
                "content_type": "voice", "content": "", "name": post.get("name") or "משתמש",
                "created_at": post.get("created_at") or "", "has_audio": bool(post.get("audio_path")),
                "has_image": False,
            })
        for post in generated:
            combined.append({
                "id": post.get("id"), "kind": "generated", "title": post.get("title") or "תוכן חדש",
                "content_type": post.get("content_type") or "text", "content": post.get("content_text") or "",
                "name": post.get("name") or "משתמש", "created_at": post.get("created_at") or "",
                "has_audio": bool(post.get("audio_path")), "has_image": bool(post.get("image_data")),
            })
        combined.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return jsonify({"items": combined[:50]})
    try:
        items = []
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, display_name, created_at
                   FROM voice_forum_posts ORDER BY created_at DESC, id DESC LIMIT 50"""
            )
            for row in cur.fetchall():
                items.append({
                    "id": row[0], "kind": "voice", "title": "הודעה קולית",
                    "content_type": "voice", "content": "", "name": row[1] or "משתמש",
                    "created_at": row[2].isoformat(), "has_audio": True, "has_image": False,
                })
            cur.execute(
                """SELECT id, display_name, title, content_type, content_text,
                          (audio_path <> ''), (image_data IS NOT NULL), created_at
                   FROM forum_content_posts ORDER BY created_at DESC, id DESC LIMIT 50"""
            )
            for row in cur.fetchall():
                items.append({
                    "id": row[0], "kind": "generated", "title": row[2] or "תוכן חדש",
                    "content_type": row[3] or "text", "content": row[4] or "",
                    "name": row[1] or "משתמש", "created_at": row[7].isoformat(),
                    "has_audio": bool(row[5]), "has_image": bool(row[6]),
                })
        items.sort(key=lambda item: item.get("created_at") or "", reverse=True)
        return jsonify({"items": items[:50]})
    except Exception as e:
        print("public forum listing failed:", redact(e)[:160], flush=True)
        return jsonify({"error": "טעינת הפורום נכשלה"}), 500
    finally:
        conn.close()


def download_forum_audio_path(audio_path):
    path = str(audio_path or "").strip()
    if not path:
        raise ValueError("missing forum audio path")
    if path.startswith("ivr2:"):
        yemot_path = path
    else:
        yemot_path = "ivr2:" + (path if path.startswith("/") else "/" + path)
    if not yemot_path.lower().endswith(".wav"):
        yemot_path += ".wav"
    response = requests.get(
        "https://www.call2all.co.il/ym/api/DownloadFile",
        params={"token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}", "path": yemot_path},
        timeout=25,
    )
    response.raise_for_status()
    if not response.content:
        raise RuntimeError("empty forum audio")
    return response.content


@app.route("/forum/api/posts/<kind>/<int:post_id>/audio")
def public_forum_audio(kind, post_id):
    conn = db_connect()
    if conn is None:
        memory = _forum_memory if kind == "voice" else _forum_content_memory
        item = next((post for post in memory if int(post.get("id") or 0) == post_id), None)
        if not item:
            return jsonify({"error": "קובץ השמע לא נמצא"}), 404
        audio_path = item.get("audio_path")
    else:
        try:
            with conn.cursor() as cur:
                if kind == "voice":
                    cur.execute("SELECT audio_path FROM voice_forum_posts WHERE id=%s", (post_id,))
                elif kind == "generated":
                    cur.execute("SELECT audio_path FROM forum_content_posts WHERE id=%s", (post_id,))
                else:
                    return jsonify({"error": "סוג פרסום לא תקין"}), 400
                row = cur.fetchone()
            if not row or not row[0]:
                return jsonify({"error": "קובץ השמע לא נמצא"}), 404
            audio_path = row[0]
        finally:
            conn.close()
    try:
        raw = download_forum_audio_path(audio_path)
        return send_file(io.BytesIO(raw), mimetype="audio/wav", as_attachment=False,
                         download_name=f"forum-{kind}-{post_id}.wav", max_age=60)
    except Exception as e:
        print("public forum audio failed:", redact(e)[:160], flush=True)
        return jsonify({"error": "השמעת קובץ השמע נכשלה"}), 502


@app.route("/forum/api/posts/<int:post_id>/image")
def public_forum_image(post_id):
    conn = db_connect()
    if conn is None:
        item = next((post for post in _forum_content_memory if int(post.get("id") or 0) == post_id), None)
        if not item or not item.get("image_data"):
            return jsonify({"error": "התמונה לא נמצאה"}), 404
        raw, mime_type = item["image_data"], item.get("image_mime") or "image/png"
    else:
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT image_data, image_mime FROM forum_content_posts WHERE id=%s", (post_id,))
                row = cur.fetchone()
            if not row or not row[0]:
                return jsonify({"error": "התמונה לא נמצאה"}), 404
            raw, mime_type = bytes(row[0]), row[1] or "image/png"
        finally:
            conn.close()
    return send_file(io.BytesIO(raw), mimetype=mime_type, as_attachment=False,
                     download_name=f"forum-image-{post_id}", max_age=300)


@app.route("/admin")
@app.route("/admin/")
def admin_page():
    return send_file(ADMIN_HTML, mimetype="text/html")


@app.route("/admin/api/whoami")
def admin_whoami():
    return jsonify({"enabled": admin_auth_enabled(), "auth": bool(session.get("admin")), "google_enabled": GOOGLE_LOGIN_ENABLED})


@app.route("/admin/google/login")
def admin_google_login():
    if not GOOGLE_LOGIN_ENABLED:
        return Response("התחברות Google עדיין לא הוגדרה בשרת", status=503, mimetype="text/plain")
    state = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
    session["google_oauth_state"] = state
    session.permanent = True
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    return Response(status=302, headers={"Location": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)})


@app.route("/admin/google/callback")
def admin_google_callback():
    if not GOOGLE_LOGIN_ENABLED:
        return Response("התחברות Google עדיין לא הוגדרה בשרת", status=503, mimetype="text/plain")
    expected = session.pop("google_oauth_state", "")
    supplied = request.args.get("state", "")
    code = request.args.get("code", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied) or not code:
        return Response("בקשת התחברות לא תקינה. חזור למסך הכניסה.", status=400, mimetype="text/plain")
    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        token_response.raise_for_status()
        id_token_value = token_response.json().get("id_token", "")
        if not id_token_value:
            raise ValueError("missing id_token")
        claims = google_id_token.verify_oauth2_token(
            id_token_value, google_auth_requests.Request(), GOOGLE_CLIENT_ID
        )
        email = str(claims.get("email", "")).strip().lower()
        if not claims.get("email_verified") or not email or not hmac.compare_digest(email, GOOGLE_ADMIN_EMAIL):
            print("Google admin login denied", flush=True)
            return Response("חשבון Google זה אינו מורשה לניהול.", status=403, mimetype="text/plain")
        session.clear()
        session["admin"] = True
        session["admin_email"] = email
        session.permanent = True
        return Response(status=302, headers={"Location": "/admin"})
    except Exception as e:
        print("Google OAuth callback failed:", redact(e)[:200], flush=True)
        return Response("ההתחברות עם Google נכשלה. נסה שוב.", status=400, mimetype="text/plain")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    if request.headers.get("X-Requested-With") != "admin":
        return jsonify({"error": "בקשה לא תקינה"}), 403
    if not ADMIN_PASSWORD:
        return jsonify({"error": "כניסה בסיסמה אינה מוגדרת. השתמש בהתחברות Google."}), 403
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


def key_cipher():
    if not GEMINI_KEY_ENCRYPTION_KEY:
        raise RuntimeError('מפתח הצפנה חסר')
    return Fernet(GEMINI_KEY_ENCRYPTION_KEY.encode('ascii'))

def key_pool_list():
    conn = db_connect()
    if conn is None: raise RuntimeError('מסד הנתונים אינו מחובר')
    try:
        with conn.cursor() as cur:
            cur.execute('SELECT id, label, key_fingerprint, assigned_phone, created_at, assigned_at, disabled FROM gemini_key_pool ORDER BY id DESC')
            return cur.fetchall()
    finally: conn.close()

def key_pool_add(label, key):
    conn = db_connect()
    if conn is None: raise RuntimeError('מסד הנתונים אינו מחובר')
    encrypted = key_cipher().encrypt(key.encode('utf-8')).decode('ascii')
    fingerprint = hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO gemini_key_pool (label, encrypted_key, key_fingerprint) VALUES (%s,%s,%s) ON CONFLICT (key_fingerprint) DO NOTHING RETURNING id', (label[:80], encrypted, fingerprint))
                return bool(cur.fetchone())
    finally: conn.close()


def key_pool_assign(phone):
    conn = db_connect()
    if conn is None: raise RuntimeError("מסד הנתונים אינו מחובר")
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("SELECT encrypted_key FROM gemini_key_pool WHERE assigned_phone=%s AND NOT disabled FOR UPDATE", (phone,))
                row = cur.fetchone()
                if not row:
                    cur.execute("SELECT id, encrypted_key FROM gemini_key_pool WHERE assigned_phone IS NULL AND NOT disabled ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1")
                    row = cur.fetchone()
                    if row:
                        cur.execute("UPDATE gemini_key_pool SET assigned_phone=%s, assigned_at=now() WHERE id=%s", (phone, row[0]))
                        encrypted = row[1]
                    else:
                        encrypted = None
                else:
                    encrypted = row[0]
        return key_cipher().decrypt(encrypted.encode("ascii")).decode("utf-8") if encrypted else ""
    finally:
        conn.close()


def key_pool_disable(key_id, disabled):
    conn = db_connect()
    if conn is None: raise RuntimeError('מסד הנתונים אינו מחובר')
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE gemini_key_pool SET disabled=%s WHERE id=%s', (bool(disabled), key_id))
                return cur.rowcount > 0
    finally: conn.close()

def key_mask(k):
    return ("••••" + k[-4:]) if k else ""


YIDDISH_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "yiddish.html")


@app.route("/admin/yiddish")
@admin_required
def admin_yiddish_page():
    return send_file(YIDDISH_HTML, mimetype="text/html")


@app.route("/admin/api/yiddish/chat", methods=["POST"])
@admin_required
def admin_yiddish_chat():
    d = request.get_json(silent=True) or {}
    message = str(d.get("message", "")).strip()
    history_in = d.get("history") or []
    if not message or len(message) > 2000 or not isinstance(history_in, list):
        return jsonify({"error": "שרייב א קורצע שאלה אין יידיש"}), 400
    contents = []
    for item in history_in[-10:]:
        if not isinstance(item, dict) or item.get("role") not in ("user", "model"):
            continue
        txt = str(item.get("text", "")).strip()[:1500]
        if txt:
            contents.append(types.Content(role=item["role"], parts=[types.Part(text=txt)]))
    contents.append(types.Content(role="user", parts=[types.Part(text=message)]))
    try:
        result = call_model(contents, types.GenerateContentConfig(system_instruction=YIDDISH_CHAT_PROMPT, max_output_tokens=600))
        answer = (result.text or "").strip()
        if not answer:
            raise RuntimeError("empty response")
        audio = ""
        try:
            wav = tts_wav(answer, cfg("default_voice"))
            audio = base64.b64encode(wav).decode("ascii")
        except Exception as e:
            print("Yiddish web TTS failed:", redact(e), flush=True)
        return jsonify({"answer": answer, "audio": audio})
    except Exception as e:
        print("Yiddish web chat failed:", redact(e), flush=True)
        return jsonify({"error": "דער שמועס איז יעצט נישט געלונגען. פרוביר נאכאמאל."}), 503


@app.route("/admin/api/state")
@admin_required
def admin_state():
    maybe_refresh()
    # Populate the admin directory from the persistent database, not only the current process cache.
    conn = db_connect()
    if conn is not None:
        try:
            with conn.cursor() as cur:
                db_ensure_profile_columns(conn)
                cur.execute(
                    """SELECT phone, display_name, enabled, assigned_address, email_address,
                              email_enabled, chat_enabled, assistant_mode, tools_enabled, history_enabled
                       FROM caller_profiles ORDER BY phone""")
                for row in cur.fetchall():
                    ph = str(row[0] or "")
                    current = dict(_store["callers"].get(ph) or {})
                    current.update({
                        "name": row[1] or current.get("name", ""),
                        "enabled": bool(row[2]),
                        "assigned_address": row[3] or "",
                        "email_address": row[4] or "",
                        "email_enabled": bool(row[5]) or bool(current.get("mail")),
                        "mail": bool(row[5]) or bool(current.get("mail")),
                        "chat_enabled": bool(row[6]),
                        "assistant_mode": row[7] or "smart",
                        "tools_enabled": bool(row[8]),
                        "history_enabled": bool(row[9])
                    })
                    _store["callers"][ph] = current
                    _caller_loaded.add(ph)
            conn.commit()  # Persist additive profile migration.
        except Exception as e:
            print("admin profile directory load failed:", redact(e), flush=True)
        finally:
            conn.close()
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
            "assigned_address": c.get("assigned_address", ""),
            "email_address": c.get("email_address", ""),
            "email_enabled": bool(c.get("email_enabled")),
            "chat_enabled": bool(c.get("chat_enabled")),
            "assistant_mode": c.get("assistant_mode", "smart"),
            "tools_enabled": bool(c.get("tools_enabled", True)),
            "history_enabled": bool(c.get("history_enabled", True)),
            "state": quota_state(ph if key else "default"),
        })
    try:
        pool_rows = key_pool_list()
        key_pool = [{"id": x[0], "label": x[1], "fingerprint": x[2], "assigned_phone": x[3],
                     "created_at": x[4].isoformat(), "assigned_at": x[5].isoformat() if x[5] else None,
                     "disabled": x[6]} for x in pool_rows]
        key_pool_error = ""
    except Exception as e:
        key_pool, key_pool_error = [], str(e)[:100]
    return jsonify({
        "key_pool": key_pool, "key_pool_error": key_pool_error,
        "uptime": now - START_TIME,
        "active_calls": sum(1 for x in calls.values() if now - x["t"] < 120),
        "store": {"source": _store["source"], "error": _store["error"]},
        "features": {"email": EMAIL_ENABLED, "webhook": WEBHOOK_ENABLED},
        "settings": {k: cfg(k) for k in settings_defaults()},
        "voices": VOICES,
        "default_key": {"mask": key_mask(GEMINI_API_KEY), "state": quota_state("default")},
        "callers": rows,
        "notifications": db_list_notifications(),
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
    for field, limit in (("assigned_address", 300), ("email_address", 254)):
        if field in d:
            cur[field] = str(d.get(field) or "").strip()[:limit]
    if "email_enabled" in d:
        cur["email_enabled"] = bool(d["email_enabled"])
        cur["mail"] = bool(d["email_enabled"])
    if "chat_enabled" in d:
        cur["chat_enabled"] = bool(d["chat_enabled"])
    if "assistant_mode" in d:
        allowed_modes = {"smart", "edgy", "psychologist", "yeshiva", "yiddish", "tools", "astrology", "central"}
        mode = str(d.get("assistant_mode") or "smart")
        if mode not in allowed_modes:
            return jsonify({"error": "סגנון העוזר אינו תקין"}), 400
        cur["assistant_mode"] = mode
    if "tools_enabled" in d:
        cur["tools_enabled"] = bool(d["tools_enabled"])
    if "history_enabled" in d:
        cur["history_enabled"] = bool(d["history_enabled"])
    if "enabled" in d:
        cur["enabled"] = bool(d["enabled"])
    if d.get("clear_mail_own"):
        cur.pop("mail_own", None)
    murl = str(d.get("mail_own_url", "")).strip()
    msecret = str(d.get("mail_own_secret", "")).strip()
    if murl or msecret:
        if not re.fullmatch(r"https://script\.google\.com/macros/s/[A-Za-z0-9_-]{10,200}/exec", murl):
            return jsonify({"error": "כתובת הסקריפט חייבת להיראות כך: https://script.google.com/macros/s/.../exec"}), 400
        if not 8 <= len(msecret) <= 100:
            return jsonify({"error": "הסיסמה של הסקריפט צריכה להיות באורך 8 עד 100 תווים"}), 400
        cur["mail_own"] = {"url": murl, "secret": enc_secret(msecret)}
    callers[phone] = cur
    _caller_loaded.add(phone)
    # Allocate an encrypted pool key immediately when an administrator registers a new caller.
    if not cur.get("key") and phone not in GEMINI_KEYS:
        try:
            allocated = key_pool_assign(phone)
            if not allocated:
                db_notify_key_pool_empty(phone)
        except Exception as e:
            print("admin caller key allocation failed:", redact(e), flush=True)
            db_notify_key_pool_empty(phone)
    warning = persist("callers", callers)
    if not db_save_caller(phone, cur) and DATABASE_URL:
        warning = (warning + " | " if warning else "") + "פרטי המתקשר לא נשמרו במסד הנתונים"
    return jsonify({"ok": True, "warning": warning})


@app.route("/admin/api/key_pool", methods=["POST"])
@admin_required
def admin_key_pool_add():
    d = request.get_json(silent=True) or {}
    key = str(d.get('key', '')).strip().replace(' ', '').replace(chr(10), '').replace(chr(13), '')
    label = str(d.get('label', '')).strip()[:80]
    if not re.fullmatch(r'[A-Za-z0-9._-]{20,200}', key): return jsonify({'error': 'מפתח Gemini לא תקין'}), 400
    try:
        if not key_pool_add(label, key): return jsonify({'error': 'המפתח כבר קיים'}), 409
        return jsonify({'ok': True})
    except Exception as e:
        print('key pool add failed:', redact(e), flush=True)
        return jsonify({'error': 'שמירת המפתח נכשלה. בדוק חיבור והצפנה.'}), 503

@app.route("/admin/api/key_pool/<int:key_id>", methods=["PUT"])
@admin_required
def admin_key_pool_update(key_id):
    d = request.get_json(silent=True) or {}
    try:
        if not key_pool_disable(key_id, d.get('disabled')): return jsonify({'error': 'המפתח לא נמצא'}), 404
        return jsonify({'ok': True})
    except Exception as e:
        print('key pool update failed:', redact(e), flush=True)
        return jsonify({'error': 'עדכון המפתח נכשל'}), 503

@app.route("/admin/api/notifications/<int:notification_id>", methods=["POST"])
@admin_required
def admin_notification_resolve(notification_id):
    if not db_resolve_notification(notification_id):
        return jsonify({"error": "לא ניתן לעדכן את ההתראה"}), 503
    return jsonify({"ok": True})


@app.route("/admin/api/database")
@admin_required
def admin_database_status():
    conn = db_connect()
    if conn is None:
        return jsonify({"connected": False, "error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT (SELECT count(*) FROM caller_profiles), (SELECT count(*) FROM call_transcripts), now()")
            row = cur.fetchone()
        return jsonify({"connected": True, "caller_profiles": row[0], "transcripts": row[1], "checked_at": row[2].isoformat()})
    except Exception as e:
        print("database status failed:", redact(e), flush=True)
        return jsonify({"connected": False, "error": "בדיקת מסד הנתונים נכשלה"}), 503
    finally:
        conn.close()


@app.route("/admin/api/transcripts")
@admin_required
def admin_transcripts():
    phone = re.sub(r"[^0-9]", "", request.args.get("phone", ""))[:15]
    name = request.args.get("name", "").strip()[:40]
    date_from = request.args.get("from", "").strip()
    date_to = request.args.get("to", "").strip()
    assistant_mode_filter = request.args.get("assistant_mode", "").strip()
    try:
        limit = min(1000, max(1, int(request.args.get("limit", "500"))))
    except ValueError:
        limit = 100
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        db_ensure_transcript_columns(conn)
        clauses, params = [], []
        if phone:
            clauses.append("phone = %s"); params.append(phone)
        if name:
            clauses.append("caller_name ILIKE %s"); params.append("%" + name + "%")
        if date_from:
            clauses.append("created_at >= %s::date"); params.append(date_from)
        if date_to:
            clauses.append("created_at < (%s::date + interval '1 day')"); params.append(date_to)
        if assistant_mode_filter in {"smart", "edgy", "psychologist", "yeshiva", "yiddish", "tools", "astrology", "central"}:
            clauses.append("assistant_mode = %s"); params.append(assistant_mode_filter)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, call_id, phone, caller_name, created_at, heard, answer, assistant_mode FROM call_transcripts"
                + where + " ORDER BY created_at DESC LIMIT %s",
                params + [limit],
            )
            rows = cur.fetchall()
        conn.commit()
        return jsonify({"items": [
            {"id": r[0], "call_id": r[1], "phone": r[2], "name": r[3],
             "created_at": r[4].isoformat(), "heard": r[5], "answer": r[6], "assistant_mode": r[7] or "smart"}
            for r in rows
        ]})
    except Exception as e:
        print("transcript search failed:", redact(e), flush=True)
        return jsonify({"error": "לא ניתן לחפש בארכיון כרגע"}), 500
    finally:
        conn.close()


@app.route("/admin/api/caller/<phone>/mail_test", methods=["POST"])
@admin_required
def admin_mail_test(phone):
    phone = re.sub(r"\D", "", phone)[:15]
    own = own_mail(phone)
    if not own:
        return jsonify({"ok": False, "error": "לא הוגדרה תיבה אישית לפרופיל הזה"})
    try:
        r = requests.post(own["url"], json={"secret": dec_secret(own["secret"]), "action": "search", "query": "in:inbox", "max": 1}, timeout=25)
        r.raise_for_status()
        if r.text.strip() == "forbidden":
            return jsonify({"ok": False, "error": "הסיסמה לא תואמת לסקריפט"})
        data = json.loads(r.text)
        if "messages" not in data:
            return jsonify({"ok": False, "error": "הסקריפט ענה אבל לא מכיר את הפעולות. הדבק את הקוד המלא מהדף"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": redact(str(e))[:150]})


MAIL_SCRIPT = """const SECRET = 'PUT-A-LONG-RANDOM-PASSWORD-HERE';

function out(o) { return ContentService.createTextOutput(JSON.stringify(o)).setMimeType(ContentService.MimeType.JSON); }

function doPost(e) {
  let d;
  try { d = JSON.parse(e.postData.contents); } catch (err) { return ContentService.createTextOutput('bad request'); }
  if (d.secret !== SECRET) return ContentService.createTextOutput('forbidden');
  try {
    if (d.action === 'send') {
      GmailApp.sendEmail(d.to, d.subject || 'הודעה', d.body || '');
      return ContentService.createTextOutput('ok');
    }
    if (d.action === 'send_attachment') {
      const bytes = Utilities.base64Decode(d.content || '');
      const blob = Utilities.newBlob(bytes, d.mimeType || 'application/octet-stream', d.filename || 'attachment.bin');
      GmailApp.sendEmail(d.to, d.subject || 'הודעה', d.body || '', {attachments: [blob]});
      return out({ok: true});
    }
    if (d.action === 'attachments') {
      const message = GmailApp.getMessageById(d.id);
      const all = message.getAttachments({includeInlineImages: false, includeAttachments: true});
      const items = [];
      let tooLarge = 0;
      all.slice(0, 5).forEach(function (a) {
        const bytes = a.getBytes();
        if (bytes.length > 6 * 1024 * 1024) { tooLarge++; return; }
        items.push({
          filename: a.getName(),
          mimeType: a.getContentType(),
          data: Utilities.base64Encode(bytes)
        });
      });
      return out({attachments: items, tooLarge: tooLarge});
    }
    if (d.action === 'search') {
      const th = GmailApp.search(d.query || 'in:inbox', 0, Math.min(d.max || 5, 10));
      return out({ messages: th.map(function (t) {
        const m = t.getMessages()[t.getMessageCount() - 1];
        return { id: m.getId(), from: m.getFrom(), subject: m.getSubject(),
          date: Utilities.formatDate(m.getDate(), Session.getScriptTimeZone(), 'dd/MM HH:mm'),
          unread: m.isUnread(), snippet: m.getPlainBody().replace(/\\s+/g, ' ').substring(0, 150) };
      }) });
    }
    if (d.action === 'read') {
      const m = GmailApp.getMessageById(d.id);
      return out({ from: m.getFrom(), subject: m.getSubject(), body: m.getPlainBody().replace(/\\s+/g, ' ').substring(0, 3000) });
    }
    if (d.action === 'find_contact') {
      const found = {};
      const q = String(d.name || '').toLowerCase();
      GmailApp.search('from:' + d.name + ' OR to:' + d.name, 0, 10).forEach(function (t) {
        t.getMessages().forEach(function (m) {
          [m.getFrom(), m.getTo()].forEach(function (f) {
            String(f).split(',').forEach(function (part) {
              const mt = part.match(/^\\s*"?([^"<]*?)"?\\s*<([^>]+)>\\s*$/);
              const nm = mt ? mt[1].trim() : '';
              const em = mt ? mt[2].trim() : part.trim();
              if (em.indexOf('@') > 0 && (nm.toLowerCase().indexOf(q) >= 0 || em.toLowerCase().indexOf(q) >= 0)) found[em] = nm || em;
            });
          });
        });
      });
      return out({ contacts: Object.keys(found).slice(0, 5).map(function (em) { return { name: found[em], email: em }; }) });
    }
    return out({ error: 'unknown action' });
  } catch (err) { return out({ error: String(err) }); }
}
"""


@app.route("/admin/api/mail_script")
@admin_required
def admin_mail_script():
    return Response(MAIL_SCRIPT, mimetype="text/plain")


@app.route("/admin/api/caller/<phone>", methods=["GET"])
@admin_required
def admin_caller_detail(phone):
    phone = re.sub(r"[^0-9]", "", str(phone))[:15]
    if not phone:
        return jsonify({"error": "מספר טלפון לא תקין"}), 400
    db_load_caller(phone)
    profile = dict(caller(phone))
    transcripts, alerts, db_error = [], [], ""
    conn = db_connect()
    if conn is None:
        db_error = "מסד הנתונים אינו מחובר, ולכן תמלולי שיחות לא נשמרים לצמיתות"
    else:
        try:
            with conn.cursor() as cur:
                db_ensure_profile_columns(conn)
                cur.execute(
                    """SELECT id, call_id, caller_name, created_at, heard, answer, assistant_mode
                       FROM call_transcripts WHERE phone=%s
                       ORDER BY created_at DESC, id DESC LIMIT 200""", (phone,))
                transcripts = [{"id": r[0], "call_id": r[1], "name": r[2] or "",
                                "created_at": r[3].isoformat(), "heard": r[4] or "", "answer": r[5] or "",
                                "assistant_mode": r[6] or "smart"}
                               for r in cur.fetchall()]
                cur.execute(
                    """SELECT id, kind, message, created_at FROM admin_notifications
                       WHERE phone=%s AND resolved_at IS NULL
                       ORDER BY created_at DESC LIMIT 50""", (phone,))
                alerts = [{"id": r[0], "kind": r[1], "message": r[2], "created_at": r[3].isoformat()} for r in cur.fetchall()]
            conn.commit()
        except Exception as e:
            print("caller detail failed:", redact(e), flush=True)
            db_error = "לא ניתן לטעון את התמלולים כרגע"
        finally:
            conn.close()
    errors = [{"t": e["t"], "kind": e.get("kind", ""), "err": e.get("err", "")}
              for e in events if e.get("phone") == phone and not e.get("ok")][:30]
    key_state = "personal" if all_keys().get(phone) else ("pool" if key_pool_has(phone) else "shared")
    own = own_mail(phone)
    return jsonify({
        "profile": {
            "phone": phone, "name": profile.get("name", ""),
            "assigned_address": profile.get("assigned_address", ""),
            "email_address": profile.get("email_address", ""),
            "email_enabled": bool(profile.get("email_enabled")),
            "chat_enabled": bool(profile.get("chat_enabled")),
            "assistant_mode": profile.get("assistant_mode", "smart"),
            "tools_enabled": bool(profile.get("tools_enabled", True)),
            "history_enabled": bool(profile.get("history_enabled", True)),
            "enabled": bool(profile.get("enabled", True)),
            "key_state": key_state,
            "key_mask": key_mask(all_keys().get(phone, "")),
            "mail_own": bool(own),
            "mail_global": bool(EMAIL_ENABLED and phone_allowed(phone)),
        },
        "transcripts": transcripts, "alerts": alerts, "errors": errors, "db_error": db_error,
    })


@app.route("/admin/api/caller/<phone>", methods=["DELETE"])
@admin_required
def admin_caller_delete(phone):
    callers = {k: dict(v) for k, v in _store["callers"].items()}
    if phone not in callers:
        return jsonify({"error": "הקו לא נמצא"}), 404
    callers.pop(phone)
    warning = persist("callers", callers)
    if not db_delete_caller(phone) and DATABASE_URL:
        warning = (warning + " | " if warning else "") + "פרטי המתקשר לא נמחקו ממסד הנתונים"
    return jsonify({"ok": True, "warning": warning})


@app.route("/admin/api/music/upload", methods=["POST"])
@admin_required
def admin_music_upload():
    language = request.args.get("lang", "he")
    if language not in ("he", "yi"):
        return jsonify({"error": "בחירת שפה לא תקינה"}), 400
    audio = request.files.get("file")
    if not audio or not audio.filename or not audio.filename.lower().endswith(".mp3"):
        return jsonify({"error": "יש לבחור קובץ MP3"}), 400
    data = audio.read(15 * 1024 * 1024 + 1)
    if not data or len(data) > 15 * 1024 * 1024:
        return jsonify({"error": "הקובץ ריק או גדול מ־15MB"}), 400
    folder = "/1/1" if language == "yi" else "/8"
    target = f"ivr2:{folder}/hold_music.wav"
    try:
        response = requests.post(
            "https://www.call2all.co.il/ym/api/UploadFile",
            data={"token": f"{YEMOT_NUMBER}:{YEMOT_PASSWORD}", "path": target, "convertAudio": "1"},
            files={"file": ("hold_music.mp3", data, "audio/mpeg")},
            timeout=90,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("responseStatus") != "OK":
            raise RuntimeError(result.get("message", "Yemot upload failed"))
        new = dict(_store["settings"])
        new["music_file_" + language] = folder + "/hold_music"
        warning = persist("settings", new)
        if warning:
            return jsonify({"ok": True, "warning": warning, "path": target})
        return jsonify({"ok": True, "path": target})
    except Exception as e:
        print("admin music upload failed:", redact(e), flush=True)
        return jsonify({"error": "העלאת המנגינה נכשלה. בדוק את החיבור לימות המשיח ונסה שוב."}), 502



@app.route("/admin/api/audio", methods=["GET"])
@admin_required
def admin_audio_list():
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT filename, size_bytes, created_at
                   FROM admin_audio_files WHERE project_key=%s
                   ORDER BY filename""",
                (AUDIO_PROJECT,))
            rows = cur.fetchall()
        audio_labels = {
            1: "תפריט ראשי", 2: "עוזר מרכזי", 3: "בחירת סוג בינה", 4: "הבחירה נקלטה",
            5: "העוזר אינו זמין", 6: "תפריט כלי AI", 7: "התחלת כלי", 8: "סיום כלי",
            9: "תפריט חיפוש", 10: "החיפוש מתבצע", 11: "לא נמצאו תוצאות",
            12: "אזור אישי", 13: "היסטוריה", 14: "העדפות", 15: "תפריט ניהול",
            16: "ניהול משתמשים", 17: "ניהול API", 18: "סטטיסטיקות", 19: "שגיאות",
            20: "ניהול כלים", 21: "נא להמתין", 22: "הודעת שגיאה", 23: "לא זמין",
            24: "חזרה", 25: "תפריט ראשי נוסף", 26: "סיום שיחה", 27: "בחירה לא תקינה",
            28: "עצירה", 29: "המשך", 30: "ניווט", 31: "פורום קולי",
            32: "הקלטה לפורום", 33: "תפריט מדיה", 34: "הסכת", 35: "בדיקה יומית",
            36: "הבדיקה הצליחה", 37: "הבדיקה נכשלה"
        }
        def audio_item(r):
            match = re.match(r"^(\d{2})-", str(r[0]))
            number = int(match.group(1)) if match else None
            label = audio_labels.get(number) if number is not None else None
            if not label:
                label = "לא זוהתה הודעה" if number is not None and number <= 40 else "קובץ ללא שיוך"
            return {"filename": r[0], "catalog_label": label,
                    "size_bytes": int(r[1]), "created_at": r[2].isoformat()}
        return jsonify({"items": [audio_item(r) for r in rows]})
    except Exception as e:
        print("admin audio list failed:", redact(e), flush=True)
        return jsonify({"error": "טעינת קובצי השמע נכשלה"}), 500
    finally:
        conn.close()


@app.route("/admin/api/audio", methods=["POST"])
@admin_required
def admin_audio_upload():
    uploaded = request.files.get("file")
    if not uploaded or not uploaded.filename:
        return jsonify({"error": "בחר קובץ ZIP או MP3"}), 400
    raw = uploaded.read(25 * 1024 * 1024 + 1)
    if not raw or len(raw) > 25 * 1024 * 1024:
        return jsonify({"error": "הקובץ ריק או גדול מ־25MB"}), 413

    files = []
    if uploaded.filename.lower().endswith(".mp3"):
        files = [(re.sub(r"[^A-Za-z0-9._-]+", "-", uploaded.filename.rsplit("/", 1)[-1]).strip(".-"), raw)]
    elif uploaded.filename.lower().endswith(".zip"):
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                entries = [x for x in archive.infolist() if not x.is_dir()]
                if len(entries) > 100:
                    return jsonify({"error": "בארכיון יש יותר מ־100 קבצים"}), 400
                total = sum(x.file_size for x in entries)
                if total > 100 * 1024 * 1024:
                    return jsonify({"error": "גודל הקבצים לאחר חילוץ חורג מ־100MB"}), 413
                seen = set()
                for entry in entries:
                    path = entry.filename.replace("\\", "/")
                    parts = path.split("/")
                    if path.startswith("/") or any(part == ".." for part in parts):
                        return jsonify({"error": "הארכיון מכיל נתיב קובץ לא תקין"}), 400
                    base = parts[-1]
                    if not base.lower().endswith(".mp3"):
                        continue
                    name = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip(".-")
                    if not name or not name.lower().endswith(".mp3") or entry.file_size <= 0 or entry.file_size > 15 * 1024 * 1024:
                        return jsonify({"error": "נמצא קובץ MP3 ריק, גדול מדי או בעל שם לא תקין"}), 400
                    if name in seen:
                        return jsonify({"error": "יש שמות קבצים כפולים בארכיון: " + name}), 400
                    seen.add(name)
                    files.append((name, archive.read(entry)))
        except (zipfile.BadZipFile, RuntimeError, OSError):
            return jsonify({"error": "קובץ ZIP פגום או לא ניתן לחילוץ"}), 400
    else:
        return jsonify({"error": "יש לבחור קובץ ZIP או MP3"}), 400

    if not files:
        return jsonify({"error": "לא נמצאו קובצי MP3 בארכיון"}), 400
    if len(files) > 100 or sum(len(data) for _, data in files) > 100 * 1024 * 1024:
        return jsonify({"error": "היקף קובצי השמע חורג מהמותר"}), 413

    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                for filename, data in files:
                    cur.execute(
                        """INSERT INTO admin_audio_files
                           (project_key, filename, content_type, data, size_bytes, created_at)
                           VALUES (%s, %s, 'audio/mpeg', %s, %s, now())
                           ON CONFLICT (project_key, filename) DO UPDATE SET
                             content_type=EXCLUDED.content_type, data=EXCLUDED.data,
                             size_bytes=EXCLUDED.size_bytes, created_at=now()""",
                        (AUDIO_PROJECT, filename, data, len(data)))
        return jsonify({"ok": True, "uploaded": len(files), "files": [x[0] for x in files]})
    except Exception as e:
        print("admin audio upload failed:", redact(e), flush=True)
        return jsonify({"error": "שמירת קובצי השמע נכשלה"}), 500
    finally:
        conn.close()


@app.route("/admin/api/audio/<filename>", methods=["GET"])
@admin_required
def admin_audio_get(filename):
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT data FROM admin_audio_files WHERE project_key=%s AND filename=%s",
                (AUDIO_PROJECT, filename))
            row = cur.fetchone()
        if not row:
            return jsonify({"error": "קובץ השמע לא נמצא"}), 404
        return send_file(io.BytesIO(bytes(row[0])), mimetype="audio/mpeg",
                         as_attachment=False, download_name=filename, max_age=0)
    except Exception as e:
        print("admin audio playback failed:", redact(e), flush=True)
        return jsonify({"error": "השמעת קובץ השמע נכשלה"}), 500
    finally:
        conn.close()


@app.route("/admin/api/audio/<filename>", methods=["DELETE"])
@admin_required
def admin_audio_delete(filename):
    conn = db_connect()
    if conn is None:
        return jsonify({"error": "מסד הנתונים אינו מחובר"}), 503
    try:
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM admin_audio_files WHERE project_key=%s AND filename=%s",
                    (AUDIO_PROJECT, filename))
                deleted = cur.rowcount
        if not deleted:
            return jsonify({"error": "קובץ השמע לא נמצא"}), 404
        return jsonify({"ok": True})
    except Exception as e:
        print("admin audio delete failed:", redact(e), flush=True)
        return jsonify({"error": "מחיקת קובץ השמע נכשלה"}), 500
    finally:
        conn.close()


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


def db_inventory_log(conn):
    """Read-only: logs a non-secret fingerprint of the database and row counts, to compare services."""
    try:
        import urllib.parse as _up
        u = _up.urlparse(DATABASE_URL)
        ident = hashlib.sha256(f"{u.hostname}/{u.path}".encode()).hexdigest()[:10]
        parts = [f"db={ident}", f"host={(u.hostname or '')[:12]}..."]
        with conn.cursor() as cur:
            for t in ("caller_profiles", "call_transcripts", "gemini_key_pool", "admin_audio_files",
                      "voice_forum_posts", "admin_notifications"):
                try:
                    cur.execute("SELECT count(*) FROM " + t)
                    parts.append(f"{t}={cur.fetchone()[0]}")
                except Exception:
                    conn.rollback()
                    parts.append(f"{t}=missing")
            try:
                cur.execute("SELECT count(DISTINCT call_id), count(DISTINCT phone) FROM call_transcripts")
                c, ph = cur.fetchone()
                parts.append(f"calls={c} phones={ph}")
            except Exception:
                conn.rollback()
        print("DB inventory: " + " ".join(parts), flush=True)
    except Exception as e:
        print("DB inventory failed:", type(e).__name__, flush=True)


def voicepack_tts(text, voice):
    # Generate prerecorded prompts only with Google's free-tier TTS model;
    # do not fall back to an environment-selected model that may be billed.
    previous_models = getattr(_tls, "tts_models_override", None)
    previous_flag = getattr(_tls, "voicepack_tts", False)
    _tls.tts_models_override = ["gemini-3.8-flash-tts"]
    _tls.voicepack_tts = True
    try:
        return tts_wav(text, voice)
    finally:
        if previous_models is None:
            try:
                delattr(_tls, "tts_models_override")
            except AttributeError:
                pass
        else:
            _tls.tts_models_override = previous_models
        _tls.voicepack_tts = previous_flag


def save_voicepack_archive(archive, state):
    conn = db_connect()
    if conn is None:
        print("voicepack archive not persisted: database unavailable", flush=True)
        return False
    try:
        status_bytes = json.dumps(state, ensure_ascii=False).encode("utf-8")
        with conn.transaction():
            with conn.cursor() as cur:
                for filename, content_type, data in (
                    ("yemot-voicepack.zip", "application/zip", archive),
                    ("yemot-voicepack-status.json", "application/json", status_bytes),
                ):
                    cur.execute(
                        """INSERT INTO admin_audio_files
                           (project_key, filename, content_type, data, size_bytes, created_at)
                           VALUES ('voicepack', %s, %s, %s, %s, now())
                           ON CONFLICT (project_key, filename) DO UPDATE SET
                           content_type=EXCLUDED.content_type, data=EXCLUDED.data,
                           size_bytes=EXCLUDED.size_bytes, created_at=now()""",
                        (filename, content_type, data, len(data)),
                    )
        print("voicepack archive saved to database:", len(archive), "bytes", flush=True)
        return True
    except Exception as e:
        print("voicepack archive save failed:", redact(e)[:180], flush=True)
        return False
    finally:
        conn.close()


def load_voicepack_archive():
    conn = db_connect()
    if conn is None:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT filename, data FROM admin_audio_files
                   WHERE project_key='voicepack'
                   AND filename IN ('yemot-voicepack.zip', 'yemot-voicepack-status.json')"""
            )
            rows = cur.fetchall()
        saved = {name: bytes(data) for name, data in rows}
        archive = saved.get("yemot-voicepack.zip")
        status_data = None
        if saved.get("yemot-voicepack-status.json"):
            try:
                status_data = json.loads(saved["yemot-voicepack-status.json"].decode("utf-8"))
            except Exception:
                status_data = None
        return {"archive": archive, "status": status_data}
    except Exception as e:
        print("voicepack archive load failed:", redact(e)[:180], flush=True)
        return None
    finally:
        conn.close()


voicepack.register(
    app, admin_required, voicepack_tts, flask,
    on_complete=save_voicepack_archive, load_archive=load_voicepack_archive,
)


# Run additive schema setup during startup rather than waiting for the first
# caller request. This creates missing tables/columns (including the voice
# forum table) while preserving all existing rows.
_startup_db_conn = db_connect()
if _startup_db_conn is not None:
    try:
        validate_gemini_key_pool(_startup_db_conn)
        db_inventory_log(_startup_db_conn)
    finally:
        _startup_db_conn.close()

maybe_refresh()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
