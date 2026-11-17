"""Free, non-commercial route lookup using Transitous (MOTIS) and OpenStreetMap.

Transitous is best-effort and permits only open-source, non-commercial, light use.
The application sends a descriptive User-Agent and limits plan calls to 600 per rolling
24 hours. Geocoding uses the public Nominatim service with caching and <=1 request/sec.
"""
import os
import time
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import urlencode

import requests

TRANSITOUS_PLAN_URL = "https://api.transitous.org/api/v6/plan"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
APP_CONTACT = "https://yemot-smart-search.onrender.com"
APP_USER_AGENT = "YemotSmartSearch/1.0 (" + APP_CONTACT + ")"
TRANSITOUS_DAILY_LIMIT = 600  # below Transitous' 720 requests / 24h coordination threshold
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

_lock = threading.Lock()
_usage_times = deque()
_geocode_cache = {}
_route_cache = {}
_nominatim_lock = threading.Lock()
_last_nominatim_request = 0.0


def _reserve_plan_request():
    """Reserve one request under a strict rolling 24-hour limit; fail closed on DB errors."""
    global _usage_times
    if DATABASE_URL:
        try:
            import psycopg
            with psycopg.connect(DATABASE_URL, connect_timeout=4) as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS transitous_api_usage "
                    "(requested_at TIMESTAMPTZ NOT NULL)"
                )
                conn.execute("SELECT pg_advisory_xact_lock(72519042)")
                conn.execute(
                    "DELETE FROM transitous_api_usage "
                    "WHERE requested_at < NOW() - INTERVAL '24 hours'"
                )
                count = conn.execute(
                    "SELECT COUNT(*) FROM transitous_api_usage"
                ).fetchone()[0]
                if count >= TRANSITOUS_DAILY_LIMIT:
                    return False
                conn.execute(
                    "INSERT INTO transitous_api_usage (requested_at) VALUES (NOW())"
                )
                return True
        except Exception:
            # Never bypass the cap if persistent accounting was configured but failed.
            return False
    now = time.time()
    with _lock:
        while _usage_times and _usage_times[0] <= now - 86400:
            _usage_times.popleft()
        if len(_usage_times) >= TRANSITOUS_DAILY_LIMIT:
            return False
        _usage_times.append(now)
        return True


def _geocode(place):
    global _last_nominatim_request
    key = " ".join(str(place or "").strip().split()).casefold()
    if not key:
        return None
    now = time.time()
    with _lock:
        cached = _geocode_cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
    # Public Nominatim policy: no more than one request per second.
    with _nominatim_lock:
        wait = 1.05 - (time.monotonic() - _last_nominatim_request)
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            NOMINATIM_URL,
            params={"q": place, "format": "jsonv2", "limit": 1, "addressdetails": 0},
            headers={"User-Agent": APP_USER_AGENT, "Accept-Language": "he,en"},
            timeout=15,
        )
        _last_nominatim_request = time.monotonic()
        response.raise_for_status()
        results = response.json()
    if not results:
        return None
    result = {
        "lat": float(results[0]["lat"]),
        "lon": float(results[0]["lon"]),
        "label": str(results[0].get("display_name") or place),
    }
    with _lock:
        _geocode_cache[key] = (now + 7 * 86400, result)
    return result


def _iso(value):
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
    except Exception:
        return ""


def _distance(value):
    try:
        metres = float(value)
    except (TypeError, ValueError):
        return ""
    return (f"{metres / 1000:.1f} קילומטרים" if metres >= 1000
            else f"{int(round(metres))} מטרים")


def _duration(seconds):
    try:
        minutes = max(1, round(float(seconds) / 60))
    except (TypeError, ValueError):
        return "משך הזמן לא זמין"
    hours, mins = divmod(minutes, 60)
    return (f"{hours} שעות ו-{mins} דקות" if hours
            else f"{mins} דקות")


def _leg_instruction(leg):
    mode = str(leg.get("mode") or "").upper()
    origin = (leg.get("from") or {}).get("name") or "נקודת המוצא"
    destination = (leg.get("to") or {}).get("name") or "נקודת היעד"
    start = _iso(leg.get("startTime"))
    end = _iso(leg.get("endTime"))
    duration = _duration(leg.get("duration"))
    if mode in ("WALK", "FOOT"):
        steps = leg.get("steps") or []
        details = []
        turns = {
            "LEFT": "פנה שמאלה", "RIGHT": "פנה ימינה",
            "CONTINUE": "המשך ישר", "SLIGHTLY_LEFT": "פנה מעט שמאלה",
            "SLIGHTLY_RIGHT": "פנה מעט ימינה", "UTURN": "בצע פניית פרסה",
            "DEPART": "התחל ללכת", "CIRCLE_CLOCKWISE": "היכנס לכיכר",
            "CIRCLE_COUNTERCLOCKWISE": "היכנס לכיכר",
        }
        for step in steps[:10]:
            direction = turns.get(str(step.get("relativeDirection") or "").upper(), "")
            street = str(step.get("streetName") or "").strip()
            dist = _distance(step.get("distance"))
            part = " ".join(x for x in (direction, ("ברחוב " + street) if street else "", dist) if x)
            if part:
                details.append(part)
        walk_text = ("; ".join(details) if details else f"הליכה מנקודת {origin} עד {destination}")
        return f"הליכה ({duration})" + (f": {walk_text}" if walk_text else "")
    route = (leg.get("routeShortName") or leg.get("displayName") or
             leg.get("routeLongName") or "תחבורה ציבורית")
    headsign = str(leg.get("headsign") or "").strip()
    stops = leg.get("intermediateStops") or []
    if headsign:
        headsign = f", לכיוון {headsign}"
    timing = f", יציאה {start}" if start else ""
    if end:
        timing += f", הגעה {end}"
    return (
        f"עלה על {route} בתחנת {origin}{headsign}{timing}; "
        f"רד בתחנת {destination} ({duration})"
    )


def compute_transitous_route(map_request):
    """Return only route details supplied by Transitous; never invent missing directions."""
    origin = str(map_request.get("origin") or "").strip()[:300]
    destination = str(map_request.get("destination") or "").strip()[:300]
    mode = str(map_request.get("travel_mode") or "").strip().upper()
    time_kind = str(map_request.get("time_kind") or "").strip().lower()
    if not origin or not destination or mode not in ("TRANSIT", "WALK") or time_kind not in ("departure", "arrival"):
        return "חסרים פרטי מסלול. אמור במשפט אחד מוצא, יעד, מועד יציאה או הגעה, והאם בתחבורה ציבורית או בהליכה."
    try:
        requested = datetime.fromisoformat(str(map_request.get("time_value") or "").replace("Z", "+00:00"))
        if requested.tzinfo is None:
            from zoneinfo import ZoneInfo
            requested = requested.replace(tzinfo=ZoneInfo("Asia/Jerusalem"))
        requested = requested.astimezone(timezone.utc)
    except Exception:
        return "לא הצלחתי להבין את התאריך והשעה. אמור תאריך ושעה ברורים ליציאה או להגעה."
    cache_key = (origin.casefold(), destination.casefold(), mode, time_kind, requested.isoformat())
    now = time.time()
    with _lock:
        cached = _route_cache.get(cache_key)
        if cached and cached[0] > now:
            return cached[1]
    try:
        start = _geocode(origin)
        end = _geocode(destination)
        if not start or not end:
            return "לא הצלחתי לזהות את המוצא או היעד במפה. נסה לציין כתובת מלאה או שם מקום מדויק."
        if not _reserve_plan_request():
            return "מכסת החיפושים היומית של שירות המסלולים נוצלה או ששירות הרישום אינו זמין. נסה שוב מאוחר יותר."
        params = {
            "fromPlace": f"{start['lat']:.6f},{start['lon']:.6f}",
            "toPlace": f"{end['lat']:.6f},{end['lon']:.6f}",
            "time": requested.isoformat(timespec="seconds"),
            "arriveBy": "true" if time_kind == "arrival" else "false",
            "timetableView": "false",
            "detailedLegs": "true",
            "detailedTransfers": "true",
            "language": "he",
            "timeout": "20",
        }
        if mode == "WALK":
            params["transitModes"] = ""
            params["directModes"] = "WALK"
        else:
            params["transitModes"] = "TRANSIT"
            params["directModes"] = ""
        response = requests.get(
            TRANSITOUS_PLAN_URL,
            params=params,
            headers={"User-Agent": APP_USER_AGENT, "Accept": "application/json"},
            timeout=28,
        )
        response.raise_for_status()
        payload = response.json()
        journeys = payload.get("direct" if mode == "WALK" else "itineraries") or []
        if not journeys and mode == "WALK":
            journeys = payload.get("itineraries") or []
        if not journeys:
            return "לא נמצא מסלול מתאים לפי הנתונים הזמינים כרגע. בדוק את הכתובות או נסה מועד אחר."
        journey = journeys[0]
        legs = journey.get("legs") or []
        if not legs:
            return "שירות המסלולים לא החזיר פירוט של שלבי הדרך, ולכן לא אציג הוראות לא מאומתות."
        instructions = [_leg_instruction(leg) for leg in legs]
        instructions = [x for x in instructions if x]
        if not instructions:
            return "שירות המסלולים לא החזיר הוראות מפורטות למסלול."
        departure = _iso(journey.get("startTime")) or requested.astimezone(ZoneInfo("Asia/Jerusalem")).strftime("%H:%M")
        arrival = _iso(journey.get("endTime"))
        total = _duration(journey.get("duration"))
        mode_label = "תחבורה ציבורית" if mode == "TRANSIT" else "הליכה ברגל"
        if time_kind == "arrival":
            time_line = f"המסלול מיועד להגעה עד {arrival or requested.astimezone().strftime('%H:%M')}."
        else:
            time_line = f"שעת היציאה המתוכננת היא {departure}."
        numbered = " ".join(f"שלב {i + 1}: {item}." for i, item in enumerate(instructions[:14]))
        result = (
            f"מצאתי מסלול מ{origin} אל {destination}, באמצעות {mode_label}. "
            f"{time_line} משך הנסיעה המשוער: {total}. "
            f"ההדרכה לפי הסדר: {numbered} "
            "הנתונים באדיבות Transitous ו-OpenStreetMap."
        )
        with _lock:
            _route_cache[cache_key] = (now + 300, result)
        return result
    except requests.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", 0)
        print("Transitous route request failed:", status, flush=True)
        if status in (400, 404):
            return "לא הצלחתי לחשב את המסלול. בדוק את שמות המקומות והתאריך והשעה."
        if status == 429:
            return "שירות המסלולים עמוס כרגע. המתן מעט ונסה שוב."
        return "שירות המסלולים אינו זמין כרגע. נסה שוב מאוחר יותר."
    except Exception as exc:
        print("Transitous route calculation failed:", str(exc)[:180], flush=True)
        return "לא הצלחתי לחשב את המסלול כרגע. נסה שוב מאוחר יותר."
