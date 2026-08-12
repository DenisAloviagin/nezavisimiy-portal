import os
import json
import asyncio
import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse

import feedparser
import httpx
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
RENDER_SERVICE_ID = os.environ.get("RENDER_SERVICE_ID", "")
SEEN_MSG_ID = os.environ.get("SEEN_MSG_ID", "")

# Конфиг возраста материалов
FRESH_DAYS = 14
ARCHIVE_DAYS = 90

RSS_FEEDS = [
    "https://psychedelicalpha.com/feed",
    "https://lucid.news/feed/",
    "https://thethirdwave.co/feed/",
    "https://doubleblindmag.com/feed/",
    "https://chacruna.net/feed/",
    "https://maps.org/feed/",
    "https://www.iceers.org/feed/",
    "https://psychedelicstoday.com/feed/",
    "https://psychedelicspotlight.com/feed/",
    "https://microdose.com/feed/",
    "https://sociedelic.com/feed/",
    "https://www.beckleyfoundation.org/feed/",
    "https://idpc.net/rss.xml",
    "https://erowid.org/rss/columns.xml",
    "https://drugpolicy.org/feed/",
    "https://transformdrugs.org/feed/",
    "https://filtermag.org/feed/",
    "https://www.wola.org/feed/",
    "https://insightcrime.org/feed/",
    "https://www.release.org.uk/feed",
    "https://meduza.io/rss/all",
    "https://www.bangkokpost.com/rss/data/topstories.xml",
    "https://www.irrawaddy.com/feed",
    "https://www.vice.com/en/rss",
]

SCIENCE_TERMS = [
    "psilocybin", "LSD", "DMT", "ayahuasca", "mescaline",
    "ibogaine", "MDMA", "ketamine", "5-MeO-DMT", "salvinorin",
    "Banisteriopsis caapi", "Psychotria viridis", "Tabernanthe iboga",
    "Lophophora williamsii", "Amanita muscaria",
    "drug decriminalization", "harm reduction", "opioid addiction treatment",
    "cannabis therapy", "psychedelic therapy",
]

HIGH_WEIGHT_JOURNALS = {
    "Journal of Psychedelic Studies", "Psychedelic Medicine",
    "Journal of Psychopharmacology", "JAMA Psychiatry",
    "The Lancet Psychiatry", "Neuropsychopharmacology",
    "Molecular Psychiatry", "Biological Psychiatry",
    "American Journal of Psychiatry", "Nature", "Nature Medicine",
    "Cell", "PNAS", "British Journal of Pharmacology",
    "Journal of Ethnopharmacology",
}

SEARCH_QUERIES = [
    "psilocybin research news",
    "MDMA therapy FDA news",
    "ayahuasca ibogaine news",
    "ketamine therapy depression news",
    "psychedelic medicine clinical trial news",
    "LSD microdosing research news",
    "cannabis legalization news",
    "THC edibles products news",
    "marijuana law reform news",
    "drug policy reform decriminalization news",
    "harm reduction naloxone news",
    "cocaine bust arrest seizure news",
    "opioid epidemic news",
    "heroin drug trafficking news",
    "methamphetamine bust news",
    "drug cartel Latin America news",
    "drug trafficking arrest news",
    "narco Mexico Colombia news",
    "celebrity drug arrest news",
    "politician drugs scandal news",
    "drug court case verdict news",
    "neuroscience psychopharmacology research news",
    "addiction treatment breakthrough news",
    "pharma company drug scandal news",
    "наркополитика новости",
    "drug policy Asia Thailand China news",
    "drug policy Europe news",
    "Israel psychedelic research news",
    "Australia drug reform news",
    "new psilocybin mushroom species discovered",
    "novel psychoactive substance ethnobotany",
    "drug overdose death news",
]

CATEGORY_ICONS = {
    "исследование": "🔬",
    "политика": "🏛",
    "психоделики": "💊",
    "регион": "🌍",
    "резонанс": "🔥",
    "криминал": "🚔",
    "каннабис": "🌿",
    "опиоиды": "⚠️",
    "нейронаука": "🧠",
    "фармакология": "⚗️",
    "зависимость": "🔗",
    "этноботаника": "🍄",
}

# Паттерны для извлечения дат событий из текста
DATE_PATTERNS = [
    r'\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}\b',
    r'\b\d{1,2}\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\b',
    r'\b(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{1,2},?\s+\d{4}\b',
    r'\b\d{1,2}\s+(janvier|février|mars|avril|mai|juin|juillet|août|septembre|octobre|novembre|décembre)\s+\d{4}\b',
]

OPINION_MARKERS = ['oped', 'op-ed', 'opinion', 'tribune', 'commentary', 'column', 'editorial', 'perspective']


# ─── Утилиты ─────────────────────────────────────────────────────────────────

def clean_text(text: str) -> str:
    return text.replace("—", "-").replace("–", "-")


def canonical_url(url: str) -> str:
    """Убираем UTM-параметры из URL для стабильного ключа дедупликации."""
    try:
        parsed = urlparse(url)
        params = parse_qs(parsed.query)
        clean_params = {k: v for k, v in params.items() if not k.startswith('utm_')}
        clean_query = urlencode(clean_params, doseq=True)
        return urlunparse(parsed._replace(query=clean_query))
    except Exception:
        return url


def item_id(url: str) -> str:
    return hashlib.sha256(canonical_url(url).encode()).hexdigest()[:16]


def get_date_range() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (now - timedelta(hours=48)).strftime("%B %d, %Y"), now.strftime("%B %d, %Y")


def extract_published_date(entry: dict) -> datetime | None:
    """Берём дату публикации, не дату обновления."""
    # Приоритет: published > pubDate > dc:date (НЕ updated/modified)
    for field in ['published_parsed', 'pubdate_parsed']:
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except Exception:
                pass
    # dc:date
    dc_date = entry.get('dc_date') or entry.get('date')
    if dc_date:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(dc_date)
        except Exception:
            pass
    return None


def extract_event_dates(text: str) -> list[str]:
    """Извлекаем даты событий из первых 1500 символов текста регуляркой."""
    found = []
    snippet = text[:1500]
    for pattern in DATE_PATTERNS:
        matches = re.findall(pattern, snippet, re.IGNORECASE)
        for m in matches:
            date_str = m if isinstance(m, str) else " ".join(m)
            if date_str not in found:
                found.append(date_str)
    return found[:3]  # максимум 3 даты


def detect_content_type(url: str, tags: list) -> str:
    """Определяем тип материала: НОВОСТЬ или КОЛОНКА."""
    url_lower = url.lower()
    for marker in OPINION_MARKERS:
        if marker in url_lower:
            return "КОЛОНКА"
    for tag in tags:
        tag_str = str(tag).lower()
        for marker in OPINION_MARKERS:
            if marker in tag_str:
                return "КОЛОНКА"
    return "НОВОСТЬ"


def age_label(pub_date: datetime | None) -> tuple[str, bool]:
    """Возвращает метку возраста и флаг архива."""
    if not pub_date:
        return "дата не определена", False
    now = datetime.now(timezone.utc)
    age = (now - pub_date).days
    if age > ARCHIVE_DAYS:
        return f"СЛИШКОМ СТАРОЕ ({age} дней)", True  # skip
    elif age > FRESH_DAYS:
        return f"[АРХИВ] {pub_date.strftime('%d.%m.%Y')}", False
    else:
        return pub_date.strftime('%d.%m.%Y'), False


# ─── Хранение seen_ids через Render API ──────────────────────────────────────

async def update_render_env(key: str, value: str):
    if not RENDER_API_KEY or not RENDER_SERVICE_ID:
        logger.warning("RENDER_API_KEY or RENDER_SERVICE_ID not set")
        return
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}"},
            )
            env_vars = resp.json()
            for var in env_vars:
                if var.get("key") == key:
                    await client.put(
                        f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars/{var['id']}",
                        headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"},
                        json={"value": value},
                    )
                    logger.info(f"Updated Render env: {key}")
                    return
            await client.post(
                f"https://api.render.com/v1/services/{RENDER_SERVICE_ID}/env-vars",
                headers={"Authorization": f"Bearer {RENDER_API_KEY}", "Content-Type": "application/json"},
                json={"key": key, "value": value},
            )
            logger.info(f"Created Render env: {key}")
    except Exception as e:
        logger.warning(f"Render API error: {e}")


async def load_seen(bot: Bot) -> set:
    if not SEEN_MSG_ID:
        logger.info("No SEEN_MSG_ID, starting fresh")
        return set()
    try:
        msg_id = int(SEEN_MSG_ID)
        forwarded = await bot.forward_message(
            chat_id=ADMIN_CHAT_ID,
            from_chat_id=ADMIN_CHAT_ID,
            message_id=msg_id
        )
        text = forwarded.text or ""
        await bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=forwarded.message_id)
        if text.startswith("SEEN:"):
            ids = [i.strip() for i in text[5:].split(",") if i.strip()]
            logger.info(f"Loaded {len(ids)} seen IDs from Telegram")
            return set(ids)
    except Exception as e:
        logger.warning(f"Could not load seen: {e}")
    return set()


async def save_seen(bot: Bot, seen: set):
    seen_list = list(seen)[-2000:]
    text = "SEEN:" + ",".join(seen_list)
    try:
        if SEEN_MSG_ID:
            try:
                await bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=int(SEEN_MSG_ID))
            except Exception:
                pass
        msg = await bot.send_message(chat_id=ADMIN_CHAT_ID, text=text[:4000])
        new_id = str(msg.message_id)
        await asyncio.sleep(0.5)
        await bot.delete_message(chat_id=ADMIN_CHAT_ID, message_id=msg.message_id)
        await update_render_env("SEEN_MSG_ID", new_id)
        logger.info(f"Saved {len(seen_list)} seen IDs")
    except Exception as e:
        logger.warning(f"Could not save seen: {e}")


# ─── RSS парсинг ─────────────────────────────────────────────────────────────

async def fetch_rss_items(seen: set) -> list[dict]:
    items = []
    now = datetime.now(timezone.utc)

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                url = entry.get("link", "")
                if not url:
                    continue

                uid = item_id(url)
                if uid in seen:
                    continue

                # Берём дату публикации (не обновления)
                pub_date = extract_published_date(entry)

                # Фильтр по возрасту
                if pub_date:
                    age_days = (now - pub_date).days
                    if age_days > ARCHIVE_DAYS:
                        continue  # слишком старое — пропускаем

                # Тип контента
                tags = [t.get('term', '') for t in entry.get('tags', [])]
                content_type = detect_content_type(url, tags)

                # Извлекаем даты событий из summary
                summary = entry.get("summary", "")
                event_dates = extract_event_dates(summary)

                items.append({
                    "title": entry.get("title", ""),
                    "url": url,
                    "uid": uid,
                    "summary": summary[:500],
                    "source": feed.feed.get("title", feed_url),
                    "pub_date": pub_date,
                    "event_dates": event_dates,
                    "content_type": content_type,
                })
        except Exception as e:
            logger.warning(f"RSS error {feed_url}: {e}")

    logger.info(f"RSS items after age filter: {len(items)}")
    return items


# ─── Научный канал ───────────────────────────────────────────────────────────

async def fetch_science_articles(seen: set) -> list[dict]:
    articles = []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    async with httpx.AsyncClient(timeout=30) as client:
        for term in SCIENCE_TERMS:
            try:
                resp = await client.get(
                    "https://www.ebi.ac.uk/europepmc/webservices/rest/search",
                    params={
                        "query": f"{term} AND FIRST_PDATE:[{cutoff} TO *]",
                        "format": "json",
                        "pageSize": 5,
                        "sort": "date desc",
                        "resultType": "core",
                    }
                )
                data = resp.json()
                for r in data.get("resultList", {}).get("result", []):
                    url = f"https://europepmc.org/article/{r.get('source','')}/{r.get('id','')}"
                    uid = item_id(url)
                    if uid in seen:
                        continue
                    journal = r.get("journalTitle", "")
                    pub_date_str = r.get("firstPublicationDate", "")
                    pub_date = None
                    if pub_date_str:
                        try:
                            pub_date = datetime.strptime(pub_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        except Exception:
                            pass
                    articles.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "uid": uid,
                        "summary": (r.get("abstractText", "") or r.get("title", ""))[:500],
                        "source": journal or "Europe PMC",
                        "pub_date": pub_date,
                        "event_dates": [],
                        "content_type": "ИССЛЕДОВАНИЕ",
                        "is_science": True,
                        "importance_boost": 1 if journal in HIGH_WEIGHT_JOURNALS else 0,
                    })
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Europe PMC error for {term}: {e}")
    logger.info(f"Science articles: {len(articles)}")
    return articles


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


async def claude_process(rss_items: list[dict], science_items: list[dict], seen: set) -> list[dict]:
    rss_text = "\n".join(f"- [{i['source']}] {i['title']} | {i['url']}" for i in rss_items[:60])
    science_text = "\n".join(f"- [{i['source']}] {i['title']} | {i['url']}" for i in science_items[:40])
    search_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)
    date_from, date_to = get_date_range()

    prompt = f"""Ты редактор Telegram-канала «Независимый Портал» — русскоязычного новостного портала обо всём что связано с веществами.

ВАЖНО: Сегодня {date_to}. Для новостей - только после {date_from}. Для науки - последние 30 дней.

Тематика: психоделики, каннабис, наркополитика, кокаин/опиоиды/метамфетамин, картели, снижение вреда, нейронаука, лечение зависимости, фармацевтика, этноботаника, резонансные истории.

Веб-поиск строго за последние 48 часов:
{search_text}

Новости из RSS (уже отфильтрованы по возрасту):
{rss_text}

Научные статьи из Europe PMC (30 дней):
{science_text}

Напиши 10-15 заметок. Включи 2-3 научные статьи.

Формат СТРОГО (без звёздочек, без markdown):
ЗАГОЛОВОК: [короткий, конкретный, двухчастный через точку]
ТЕКСТ: [строго 2-3 предложения, максимум 300 символов, только факты]
THREADS: [1-2 предложения с эмодзи в начале, максимум 200 символов, суть + ссылка в конце]
ДАТА: [ДД.ММ.ГГГГ — дата публикации материала]
КАТЕГОРИЯ: [исследование / политика / психоделики / регион / резонанс / криминал / каннабис / опиоиды / нейронаука / фармакология / зависимость / этноботаника]
ВАЖНОСТЬ: [1-5]
РЕГИОН: [США / Европа / Латинская Америка / Азия / Россия / Ближний Восток / Африка / Глобально]
ИСТОЧНИК: [название]
ССЫЛКА: [URL]
---"""

    data = await call_claude_with_retry({
        "model": "claude-sonnet-4-6",
        "max_tokens": 8000,
        "tools": [{"type": "web_search_20250305", "name": "web_search"}],
        "messages": [{"role": "user", "content": prompt}],
    })

    full_text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            full_text += block["text"]

    posts = []
    for chunk in full_text.split("---"):
        chunk = chunk.strip()
        if not chunk:
            continue
        post = {}
        for line in chunk.split("\n"):
            line = line.strip()
            if line.startswith("ЗАГОЛОВОК:"):
                post["title"] = line.replace("ЗАГОЛОВОК:", "").strip()
            elif line.startswith("ТЕКСТ:"):
                post["text"] = line.replace("ТЕКСТ:", "").strip()
            elif line.startswith("THREADS:"):
                post["threads"] = line.replace("THREADS:", "").strip()
            elif line.startswith("ДАТА:"):
                post["date"] = line.replace("ДАТА:", "").strip()
            elif line.startswith("КАТЕГОРИЯ:"):
                post["category"] = line.replace("КАТЕГОРИЯ:", "").strip().lower()
            elif line.startswith("ВАЖНОСТЬ:"):
                post["importance"] = line.replace("ВАЖНОСТЬ:", "").strip()
            elif line.startswith("РЕГИОН:"):
                post["region"] = line.replace("РЕГИОН:", "").strip()
            elif line.startswith("ИСТОЧНИК:"):
                post["source"] = line.replace("ИСТОЧНИК:", "").strip()
            elif line.startswith("ССЫЛКА:"):
                post["url"] = line.replace("ССЫЛКА:", "").strip()

        if not post.get("title") or not post.get("text"):
            continue
        if post.get("url") and item_id(post["url"]) in seen:
            continue

        # Фильтр по дате
        date_str = post.get("date", "")
        if date_str:
            try:
                post_date = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - post_date).days
                if age_days > ARCHIVE_DAYS:
                    continue
                if age_days > FRESH_DAYS:
                    post["date"] = f"[АРХИВ] {date_str}"
            except ValueError:
                pass

        # Добавляем тип контента
        url = post.get("url", "")
        post["content_type"] = detect_content_type(url, [])

        posts.append(post)

    posts.sort(key=lambda x: int(x.get("importance", "3") or "3"), reverse=True)
    logger.info(f"Posts ready: {len(posts)}")
    return posts


# ─── Форматирование ───────────────────────────────────────────────────────────

def format_post(post: dict, rss_meta: dict | None = None) -> str:
    icon = CATEGORY_ICONS.get(post.get("category", ""), "📌")
    try:
        importance = "⭐" * int(post.get("importance", "3") or "3")
    except ValueError:
        importance = "⭐⭐⭐"

    content_type = post.get("content_type", "НОВОСТЬ")
    type_label = "📰 НОВОСТЬ" if content_type == "НОВОСТЬ" else "✍️ КОЛОНКА"

    meta = []
    if post.get("region"):
        meta.append(f"🌐 {post['region']}")
    meta.append(importance)

    text = f"{icon} <b>{clean_text(post['title'])}</b>\n\n"
    text += f"{clean_text(post['text'])}\n\n"

    # Дата публикации
    pub_date_str = post.get("date", "дата не определена")
    text += f"📅 Публикация: {pub_date_str}\n"

    # Дата события из RSS метаданных
    if rss_meta and rss_meta.get("event_dates"):
        text += f"📌 Событие: {', '.join(rss_meta['event_dates'])}\n"
    else:
        text += f"📌 Событие: не извлечена\n"

    text += f"{type_label}\n"
    text += " · ".join(meta) + "\n"

    if post.get("source"):
        text += f"📰 {post['source']}"
    if post.get("url"):
        text += f" — <a href='{post['url']}'>читать</a>"

    return text[:4000]


# ─── Главная функция ──────────────────────────────────────────────────────────

async def run_digest():
    logger.info("Starting digest...")
    bot = Bot(token=BOT_TOKEN)

    seen = await load_seen(bot)

    rss_items, science_items = await asyncio.gather(
        fetch_rss_items(seen),
        fetch_science_articles(seen)
    )
    logger.info(f"RSS: {len(rss_items)}, Science: {len(science_items)}")

    # Индекс RSS метаданных по URL для обогащения карточек
    rss_index = {item["url"]: item for item in rss_items}

    try:
        posts = await claude_process(rss_items, science_items, seen)
    except httpx.ReadTimeout:
        logger.error("Claude timed out")
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="Независимый Портал: Claude не ответил вовремя. Попробуй Trigger Run позже."
        )
        return

    if not posts:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="Независимый Портал: свежих материалов не найдено."
        )
        await save_seen(bot, seen)
        return

    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📋 <b>Дайджест {datetime.now(timezone.utc).strftime('%d.%m.%Y')}</b>\n"
            f"RSS: {len(rss_items)} новостей\n"
            f"Наука: {len(science_items)} статей\n"
            f"Заметок: {len(posts)}\n\n"
            f"Перешли нужные в канал вручную."
        ),
        parse_mode="HTML",
    )

    for post in posts:
        rss_meta = rss_index.get(post.get("url", ""))
        text = format_post(post, rss_meta)

        try:
            await bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
        except Exception as e:
            logger.warning(f"HTML send failed: {e}")
            try:
                plain = f"{post['title']}\n\n{post['text']}\n\n{post.get('url', '')}"
                await bot.send_message(chat_id=ADMIN_CHAT_ID, text=plain)
            except Exception as e2:
                logger.error(f"Plain send failed: {e2}")

        if post.get("threads"):
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=clean_text(post["threads"])[:500],
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Threads send failed: {e}")

        if post.get("url"):
            seen.add(item_id(post["url"]))
        await asyncio.sleep(1)

    await save_seen(bot, seen)
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run_digest())
