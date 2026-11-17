"""חבילת הקראות קוליות: 40 הודעות בעברית בקול גברי, להעלאה לימות המשיח.

המודול עצמאי: טקסטי ההודעות, יצירת השמע, המרה לפורמט של ימות (WAV 8kHz מונו 16 ביט) ו-ZIP.
השרת רק מחבר אליו את פונקציית ה-TTS ואת בדיקת ההרשאה (ראו register בסוף הקובץ).
"""
import array
import csv
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
import wave
import zipfile

# (מספר, שם קובץ, היכן משתמשים, טקסט להקראה)
MESSAGES = [
    ("welcome", "פתיחה", "ברוכים הבאים למערכת הבינה המלאכותית. כאן אפשר לשוחח עם סוגים שונים של עוזרים חכמים, להשתמש בכלים שימושיים ולנהל אזור אישי."),
    ("main_menu", "תפריט ראשי", "לעוזר המרכזי עם גישה לכל הכלים הקש אפס. לתפריט העוזרים הקש אחת. לתפריט כלי הבינה המלאכותית הקש שתיים. לכלים שימושיים הקש שלוש. לאזור האישי הקש ארבע. לניהול המערכת הקש חמש. לפורום הקולי בעיוות קול הקש שש. להמשך השיחה האחרונה הקש שבע. לשמיעת התפריט מחדש הקש תשע. לחזרה לתפריט הקודם הקש כוכבית. לחזרה לתפריט הראשי הקש סולמית."),
    ("menu_unknown", "בחירה לא מזוהה", "לא זיהיתי את הבחירה. אנא נסה שוב."),
    ("admin_menu", "תפריט ניהול", "תפריט ניהול המערכת. לבדיקת מצב המערכת הקש אחת. להפעלה או כיבוי הקש שתיים. להגדרות המענה הקולי הקש שלוש. להחלפת קול ברירת המחדל הקש ארבע. להתראות ושגיאות הקש חמש. לתמלולי שיחות אחרונות הקש שש. לחסימת משתמש הקש שבע. לפתיחת חסימה הקש שמונה. לחזרה לתפריט הראשי הקש אפס. לכוכבית לחזרה לתפריט הקודם ולסולמית לתפריט הראשי."),
    ("assistants_menu", "תפריט העוזרים", "תפריט העוזרים. לבינה החכמה הקש אחת. לבינה הישירה הקש שתיים. לבינה לשיחה והקשבה הקש שלוש. לבינת הישיבה הקש ארבע. לבינה ביידיש הקש חמש. למפת האישיות הקש שש. לחזרה לתפריט הראשי הקש אפס. לכוכבית לחזרה לתפריט הקודם ולסולמית לתפריט הראשי."),
    ("smart_intro", "פתיחת הבינה החכמה", "בחרת בבינה החכמה. היא עונה על שאלות, מסבירה, מחפשת מידע ועוזרת בכל נושא. אמור את בקשתך בקול ברור לאחר הצליל. כדי לחזור לתפריט הראשי אמור: חזרה לתפריט."),
    ("edgy_intro", "פתיחת הבינה הישירה", "בחרת בבינה הישירה. היא עונה בחדות ובהומור, בלי לפגוע באיש. אמור את בקשתך לאחר הצליל. כדי לחזור לתפריט הראשי אמור: חזרה לתפריט."),
    ("psychologist_intro", "פתיחת בינה לשיחה והקשבה", "בחרת בבינה לשיחה והקשבה. היא כאן כדי להקשיב ולעזור לך לחשוב על הדברים. היא אינה תחליף לאיש מקצוע. אמור מה על ליבך לאחר הצליל."),
    ("yeshiva_intro", "פתיחת בינת הישיבה", "בחרת בבינת הישיבה. היא עונה בסגנון של שיחה בבית המדרש, בשפה מכבדת וברורה. אמור את שאלתך לאחר הצליל."),
    ("yiddish_intro", "הסבר על הבינה ביידיש", "בחרת בבינה ביידיש. אפשר לדבר איתה ביידיש והיא תענה ביידיש. כדי לחזור לתפריט הראשי אמור: חזרה לתפריט."),
    ("astrology_intro", "פתיחת מפת האישיות", "ברוכים הבאים למפת האישיות. כדי להתחיל, אמור את שמך הפרטי ואת יום, חודש ושנת הלידה שלך."),
    ("tools_menu", "תפריט כלי AI", "ברוכים הבאים למרכז כלי הבינה המלאכותית. לשימוש חופשי בבינה עם כל הכלים הקש אפס. למיילים הקש אחת. ליצירת הסכת הקש שתיים. ליצירת תמונה ושליחתה למייל הקש שלוש. לקריאת קובץ באמצעות בינה מלאכותית הקש ארבע. לכתיבה וניסוח הקש חמש. לחיפוש ומחקר הקש שש. לתכנון וחישובים הקש שבע. לשמיעת התפריט מחדש הקש תשע. לכוכבית לחזרה לתפריט הקודם ולסולמית לתפריט הראשי."),
    ("tools_free", "כלים: שימוש חופשי", "בחרת בשימוש חופשי. אמור מה תרצה שהבינה תעשה בשבילך."),
    ("tools_mail", "כלים: מיילים", "בחרת במיילים. אפשר לבקש לשמוע מיילים חדשים, לחפש מייל או לכתוב ולשלוח מייל. אמור מה תרצה לעשות."),
    ("tools_podcast", "כלים: הסכת", "בחרת ביצירת הסכת. אמור על איזה נושא תרצה הסכת קצר, והוא ייווצר ויישלח אליך."),
    ("tools_image", "כלים: תמונה", "בחרת ביצירת תמונה. תאר במילים איזו תמונה ליצור, והיא תישלח למייל שלך."),
    ("tools_file", "כלים: קריאת קובץ", "בחרת בקריאת קובץ. הקובץ יישלח למייל שלך, והבינה תקרא ותסכם אותו."),
    ("tools_writing", "כלים: כתיבה", "בחרת בכתיבה וניסוח. אמור מה לכתוב, למי ובאיזה סגנון."),
    ("tools_research", "כלים: חיפוש ומחקר", "בחרת בחיפוש ומחקר. אמור מה תרצה לברר, והבינה תחפש ותענה."),
    ("tools_planning", "כלים: תכנון וחישובים", "בחרת בתכנון וחישובים. אמור מה תרצה לתכנן או לחשב."),
    ("utilities_menu", "תפריט כלים שימושיים", "תפריט כלים שימושיים. לבדיקת מזג אוויר הקש אחת. לחישוב הקש שתיים. להמרת יחידות ומטבעות הקש שלוש. לתרגום הקש ארבע. לחיפוש מהיר הקש חמש. לחזרה לתפריט הראשי הקש אפס. לכוכבית לחזרה לתפריט הקודם ולסולמית לתפריט הראשי."),
    ("util_weather", "כלים שימושיים: מזג אוויר", "בחרת בבדיקת מזג אוויר. אמור באיזו עיר לבדוק, או שאל שאלה על מזג האוויר."),
    ("util_calc", "כלים שימושיים: מחשבון", "בחרת במחשבון. אמור את התרגיל או החישוב שברצונך לבצע."),
    ("util_convert", "כלים שימושיים: המרות", "בחרת בהמרת יחידות ומטבעות. אמור מה להמיר, מאיזו יחידה או מטבע ולאיזו."),
    ("util_translate", "כלים שימושיים: תרגום", "בחרת בתרגום. אמור את הטקסט ואת שפת היעד."),
    ("util_search", "כלים שימושיים: חיפוש מהיר", "בחרת בחיפוש מהיר. אמור מה תרצה לחפש ולבדוק."),
    ("wiki_intro", "ויקיפדיה: פתיחה", "ברוכים הבאים לחיפוש בוויקיפדיה. אמור את שם הערך או את הנושא שתרצה לשמוע עליו."),
    ("wiki_notfound", "ויקיפדיה: לא נמצא", "לא מצאתי ערך מתאים. נסה לומר את השם בצורה אחרת, או נושא קרוב."),
    ("wiki_more", "ויקיפדיה: המשך", "אפשר לשאול שאלה נוספת על הערך, או לומר ערך חדש לחיפוש. לחזרה לתפריט הראשי אמור: חזרה לתפריט."),
    ("personal_menu", "אזור אישי", "האזור האישי. לשמיעת פרטי הפרופיל שלך הקש אחת. להחלפת הקול שלך הקש שתיים. לעדכון השם שלך הקש שלוש. להפעלה או כיבוי של שמירת היסטוריית השיחות הקש ארבע. לשמיעת השיחה האחרונה שלך הקש חמש. לחזרה לתפריט הראשי הקש אפס. לכוכבית לחזרה לתפריט הקודם ולסולמית לתפריט הראשי."),
    ("register_name", "הרשמה: אמירת שם", "זו הפעם הראשונה שאתה מתקשר. אמור את שמך הפרטי ובסיום הקש סולמית."),
    ("register_done", "הרשמה: אישור", "תודה, שמך נשמר. מעכשיו אקרא לך בשמך."),
    ("admin_code", "ניהול: בקשת קוד", "לכניסה לניהול המערכת הקש את קוד המנהל ובסיום הקש סולמית."),
    ("admin_wrong", "ניהול: קוד שגוי", "הקוד שהוקש שגוי. אנא נסה שוב."),
    ("admin_locked", "ניהול: נעילה", "בגלל ניסיונות שגויים רבים הכניסה לניהול נעולה לזמן מה. נסה שוב מאוחר יותר."),
    ("forum_menu", "פורום קולי", "הפורום הקולי בעיוות קול. להאזנה להודעות האחרונות הקש אחת. לפרסום הודעה קולית חדשה הקש שתיים. לחזרה לתפריט הראשי הקש אפס. לכוכבית לחזרה לתפריט הקודם ולסולמית לתפריט הראשי."),
    ("please_wait", "נא להמתין", "נא להמתין, אני מכין תשובה."),
    ("error_try_again", "שגיאה כללית", "אירעה תקלה זמנית. אנא נסה שוב בעוד רגע."),
    ("no_permission", "אין הרשאה", "אין לך הרשאה להשתמש בשלוחה זו."),
    ("goodbye", "סיום שיחה", "תודה שהתקשרת. להתראות."),
]

assert len(MESSAGES) == 40, len(MESSAGES)

VOICE = "Charon"  # קול גברי, מסביר וברור
STYLE = "דבר בעברית בקול גברי רגוע, ברור ונעים, בקצב איטי מעט, כמו הודעה במערכת טלפונית."
TARGET_RATE = 8000  # הפורמט המקורי של ימות המשיח

_state = {"running": False, "done": 0, "failed": [], "started": 0.0, "finished": 0.0, "dir": ""}
_lock = threading.Lock()


def file_name(index, key):
    return "%03d" % (index + 1)  # ימות: שם קובץ מספרי, למשל 001.wav


def to_yemot_wav(wav_bytes):
    """ממיר WAV כלשהו ל-8kHz מונו 16 ביט, בלי תלות בספריות חיצוניות."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        ch, width, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        frames = w.readframes(n)
    if width != 2:
        raise ValueError("רק שמע 16 ביט נתמך")
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if ch == 2:
        samples = array.array("h", [(samples[i] + samples[i + 1]) // 2 for i in range(0, len(samples) - 1, 2)])
    if rate != TARGET_RATE:
        ratio = rate / TARGET_RATE
        out_len = int(len(samples) / ratio)
        out = array.array("h", [0]) * out_len
        if rate % TARGET_RATE == 0:
            k = rate // TARGET_RATE
            for i in range(out_len):
                seg = samples[i * k:(i + 1) * k]
                out[i] = sum(seg) // len(seg)
        else:
            for i in range(out_len):
                pos = i * ratio
                j = int(pos)
                frac = pos - j
                nxt = samples[j + 1] if j + 1 < len(samples) else samples[j]
                out[i] = int(samples[j] * (1 - frac) + nxt * frac)
        samples = out
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        data = array.array("h", samples)
        if sys.byteorder == "big":
            data.byteswap()
        w.writeframes(data.tobytes())
    return buf.getvalue()


def _worker(tts, voice, only, on_complete=None):
    outdir = _state["dir"]
    try:
        for i, (key, label, text) in enumerate(MESSAGES):
            if only and key not in only:
                continue
            path = os.path.join(outdir, file_name(i, key) + ".wav")
            if os.path.exists(path):  # לא מייצרים שוב מה שכבר נוצר
                with _lock:
                    _state["done"] += 1
                continue
            ok = False
            for attempt in range(4):
                try:
                    wav = to_yemot_wav(tts(text, voice))
                    with open(path, "wb") as f:
                        f.write(wav)
                    ok = True
                    break
                except Exception as e:  # noqa: BLE001
                    print("voicepack", key, "attempt", attempt + 1, "failed:", str(e)[:160], flush=True)
                    time.sleep(8 * (attempt + 1))
            with _lock:
                if ok:
                    _state["done"] += 1
                else:
                    _state["failed"].append(key)
            time.sleep(3)  # מרווח בין בקשות, כדי לא לחצות את מכסת ה-TTS
    finally:
        with _lock:
            _state["running"] = False
            _state["finished"] = time.time()
        if on_complete:
            try:
                on_complete(build_zip(), status())
            except Exception as e:  # noqa: BLE001
                print("voicepack archive persistence failed:", str(e)[:160], flush=True)


def start(tts, voice=VOICE, only=None, on_complete=None):
    with _lock:
        if _state["running"]:
            return False
        if not _state["dir"] or not os.path.isdir(_state["dir"]):
            _state["dir"] = tempfile.mkdtemp(prefix="voicepack_")
        _state.update(running=True, done=0, failed=[], started=time.time(), finished=0.0)
    threading.Thread(target=_worker, args=(tts, voice, set(only or []), on_complete), daemon=True).start()
    return True


def status():
    with _lock:
        existing = 0
        if _state["dir"] and os.path.isdir(_state["dir"]):
            existing = len([f for f in os.listdir(_state["dir"]) if f.endswith(".wav")])
        return {"running": _state["running"], "created": existing, "total": len(MESSAGES),
                "failed": list(_state["failed"]), "voice": VOICE}


def build_zip():
    """ZIP של הקבצים שנוצרו + index.csv שמסביר איזה קובץ הוא איזו הודעה."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        rows = [["file", "key", "purpose", "text"]]
        for i, (key, label, text) in enumerate(MESSAGES):
            name = file_name(i, key) + ".wav"
            path = os.path.join(_state["dir"] or "", name)
            if _state["dir"] and os.path.exists(path):
                z.write(path, name)
            rows.append([name, key, label, text])
        sio = io.StringIO()
        csv.writer(sio).writerows(rows)
        z.writestr("index.csv", "﻿" + sio.getvalue())
        z.writestr("README.txt",
                   "40 הודעות בעברית בקול גברי (WAV 8kHz מונו 16 ביט). "
                   "יש להעלות לתיקיית השלוחה בימות המשיח; הקובץ index.csv מפרט איזו הודעה בכל קובץ.\n")
    return buf.getvalue()


PAGE = """<!doctype html><html lang="he" dir="rtl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>הקראות קוליות</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:2rem auto;padding:0 16px;line-height:1.6}
button,a.b{display:inline-block;padding:.6rem 1.1rem;margin:.3rem .3rem .3rem 0;border:1px solid #444;border-radius:8px;background:#fff;cursor:pointer;text-decoration:none;color:#000;font-size:1rem}
#s{margin:1rem 0;padding:.8rem;background:#f3f3f3;border-radius:8px}</style></head><body>
<h1>40 הקראות קוליות</h1><p>קול גברי בעברית (Charon). קובצי WAV מותאמים לימות: 8kHz, מונו, 16 ביט. היצירה רצה ברקע והחבילה נשמרת להורדה גם לאחר הפעלה מחדש.</p>
<button id="go">התחל יצירה</button> <a class="b" id="zip" href="/admin/api/voicepack/zip">הורד ZIP</a>
<div id="s">טוען...</div>
<script>
const H={"X-Requested-With":"admin","Content-Type":"application/json"};
async function st(){try{const r=await fetch("/admin/api/voicepack/status");const j=await r.json();
document.getElementById("s").textContent=j.error?j.error:(j.running?"יוצר... ":"")+j.created+" מתוך "+j.total+" קבצים"+(j.failed&&j.failed.length?" | נכשלו: "+j.failed.join(", "):"");}catch(e){document.getElementById("s").textContent="שגיאת תקשורת"}}
document.getElementById("go").onclick=async()=>{const r=await fetch("/admin/api/voicepack/start",{method:"POST",headers:H,body:"{}"});const j=await r.json();document.getElementById("s").textContent=j.started?"היצירה התחילה ברקע...": "היצירה כבר פועלת. אפשר לעקוב אחר ההתקדמות כאן.";st()};
st();setInterval(st,5000);
</script></body></html>"""


def register(app, admin_required, tts, flask, on_complete=None, load_archive=None):
    """מחבר את הנקודות לשרת. admin_required = אותו מעטפת הרשאה של ממשק הניהול."""
    Response, jsonify = flask.Response, flask.jsonify

    @app.route("/admin/voicepack")
    def voicepack_page():
        if not flask.session.get("admin"):
            return Response("נדרשת כניסה לממשק הניהול", status=401, mimetype="text/plain; charset=utf-8")
        return Response(PAGE, mimetype="text/html; charset=utf-8")

    @app.route("/admin/api/voicepack/start", methods=["POST"])
    @admin_required
    def voicepack_start():
        return jsonify({"started": start(tts, on_complete=on_complete)})

    @app.route("/admin/api/voicepack/status")
    @admin_required
    def voicepack_status():
        current = status()
        if not current["running"] and current["created"] == 0 and load_archive:
            saved = load_archive()
            if saved and saved.get("status"):
                current.update(saved["status"])
                current["persisted"] = True
        return jsonify(current)

    @app.route("/admin/api/voicepack/zip")
    @admin_required
    def voicepack_zip():
        data = build_zip()
        if status()["created"] == 0 and load_archive:
            saved = load_archive()
            if saved and saved.get("archive"):
                data = saved["archive"]
        return Response(data, mimetype="application/zip",
                        headers={"Content-Disposition": "attachment; filename=yemot-voicepack.zip"})
