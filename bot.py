"""
Независимый Портал: ежедневная лента про Бразилию и Санта-Катарину для Threads.

Каждое утро бот присылает автору в личку:
  1. прогноз погоды на день для Флорианополиса и готовый Threads-пост про погоду;
  2. подборку новостей: сначала Санта-Катарина, потом вся Бразилия.
     К каждой новости: карточка для чтения (со ссылкой) и готовый Threads-пост.

Переменные окружения:
  BOT_TOKEN, ADMIN_CHAT_ID, ANTHROPIC_API_KEY   обязательные
  GITHUB_TOKEN                                  память о том, что уже отправлено
  GITHUB_REPO                                   необязательная, по умолчанию DenisAloviagin/nezavisimiy-portal
"""
import os
import re
import json
import html
import base64
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import feedparser
import httpx
from telegram import Bot

try:
    from telegram import LinkPreviewOptions
except ImportError:  # старая версия python-telegram-bot
    LinkPreviewOptions = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "DenisAloviagin/nezavisimiy-portal")
STATE_BRANCH = "state"
STATE_PATH = "seen.json"

# ─── Настройки ────────────────────────────────────────────────────────────────

CITY_NAME = "Флорианополис"
CITY_SHORT = "Флорипа"
CITY_LAT, CITY_LON = -27.5954, -48.5480
SEA_LAT, SEA_LON = -27.60, -48.35  # точка в море у восточного берега острова

FRESH_DAYS = 3          # свежее: идёт как есть
ARCHIVE_DAYS = 7        # от FRESH_DAYS до ARCHIVE_DAYS: помечается [АРХИВ], старше: не берём
MEMORY_DAYS = 14        # сколько дней помним отправленные ссылки
TITLE_MEMORY_DAYS = 7   # заголовки за сколько дней показываем Claude как "уже было"
SIMILARITY_LIMIT = 0.4

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

RSS_FEEDS = [
    "https://g1.globo.com/rss/g1/sc/santa-catarina/",
    "https://g1.globo.com/rss/g1/",
    "https://g1.globo.com/rss/g1/pop-arte/",
    "https://agenciabrasil.ebc.com.br/feed/ultimasnoticias/feed.xml",
    "https://ndmais.com.br/feed/",
]

SEARCH_QUERIES = [
    # Санта-Катарина и Флорианополис
    "Florianópolis notícias hoje",
    "Santa Catarina notícias hoje",
    "Norte da Ilha Florianópolis notícias Canasvieiras Jurerê Ingleses",
    "Florianópolis praias balneabilidade imprópria banho",
    "Florianópolis Defesa Civil alerta chuva temporal",
    "Florianópolis trânsito obras ponte Hercílio Luz",
    "Florianópolis temporada verão turistas argentinos",
    "Santa Catarina polícia operação Florianópolis",
    "Florianópolis notícia curiosa",
    "NSC Total Florianópolis cotidiano",
    "ND Mais Florianópolis",
    # Странное и вирусное
    "Brasil notícia curiosa bizarra hoje",
    "Brasil achado inusitado descoberta",
    "Brasil animal selvagem notícia hoje",
    "Brazil weird news viral",
    # Быт и правила для иностранцев
    "Brasil nova regra estrangeiros visto",
    "Brasil lei nova entra em vigor",
    "Brasil custo de vida preços supermercado",
    "Brazil expats news",
    # Культура, еда, спорт, природа
    "Brasil cultura festa tradição notícia",
    "Brasil gastronomia comida típica notícia",
    "Brasil futebol notícia polêmica",
    "Brasil carnaval novidades",
    "Amazônia notícia descoberta",
    # Резонанс и вещества (только Бразилия)
    "Brasil crime caso repercussão nacional",
    "Brasil ayahuasca regulamentação notícia",
    "Brasil cannabis STF Anvisa notícia",
]

CATEGORY_ICONS = {
    "странное": "🤯",
    "санта-катарина": "🏝",
    "быт": "🛒",
    "закон": "⚖️",
    "культура": "🎭",
    "еда": "🍽",
    "спорт": "⚽️",
    "природа": "🌴",
    "криминал": "🚔",
    "резонанс": "🔥",
    "психоделики": "💊",
    "каннабис": "🌿",
}

# Допустимые регионы: всё остальное отсеивается кодом, даже если Claude его прислал
ALLOWED_REGION_PARTS = [
    "бразил", "санта-катарин", "флорианополис", "сан-паулу", "рио",
    "амазон", "северо-восток",
]
SC_REGION_PARTS = ["санта-катарин", "флорианополис"]

DATE_PATTERNS = [
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b",
    r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b",
    r"\b\d{1,2}\s+de\s+(?:janeiro|fevereiro|março|abril|maio|junho|julho|agosto|setembro|outubro|novembro|dezembro)(?:\s+de\s+\d{4})?\b",
    r"\b\d{1,2}/\d{1,2}/\d{4}\b",
]

OPINION_MARKERS = [
    "oped", "op-ed", "opinion", "opiniao", "opinião", "tribune", "commentary",
    "column", "coluna", "colunista", "editorial", "perspective",
]

WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]

WMO = {
    0: ("ясно", "☀️"), 1: ("преимущественно ясно", "🌤"), 2: ("переменная облачность", "⛅️"),
    3: ("пасмурно", "☁️"), 45: ("туман", "🌫"), 48: ("туман", "🌫"),
    51: ("морось", "🌦"), 53: ("морось", "🌦"), 55: ("сильная морось", "🌦"),
    56: ("ледяная морось", "🌧"), 57: ("ледяная морось", "🌧"),
    61: ("небольшой дождь", "🌦"), 63: ("дождь", "🌧"), 65: ("сильный дождь", "🌧"),
    66: ("ледяной дождь", "🌧"), 67: ("ледяной дождь", "🌧"),
    80: ("кратковременные дожди", "🌦"), 81: ("ливни", "🌧"), 82: ("сильные ливни", "⛈"),
    95: ("гроза", "⛈"), 96: ("гроза с градом", "⛈"), 99: ("сильная гроза с градом", "⛈"),
}


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def dash(text: str) -> str:
    return (text or "").replace("—", "-").replace("–", "-")


def esc(text: str) -> str:
    """Безопасный для Telegram HTML текст без длинных тире."""
    return html.escape(dash(text), quote=False)


def canonical_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        params = parse_qs(parsed.query)
        keep = {k: v for k, v in params.items() if not k.startswith("utm_")}
        return urlunparse(parsed._replace(query=urlencode(keep, doseq=True), fragment=""))
    except Exception:
        return url


def item_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]


def stems(text: str) -> set:
    words = re.findall(r"[a-zа-яё0-9]+", (text or "").lower())
    return {w[:5] for w in words if len(w) >= 4 or w.isdigit()}


def is_similar(a: str, b: str) -> bool:
    """Похожи ли два заголовка: ловит одну историю из разных источников."""
    sa, sb = stems(a), stems(b)
    if len(sa) < 3 or len(sb) < 3:
        return False
    inter = len(sa & sb)
    return inter >= 3 and inter / len(sa | sb) >= SIMILARITY_LIMIT


def extract_published_date(entry) -> datetime | None:
    """Дата публикации, не обновления. Если даты нет, возвращаем None, а не 'сейчас'."""
    for field in ("published_parsed", "pubdate_parsed"):
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    return None


def extract_event_dates(text: str) -> list[str]:
    found = []
    snippet = (text or "")[:1500]
    for pattern in DATE_PATTERNS:
        for m in re.finditer(pattern, snippet, re.IGNORECASE):
            if m.group(0) not in found:
                found.append(m.group(0))
    return found[:3]


def detect_content_type(url: str, tags: list) -> str:
    haystack = (url or "").lower() + " " + " ".join(str(t).lower() for t in tags)
    return "КОЛОНКА" if any(m in haystack for m in OPINION_MARKERS) else "НОВОСТЬ"


def human_error(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def ru_date(d) -> str:
    return f"{WEEKDAYS[d.weekday()].capitalize()}, {d.day} {MONTHS[d.month - 1]}"


def preview_kwargs(preview: bool) -> dict:
    if LinkPreviewOptions is not None:
        return {"link_preview_options": LinkPreviewOptions(is_disabled=not preview)}
    return {"disable_web_page_preview": not preview}


async def send_html(bot: Bot, text: str, preview: bool = False):
    """Отправка в HTML; если Telegram не принял разметку, шлём простым текстом."""
    try:
        return await bot.send_message(
            chat_id=ADMIN_CHAT_ID, text=text[:4000], parse_mode="HTML",
            **preview_kwargs(preview),
        )
    except Exception as e:
        logger.warning(f"HTML send failed: {e}")
        plain = html.unescape(re.sub(r"<[^>]+>", "", text))
        return await bot.send_message(chat_id=ADMIN_CHAT_ID, text=plain[:4000])


def threads_message(text: str, label: str = "✍️ Threads") -> str:
    n = len(text)
    warn = " ⚠️ длиннее 500" if n > 500 else ""
    return f"{label} (≈{n}/500){warn}\n<pre>{esc(text)}</pre>"


# ─── Память: что уже отправляли (ветка state в GitHub) ────────────────────────

GH_API = "https://api.github.com"


def gh_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def gh_load_state(client: httpx.AsyncClient) -> tuple[list, str | None]:
    r = await client.get(
        f"{GH_API}/repos/{GITHUB_REPO}/contents/{STATE_PATH}",
        params={"ref": STATE_BRANCH}, headers=gh_headers(),
    )
    if r.status_code == 404:
        return [], None  # файла ещё нет: первый запуск
    r.raise_for_status()
    data = r.json()
    raw = base64.b64decode(data["content"]).decode("utf-8")
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    items = parsed.get("items", []) if isinstance(parsed, dict) else []
    return items, data["sha"]


async def gh_save_state(client: httpx.AsyncClient, items: list, sha: str | None):
    headers = gh_headers()
    base = f"{GH_API}/repos/{GITHUB_REPO}"

    ref = await client.get(f"{base}/git/ref/heads/{STATE_BRANCH}", headers=headers)
    if ref.status_code == 404:
        repo = await client.get(base, headers=headers)
        repo.raise_for_status()
        default_branch = repo.json().get("default_branch", "main")
        head = await client.get(f"{base}/git/ref/heads/{default_branch}", headers=headers)
        head.raise_for_status()
        created = await client.post(
            f"{base}/git/refs", headers=headers,
            json={"ref": f"refs/heads/{STATE_BRANCH}", "sha": head.json()["object"]["sha"]},
        )
        created.raise_for_status()
    else:
        ref.raise_for_status()

    content = base64.b64encode(
        json.dumps({"items": items}, ensure_ascii=False).encode("utf-8")
    ).decode()
    body = {"message": "state: seen items", "content": content, "branch": STATE_BRANCH}
    if sha:
        body["sha"] = sha
    r = await client.put(f"{base}/contents/{STATE_PATH}", headers=headers, json=body)
    if r.status_code in (409, 422):  # файл изменился, берём свежий sha и пробуем ещё раз
        _, fresh_sha = await gh_load_state(client)
        if fresh_sha:
            body["sha"] = fresh_sha
        r = await client.put(f"{base}/contents/{STATE_PATH}", headers=headers, json=body)
    r.raise_for_status()


def state_hint(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code in (401, 403):
            return "HTTP %s: проверь GITHUB_TOKEN и право Contents: Read and write" % code
        if code == 404:
            return f"HTTP 404: проверь GITHUB_REPO ({GITHUB_REPO}) и доступ токена к репозиторию"
        return f"HTTP {code}"
    return type(e).__name__


def prune_items(items: list, now: datetime) -> list:
    keep = []
    for it in items:
        try:
            ts = datetime.fromisoformat(it["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now - ts <= timedelta(days=MEMORY_DAYS) and it.get("id"):
            keep.append(it)
    return keep


def recent_titles(items: list, now: datetime, limit: int = 60) -> list[str]:
    out = []
    for it in sorted(items, key=lambda x: x.get("ts", ""), reverse=True):
        try:
            ts = datetime.fromisoformat(it["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if now - ts <= timedelta(days=TITLE_MEMORY_DAYS) and it.get("title"):
            out.append(it["title"])
    return out[:limit]


# ─── Погода (Open-Meteo, бесплатно, без ключа) ───────────────────────────────

async def fetch_weather(client: httpx.AsyncClient):
    r = await client.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": CITY_LAT, "longitude": CITY_LON,
            "timezone": "America/Sao_Paulo", "forecast_days": 1,
            "daily": ("weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,"
                      "precipitation_probability_max,wind_speed_10m_max,wind_gusts_10m_max,"
                      "uv_index_max,sunrise,sunset"),
            "hourly": "temperature_2m,precipitation_probability,weather_code",
        },
    )
    r.raise_for_status()
    data = r.json()
    marine = None
    try:  # море необязательно: если не ответило, просто пропускаем строку
        m = await client.get(
            "https://marine-api.open-meteo.com/v1/marine",
            params={
                "latitude": SEA_LAT, "longitude": SEA_LON,
                "timezone": "America/Sao_Paulo", "forecast_days": 1,
                "hourly": "sea_surface_temperature", "daily": "wave_height_max",
            },
        )
        m.raise_for_status()
        marine = m.json()
    except Exception as e:
        logger.warning(f"Marine API: {e}")
    return data, marine


def _col(h: dict, key: str, hours) -> list:
    arr = h.get(key) or []
    return [arr[i] for i in hours if i < len(arr) and arr[i] is not None]


def build_weather(data: dict, marine: dict | None) -> dict:
    d = data["daily"]
    h = data.get("hourly", {}) or {}

    def first(key):
        arr = d.get(key) or []
        return arr[0] if arr else None

    W = {
        "date": first("time"),
        "code": first("weather_code"),
        "tmax": first("temperature_2m_max"),
        "tmin": first("temperature_2m_min"),
        "rain_mm": first("precipitation_sum"),
        "rain_prob": first("precipitation_probability_max"),
        "wind": first("wind_speed_10m_max"),
        "gust": first("wind_gusts_10m_max"),
        "uv": first("uv_index_max"),
        "sunrise": (first("sunrise") or "")[-5:],
        "sunset": (first("sunset") or "")[-5:],
    }
    if W["tmax"] is None or W["tmin"] is None:
        raise ValueError("в ответе погоды нет температуры")

    temps_m = _col(h, "temperature_2m", range(6, 12))
    temps_d = _col(h, "temperature_2m", range(12, 18))
    temps_e = _col(h, "temperature_2m", range(18, 24))
    W["t_morning"] = round(sum(temps_m) / len(temps_m)) if temps_m else None
    W["t_day"] = round(max(temps_d)) if temps_d else round(W["tmax"])
    W["t_evening"] = round(sum(temps_e) / len(temps_e)) if temps_e else None

    probs = h.get("precipitation_probability") or []
    hours = [i for i, p in enumerate(probs) if p is not None and p >= 50]
    window = None
    if hours:
        runs, start, prev = [], hours[0], hours[0]
        for x in hours[1:]:
            if x == prev + 1:
                prev = x
                continue
            runs.append((start, prev))
            start = prev = x
        runs.append((start, prev))
        s, e = max(runs, key=lambda r: r[1] - r[0])
        window = (s, e + 1)
    W["rain_window"] = window
    W["rain_all_day"] = bool(window and window[1] - window[0] >= 18)

    sea = wave = None
    if marine:
        try:
            ssts = [x for x in marine["hourly"]["sea_surface_temperature"] if x is not None]
            sea = sum(ssts) / len(ssts) if ssts else None
            waves = marine["daily"]["wave_height_max"]
            wave = waves[0] if waves else None
        except (KeyError, TypeError):
            pass
    W["sea"], W["wave"] = sea, wave
    return W


def beach_verdict(W: dict) -> str:
    prob = W["rain_prob"] or 0
    mm = W["rain_mm"] or 0
    gust = W["gust"] or 0
    if prob >= 60 or mm >= 5:
        return "Пляж: не сегодня"
    if W["tmax"] >= 25 and gust < 45 and prob < 30:
        return "Пляж: да"
    if W["tmax"] >= 22 and prob < 50:
        return "Пляж: можно, если без сильного ветра"
    return "Пляж: прохладно"


def _r1(v) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".")


def weather_card(W: dict, today) -> str:
    desc, emoji = WMO.get(W["code"], ("", "🌤"))
    tmax, tmin = round(W["tmax"]), round(W["tmin"])
    prob = W["rain_prob"]
    lines = [f"🌤 <b>Погода, {esc(CITY_NAME)}</b>", esc(ru_date(today)), ""]
    head = f"{emoji} {desc.capitalize()}. " if desc else ""
    lines.append(f"{head}Днём до {tmax}°, ночью {tmin}°.")

    parts = []
    if W["t_morning"] is not None:
        parts.append(f"утром {W['t_morning']}°")
    parts.append(f"днём до {W['t_day']}°")
    if W["t_evening"] is not None:
        parts.append(f"вечером {W['t_evening']}°")
    lines.append(", ".join(parts).capitalize())

    if prob is not None and prob >= 20:
        rain = f"🌧 Дождь: вероятность до {round(prob)}%"
        if W["rain_mm"]:
            rain += f", около {_r1(W['rain_mm'])} мм"
        if W.get("rain_all_day"):
            rain += ", почти весь день"
        elif W["rain_window"]:
            rain += f", главное окно {W['rain_window'][0]:02d}:00-{W['rain_window'][1]:02d}:00"
        lines.append(rain)
    elif prob is not None:
        lines.append(f"🌧 Дождя почти нет (вероятность до {round(prob)}%)")

    if W["wind"] is not None:
        wind = f"💨 Ветер до {round(W['wind'])} км/ч"
        if W["gust"] is not None:
            wind += f", порывы до {round(W['gust'])}"
        lines.append(wind)
    if W["uv"] is not None:
        lines.append(f"🧴 УФ-индекс до {round(W['uv'])}")
    if W["sunrise"] and W["sunset"]:
        lines.append(f"🌅 Рассвет {W['sunrise']}, закат {W['sunset']}")
    if W["sea"] is not None:
        sea = f"🌊 Море: {round(W['sea'])}°"
        if W["wave"] is not None:
            sea += f", волны до {_r1(W['wave'])} м"
        lines.append(sea)
    lines.append(f"🏖 {beach_verdict(W)}")
    return "\n".join(lines)


def weather_threads(W: dict, today) -> str:
    def pick(options):
        return options[today.toordinal() % len(options)]

    tmax, tmin = round(W["tmax"]), round(W["tmin"])
    prob = round(W["rain_prob"] or 0)
    mm = W["rain_mm"] or 0
    gust = round(W["gust"] or 0)
    code = W["code"] or 0
    desc = WMO.get(code, ("переменная облачность", ""))[0]
    s = CITY_SHORT
    win = W.get("rain_window")
    if W.get("rain_all_day"):
        rainwin = ", почти весь день"
    else:
        rainwin = f", основное окно {win[0]:02d}:00-{win[1]:02d}:00" if win else ""

    if code >= 95:
        kind = "storm"
    elif prob >= 60 or mm >= 5:
        kind = "rain"
    elif gust >= 50:
        kind = "wind"
    elif tmax >= 30:
        kind = "hot"
    elif tmax >= 25 and prob < 40:
        kind = "beach"
    elif tmax >= 19:
        kind = "mild"
    else:
        kind = "cold"

    hooks = {
        "storm": [f"⛈ {s} сегодня с грозой, шанс дождя до {prob}%.",
                  f"⛈ Сегодня на острове гроза. Дождь вероятен на {prob}%, порывы до {gust} км/ч."],
        "rain": [f"☔️ {s} сегодня мокрая: дождь вероятен на {prob}%{rainwin}.",
                 f"☔️ Зонт сегодня нужен: до {prob}% на дождь{rainwin}."],
        "wind": [f"💨 {s} сегодня с ветром: порывы до {gust} км/ч.",
                 f"💨 На острове ветрено, порывы до {gust} км/ч."],
        "hot": [f"🔥 {s} сегодня до {tmax}°.",
                f"🥵 Сегодня на острове до {tmax}°, ночью {tmin}°."],
        "beach": [f"☀️ {s} сегодня до {tmax}°, шанс дождя {prob}%.",
                  f"🏖 Сегодня пляжный день: до {tmax}°, шанс дождя {prob}%."],
        "mild": [f"⛅️ {s} сегодня до {tmax}°, {desc}.",
                 f"🌤 Сегодня на острове до {tmax}°, {desc}."],
        "cold": [f"🧥 {s} сегодня прохладная: от {tmin}° до {tmax}°.",
                 f"🧥 Сегодня на острове от {tmin}° до {tmax}°, {desc}."],
    }
    questions = {
        "storm": ["Кто уже проверил, есть ли свет и заряжены ли телефоны?",
                  "Как у вас с электричеством в такие дни, держится?",
                  "Что делаете, когда на острове гремит?"],
        "rain": ["Что делаете, когда на острове льёт: кофейня, кино или сериал дома?",
                 "Кто всё равно пойдёт на пляж?",
                 "Чем занимаетесь в такой дождь?"],
        "wind": ["Кто сегодня всё равно идёт на пляж?",
                 "Какой пляж на острове в ветер самый закрытый?",
                 "Кайтеры, вам сегодня повезло?"],
        "hot": ["Как переживаете жару: море, кондиционер или душ каждый час?",
                "Какой пляж выбираете в такую жару?",
                "Кто уже в воде?"],
        "beach": ["На какой пляж сегодня?",
                  "Север острова или юг, куда сегодня едете?",
                  "Кто уже определился с пляжем?"],
        "mild": ["Чем занимаетесь в такую погоду?",
                 "Какой маршрут на сегодня: набережная, тропа или кофейня?",
                 "Куда сегодня выберетесь?"],
        "cold": ["Кто достал куртку, а кто всё равно в шлёпанцах?",
                 "Как греетесь в такие дни, если в доме нет отопления?",
                 "Что делаете, когда на острове холодно?"],
    }

    body = []
    temp_parts = []
    if W["t_morning"] is not None:
        temp_parts.append(f"Утром {W['t_morning']}°")
    temp_parts.append(f"днём {W['t_day']}°" if temp_parts else f"Днём {W['t_day']}°")
    if W["t_evening"] is not None:
        temp_parts.append(f"вечером {W['t_evening']}°")
    body.append(", ".join(temp_parts) + ".")

    extra = []
    if W["sea"] is not None:
        sea = f"Вода {round(W['sea'])}°"
        if W["wave"] is not None:
            sea += f", волны до {_r1(W['wave'])} м"
        extra.append(sea + ".")
    if kind != "wind" and gust >= 40:
        extra.append(f"Порывы до {gust} км/ч.")
    if W["uv"] is not None and W["uv"] >= 8:
        extra.append(f"УФ-индекс {round(W['uv'])}, крем нужен.")
    if extra:
        body.append(" ".join(extra))

    return dash(pick(hooks[kind]) + "\n\n" + "\n".join(body) + "\n\n" + pick(questions[kind]))


async def send_weather(bot: Bot, http: httpx.AsyncClient):
    try:
        data, marine = await fetch_weather(http)
        W = build_weather(data, marine)
        try:
            today = datetime.strptime(W["date"], "%Y-%m-%d").date()
        except Exception:
            today = now_utc().astimezone(timezone(timedelta(hours=-3))).date()
        await send_html(bot, weather_card(W, today))
        await send_html(bot, threads_message(weather_threads(W, today), "✍️ Threads: погода"))
    except Exception as e:
        logger.exception("Weather failed")
        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"⚠️ Прогноз погоды не получен ({human_error(e)}). Дайджест продолжаю.",
            )
        except Exception:
            pass


# ─── RSS ─────────────────────────────────────────────────────────────────────

async def fetch_rss_items(http: httpx.AsyncClient, seen_ids: set, max_age_days: int = ARCHIVE_DAYS):
    now = now_utc()
    sem = asyncio.Semaphore(5)

    async def one(url):
        async with sem:
            try:
                r = await http.get(url, headers={"User-Agent": UA}, follow_redirects=True, timeout=20)
                r.raise_for_status()
                return url, feedparser.parse(r.content), None
            except Exception as e:
                return url, None, e

    results = await asyncio.gather(*(one(u) for u in RSS_FEEDS))
    items, dead = [], []
    for url, feed, err in results:
        parsed = urlparse(url)
        label = parsed.netloc + parsed.path.rstrip("/")
        if err or feed is None or not feed.entries:
            logger.warning(f"RSS dead: {label} ({human_error(err) if err else 'пусто'})")
            dead.append(label)
            continue
        count = 0
        for entry in feed.entries[:15]:
            link = entry.get("link", "")
            if not link:
                continue
            uid = item_id(link)
            if uid in seen_ids:
                continue
            pub = extract_published_date(entry)
            if pub and (now - pub).days > max_age_days:
                continue
            tags = [t.get("term", "") for t in entry.get("tags", [])]
            summary = re.sub(r"<[^>]+>", " ", entry.get("summary", ""))
            items.append({
                "title": entry.get("title", ""),
                "url": link,
                "uid": uid,
                "source": feed.feed.get("title", parsed.netloc),
                "pub_date": pub,
                "event_dates": extract_event_dates(summary),
                "content_type": detect_content_type(link, tags),
            })
            count += 1
        logger.info(f"RSS {label}: {count} новых")
    logger.info(f"RSS итого: {len(items)}, не ответили: {len(dead)}")
    return items, dead


# ─── Claude ──────────────────────────────────────────────────────────────────

async def call_claude_with_retry(payload: dict, timeout: int = 300, retries: int = 2) -> dict:
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "anthropic-beta": "web-search-2025-03-05",
                        "content-type": "application/json",
                    },
                    json=payload,
                )
                return response.json()
        except httpx.ReadTimeout:
            if attempt < retries - 1:
                logger.warning(f"Claude timeout, retry {attempt + 1}...")
                await asyncio.sleep(10)
            else:
                raise
    return {}


KEYS = {
    "ЗАГОЛОВОК": "title", "ТЕКСТ": "text", "THREADS": "threads", "ДАТА": "date",
    "КАТЕГОРИЯ": "category", "ВАЖНОСТЬ": "importance", "РЕГИОН": "region",
    "ИСТОЧНИК": "source", "ССЫЛКА": "url",
}
KEY_RE = re.compile(r"^(" + "|".join(KEYS) + r")\s*:\s*(.*)$")


def clean_threads(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"(?<!\w)#[^\W\d]\w*", "", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return dash(re.sub(r"\n{3,}", "\n\n", text).strip())


def parse_posts(full_text: str) -> list[dict]:
    posts = []
    for chunk in full_text.split("---"):
        post, current = {}, None
        for raw in chunk.split("\n"):
            plain = raw.replace("**", "").replace("`", "").strip()
            m = KEY_RE.match(plain.lstrip("#>•*- ").strip())
            if m:
                current = KEYS[m.group(1)]
                post[current] = m.group(2).strip()
            elif current == "threads":
                post[current] += "\n" + plain
            elif current in ("title", "text") and plain:
                post[current] += " " + plain
        if not post.get("title") or not post.get("text"):
            continue
        url_match = re.search(r"https?://[^\s)>\]]+", post.get("url", ""))
        post["url"] = url_match.group(0) if url_match else ""
        imp = re.search(r"\d", post.get("importance", ""))
        post["importance"] = min(int(imp.group(0)), 5) if imp else 3
        post["threads"] = clean_threads(post.get("threads", ""))
        posts.append(post)
    return posts


def build_prompt(rss_items: list[dict], titles: list[str], window_hours: int) -> str:
    now = now_utc()
    date_to = now.strftime("%d.%m.%Y")
    date_from = (now - timedelta(hours=window_hours)).strftime("%d.%m.%Y")
    rss_text = "\n".join(
        f"- [{i['source']}] {i['title']} | {i['url']} | "
        f"{i['pub_date'].strftime('%d.%m.%Y') if i['pub_date'] else 'дата неизвестна'}"
        for i in rss_items[:80]
    ) or "(пусто)"
    search_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)
    sent_text = "\n".join(f"- {t}" for t in titles) or "(пока ничего)"

    return f"""Ты редактор ленты в Threads про Бразилию, с упором на Санта-Катарину и Флорианополис. Автор живёт во Флорианополисе. Аудитория: русскоязычные, которые живут в Бразилии, собираются сюда переехать или просто следят за страной. Тон живой и развлекательный, не деловой. Из твоей подборки автор сам выбирает, что публиковать.

Сегодня {date_to}. Бери только то, что опубликовано не раньше {date_from}.

ЖЁСТКОЕ ОГРАНИЧЕНИЕ. Только Бразилия. Мировая политика, события в других странах, зарубежные исследования и мировые новости про вещества не нужны, даже если история отличная. Событие должно происходить в Бразилии или прямо касаться людей, которые здесь живут. Если сомневаешься, не включай.

Что искать, по убыванию ценности:
1. Санта-Катарина и Флорианополис: события, погода и стихия, пляжи и качество воды, стройки и мосты, транспорт, сезон и туристы, полиция и происшествия, всё, что человек на острове замечает на улице.
2. Странное и вирусное по всей Бразилии: курьёзы, необычные находки, истории, которые хочется переслать.
3. Быт и правила для иностранцев: цены, новые правила, визы, налоги. Коротко и по делу, без экономического занудства.
4. Культура, еда, футбол, карнавал, музыка.
5. Природа и животные.
6. Резонансные дела общенационального масштаба.
7. Психоделики, каннабис, аяуаска, но только если это происходит в Бразилии. Не больше одной заметки за выпуск.

УЖЕ ОТПРАВЛЯЛИ за последние дни. Не повторяй эти истории, даже если нашёл их в другом источнике или в других словах. Продолжение допустимо только с новым фактом, и тогда это должно быть видно в заголовке:
{sent_text}

Веб-поиск по этим запросам (источники на португальском переводи на русский, имена и названия мест пиши так, как их пишут по-русски):
{search_text}

Материалы из RSS (с датой публикации):
{rss_text}

Не склеивай разные события под одним заголовком. Даты привязывай к событию: «принят», «подписан», «вступает в силу». Если в источнике их несколько, укажи все.

Напиши от 8 до 12 заметок. Если подходящих меньше, пиши меньше: шесть сильных лучше двенадцати с натяжками и повторами. Одну историю из разных источников бери один раз.

Формат СТРОГО (без звёздочек и markdown, каждый ключ на новой строке):
ЗАГОЛОВОК: [одно предложение, коротко и конкретно]
ТЕКСТ: [2-3 предложения, максимум 300 символов, только факты, для автора, чтобы быстро понять суть]
THREADS: [готовый пост по правилам ниже, может занимать несколько строк]
ДАТА: [ДД.ММ.ГГГГ, дата публикации материала]
КАТЕГОРИЯ: [странное / санта-катарина / быт / закон / культура / еда / спорт / природа / криминал / резонанс / психоделики / каннабис]
ВАЖНОСТЬ: [1-5, где 5 значит «точно стоит публиковать»]
РЕГИОН: [Флорианополис / Санта-Катарина / Юг Бразилии / Сан-Паулу / Рио / Амазония / Северо-Восток / Вся Бразилия]
ИСТОЧНИК: [название]
ССЫЛКА: [URL]
---

Как писать THREADS (от 350 до 480 символов вместе с переносами строк):
Строка 1: зацепка. Конкретная деталь, цифра или неожиданный факт из самой новости. Не вопрос-кликбейт, не «вы не поверите», не пересказ заголовка. Это единственное, что видно до кнопки «ещё».
Пустая строка.
Дальше 2-3 коротких предложения: что произошло, где, когда, одна конкретная деталь. Читатель не знает контекста и не откроет ссылку, поэтому всё нужное должно быть в тексте. Ссылки в посте нет, поэтому назови источник одним словом: «пишет G1», «по данным NSC».
Если применимо, одна строка о том, кого это касается и с какой даты.
Пустая строка.
Последняя строка: вопрос, на который человек ответит из своего опыта (про его пляж, его район, его случай). Не «что думаете?» и не «согласны?».

Голос: прямой, тёплый, чуть ироничный. Короткие фразы, конкретные детали вместо абстракций. К читателю на «вы» или безлично. Не пиши «я» и не придумывай личный опыт автора. Числа всегда с контекстом: сколько из скольких, за какой период. Если факт подтверждает один источник, так и скажи. Никаких выводов и оценок от себя, никаких драматичных противопоставлений.
Запрещено: тире длиннее дефиса, конструкции «не X, а Y», «вот о чём», «и тут самое интересное», «стало известно», «сообщается», хэштеги, ссылки.
Эмодзи: пост должен быть яркий. Ставь 4-6 эмодзи: один в начале первой строки, по одному в начале строки с фактами, в строке «кого касается» и перед вопросом. Выбирай эмодзи по смыслу новости (🌊 море, 🚗 дорога, 🍤 еда, ⚠️ предупреждение, 📍 место, 👀 вопрос), не повторяй один и тот же дважды. Эмодзи считаются в лимит символов, так что укладывайся в 480 с ними."""


async def claude_process(rss_items, seen_ids: set, titles: list[str], window_hours: int):
    prompt = build_prompt(rss_items, titles, window_hours)
    data = await call_claude_with_retry({
        "model": "claude-sonnet-4-6",
        "max_tokens": 8000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}],
    })
    if data.get("type") == "error" or "content" not in data:
        raise RuntimeError(f"Claude API: {str(data)[:300]}")

    full_text = "".join(b["text"] for b in data["content"] if b.get("type") == "text")
    logger.info(f"Claude ответил, символов: {len(full_text)}")
    parsed = parse_posts(full_text)

    now = now_utc()
    stats = {"seen": 0, "similar": 0, "region": 0, "old": 0}
    posts, batch_ids, batch_titles = [], set(), []
    for post in parsed:
        region = post.get("region", "").lower()
        if not any(part in region for part in ALLOWED_REGION_PARTS):
            stats["region"] += 1
            continue

        uid = item_id(post["url"]) if post["url"] else item_id(post["title"])
        if uid in seen_ids or uid in batch_ids:
            stats["seen"] += 1
            continue
        if any(is_similar(post["title"], t) for t in titles + batch_titles):
            stats["similar"] += 1
            continue

        label = "дата не определена"
        m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", post.get("date", ""))
        if m:
            try:
                d = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)), tzinfo=timezone.utc)
                age = (now - d).days
                if age > ARCHIVE_DAYS:
                    stats["old"] += 1
                    continue
                label = d.strftime("%d.%m.%Y")
                if age > FRESH_DAYS:
                    label = f"[АРХИВ] {label}"
            except ValueError:
                pass

        post["uid"] = uid
        post["date_label"] = label
        post["content_type"] = detect_content_type(post["url"], [])
        post["is_sc"] = (
            any(part in region for part in SC_REGION_PARTS)
            or post.get("category", "").lower() == "санта-катарина"
        )
        batch_ids.add(uid)
        batch_titles.append(post["title"])
        posts.append(post)

    posts.sort(key=lambda p: (not p["is_sc"], -p["importance"]))
    logger.info(f"Заметок готово: {len(posts)}, отсеяно: {stats}")
    return posts, stats


# ─── Карточки ────────────────────────────────────────────────────────────────

def format_card(post: dict, rss_meta: dict | None = None) -> str:
    icon = CATEGORY_ICONS.get(post.get("category", "").lower(), "📌")
    type_label = "🗞 НОВОСТЬ" if post.get("content_type") == "НОВОСТЬ" else "✍️ КОЛОНКА"
    meta = [f"📅 {esc(post.get('date_label', 'дата не определена'))}"]
    if post.get("region"):
        meta.append(f"🌐 {esc(post['region'])}")
    meta.append("⭐" * post["importance"])

    text = f"{icon} <b>{esc(post['title'])}</b>\n\n{esc(post['text'])}\n\n"
    text += " · ".join(meta) + "\n"
    if rss_meta and rss_meta.get("event_dates"):
        text += f"📌 Событие: {esc(', '.join(rss_meta['event_dates']))}\n"
    line = type_label
    if post.get("source"):
        line += f" · {esc(post['source'])}"
    if post.get("url"):
        line += f' · <a href="{html.escape(post["url"], quote=True)}">читать</a>'
    return (text + line)[:4000]


# ─── Главная функция ─────────────────────────────────────────────────────────

async def run_digest():
    logger.info("Старт")
    bot = Bot(token=BOT_TOKEN)

    async with httpx.AsyncClient(timeout=30) as http:
        # 1. Погода идёт первой и не зависит от остального
        await send_weather(bot, http)

        # 2. Память о том, что уже отправляли
        items, sha, state_ok, state_note = [], None, False, ""
        if GITHUB_TOKEN:
            try:
                items, sha = await gh_load_state(http)
                state_ok = True
            except Exception as e:
                logger.exception("State load failed")
                state_note = f"не удалось прочитать память ({state_hint(e)})"
        else:
            state_note = "не задана переменная GITHUB_TOKEN"
        now = now_utc()
        items = prune_items(items, now)
        seen_ids = {it["id"] for it in items}
        titles = recent_titles(items, now)
        # без памяти окно короче, чтобы повторов было меньше
        window_hours = 48 if state_ok else 30

        # 3. Сбор и подборка
        try:
            rss_items, dead_feeds = await fetch_rss_items(http, seen_ids, window_hours // 24 + 1)
        except Exception:
            logger.exception("RSS failed")
            rss_items, dead_feeds = [], []
        rss_index = {i["url"]: i for i in rss_items}

        posts, stats = None, {}
        try:
            posts, stats = await claude_process(rss_items, seen_ids, titles, window_hours)
        except httpx.ReadTimeout:
            await bot.send_message(chat_id=ADMIN_CHAT_ID,
                                   text="Независимый Портал: Claude не ответил вовремя. Попробуй Trigger Run позже.")
        except Exception as e:
            logger.exception("Claude failed")
            await bot.send_message(chat_id=ADMIN_CHAT_ID,
                                   text=f"Независимый Портал: ошибка при подборке новостей ({human_error(e)}). Смотри логи Render.")

        warnings = []
        if state_note:
            warnings.append(f"⚠️ Память отключена: {state_note}. Повторы возможны.")
        if dead_feeds:
            warnings.append("⚠️ Не ответили ленты: " + ", ".join(dead_feeds))

        sent = 0
        try:
            if posts is not None:
                sc = [p for p in posts if p["is_sc"]]
                rest = [p for p in posts if not p["is_sc"]]
                skipped = []
                if stats.get("seen", 0) + stats.get("similar", 0):
                    skipped.append(f"повторов {stats['seen'] + stats['similar']}")
                if stats.get("region"):
                    skipped.append(f"не про Бразилию {stats['region']}")
                if stats.get("old"):
                    skipped.append(f"старых {stats['old']}")

                head = f"📋 <b>Дайджест {now.astimezone(timezone(timedelta(hours=-3))).strftime('%d.%m.%Y')}</b>\n"
                if posts:
                    head += f"Заметок: {len(posts)} (Санта-Катарина: {len(sc)}, Бразилия: {len(rest)})\n"
                else:
                    head += "Нового по Бразилии сегодня не нашлось.\n"
                if skipped:
                    head += "Отсеяно: " + ", ".join(skipped) + "\n"
                if warnings:
                    head += "\n" + "\n".join(esc(w) for w in warnings)
                await send_html(bot, head)

                for title, group in (("🏝 Санта-Катарина", sc), ("🇧🇷 Вся Бразилия", rest)):
                    if not group:
                        continue
                    await send_html(bot, f"<b>{title}</b> · {len(group)}")
                    for post in group:
                        try:
                            await send_html(bot, format_card(post, rss_index.get(post["url"])), preview=True)
                            if post.get("threads"):
                                await send_html(bot, threads_message(post["threads"]))
                        except Exception:
                            logger.exception("Send failed")
                            continue
                        items.append({
                            "id": post["uid"], "title": post["title"][:140],
                            "url": post["url"], "ts": now_utc().isoformat(),
                        })
                        sent += 1
                        await asyncio.sleep(1)
        finally:
            # 4. Сохраняем память, даже если что-то упало по дороге
            if GITHUB_TOKEN and state_ok:
                try:
                    await gh_save_state(http, prune_items(items, now_utc()), sha)
                    logger.info(f"Память сохранена, записей: {len(items)}")
                except Exception as e:
                    logger.exception("State save failed")
                    try:
                        await bot.send_message(
                            chat_id=ADMIN_CHAT_ID,
                            text=f"⚠️ Память не сохранилась ({state_hint(e)}). Завтра возможны повторы.")
                    except Exception:
                        pass
    logger.info(f"Готово, отправлено заметок: {sent}")


if __name__ == "__main__":
    asyncio.run(run_digest())
