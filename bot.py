import os
import json
import asyncio
import hashlib
import logging
import re
import html
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
    # Бразилия
    "https://g1.globo.com/rss/g1/sc/santa-catarina/",
    "https://g1.globo.com/rss/g1/pop-arte/",
    "https://g1.globo.com/rss/g1/mundo/",
    "https://g1.globo.com/rss/g1/economia/",
    "https://agenciabrasil.ebc.com.br/feed/ultimasnoticias/feed.xml",
    # Психоделики: одна рубрика, оставляем самое сильное
    "https://chacruna.net/feed/",
    "https://lucid.news/feed/",
    "https://psychedelicalpha.com/feed",
    "https://filtermag.org/feed/",
]

# Ежедневный сбор: свежее 3 дней идёт как есть, 3-7 дней с пометкой АРХИВ, старше 7 не берём
FRESH_DAYS = 3
ARCHIVE_DAYS = 7

SEARCH_QUERIES = [
    "Brasil notícia curiosa bizarra hoje",
    "Brazil weird news viral",
    "Florianópolis notícia curiosa",
    "Brasil animal selvagem notícia hoje",
    "Brasil achado inusitado descoberta",
    "Florianópolis notícias hoje",
    "Santa Catarina notícias hoje praia",
    "Florianópolis turismo temporada verão",
    "Santa Catarina clima tempestade alerta",
    "NSC Total Florianópolis cotidiano",
    "ND Mais Florianópolis",
    "Brasil custo de vida preços supermercado",
    "Brasil imigrantes estrangeiros nova regra visto",
    "Brasil lei nova entra em vigor",
    "Brasil dólar real cotação hoje",
    "Brazil expats news",
    "Brasil cultura festa tradição notícia",
    "Brasil gastronomia comida típica notícia",
    "Brasil futebol notícia polêmica",
    "Brasil carnaval novidades",
    "Brasil música cultura pop notícia",
    "Amazônia notícia descoberta",
    "Brasil meio ambiente fauna notícia",
    "Brasil crime caso nacional repercussão",
    "Brasil operação polícia facção notícia",
    "Brasil STF decisão polêmica",
    "Brasil ayahuasca regulamentação notícia",
    "Brasil cannabis STF Anvisa notícia",
    "Brasil psicodélicos pesquisa notícia",
    "psychedelics Brazil news",
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
    return html.escape(text.replace("—", "-").replace("–", "-"), quote=False)


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
    return (now - timedelta(hours=72)).strftime("%B %d, %Y"), now.strftime("%B %d, %Y")


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
        for m in re.finditer(pattern, snippet, re.IGNORECASE):
            date_str = m.group(0)
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


async def claude_process(rss_items: list[dict], seen: set) -> list[dict]:
    rss_text = "\n".join(f"- [{i['source']}] {i['title']} | {i['url']}" for i in rss_items[:80])
    search_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)
    date_from, date_to = get_date_range()

    prompt = f"""Ты редактор Telegram-канала «Независимый Портал». Теперь это русскоязычная лента про жизнь в Бразилии: для людей, которые здесь живут, собираются переехать или просто следят за страной. Канал развлекательный, не деловой. Редактор живёт во Флорианополисе (Санта-Катарина) и сам выбирает из подборки, что пойдёт в ролики.

ВАЖНО: сегодня {date_to}. Бери только материалы, опубликованные после {date_from}. Всё, что старше, пропускай.

Что искать, по убыванию ценности:
1. Странное и вирусное: курьёзы, необычные находки, истории, которые хочется переслать.
2. Санта-Катарина и Флорианополис: события, погода, сезон, стройки, транспорт, пляжи, то, что чувствуется на острове.
3. Жизнь и быт: цены, курс реала, новые правила для иностранцев, визы, налоги, но коротко и по делу, без экономического занудства.
4. Культура, еда, футбол, карнавал, музыка.
5. Природа и животные: Амазония, фауна, находки.
6. Криминал и резонансные дела общенационального масштаба.
7. Психоделики, каннабис, аяуаска в Бразилии: одна рубрика, не больше 1-2 заметок за выпуск.

Источники на португальском переводи на русский. Названия мест и людей пиши так, как их пишут по-русски, спорные имена оставляй в оригинале в скобках. Не склеивай разные события под одним заголовком. Даты привязывай к событию: «принят», «подписан», «вступает в силу», если в источнике их несколько, укажи все.

Веб-поиск по этим запросам:
{search_text}

Материалы из RSS (уже отфильтрованы по возрасту):
{rss_text}

Напиши 12-18 заметок. Не повторяй одну и ту же историю из разных источников: бери самый надёжный. Выводов, оценок и авторской позиции в конце заметок не пиши, только факты.

Формат СТРОГО (без звёздочек, без markdown):
ЗАГОЛОВОК: [короткий, конкретный, двухчастный через точку]
ТЕКСТ: [строго 2-3 предложения, максимум 300 символов, только факты]
THREADS: [1-2 предложения с эмодзи в начале, максимум 200 символов, суть + ссылка в конце]
ДАТА: [ДД.ММ.ГГГГ — дата публикации материала]
КАТЕГОРИЯ: [странное / санта-катарина / быт / закон / культура / еда / спорт / природа / криминал / резонанс / психоделики / каннабис]
ВАЖНОСТЬ: [1-5, где 5 - то, что точно стоит ролика]
РЕГИОН: [Санта-Катарина / Юг Бразилии / Сан-Паулу / Рио / Амазония / Вся Бразилия / Мир]
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
    type_label = "🗞 НОВОСТЬ" if content_type == "НОВОСТЬ" else "✍️ КОЛОНКА"

    meta = []
    if post.get("region"):
        meta.append(f"🌐 {clean_text(post['region'])}")
    meta.append(importance)

    text = f"{icon} <b>{clean_text(post['title'])}</b>\n\n"
    text += f"{clean_text(post['text'])}\n\n"

    # Дата публикации
    pub_date_str = clean_text(post.get("date", "дата не определена"))
    text += f"📅 Публикация: {pub_date_str}\n"

    # Дата события из RSS метаданных
    if rss_meta and rss_meta.get("event_dates"):
        text += f"📌 Событие: {clean_text(', '.join(rss_meta['event_dates']))}\n"
    else:
        text += f"📌 Событие: не извлечена\n"

    text += f"{type_label}\n"
    text += " · ".join(meta) + "\n"

    if post.get("source"):
        text += f"📰 {clean_text(post['source'])}"
    if post.get("url"):
        safe_url = html.escape(post["url"], quote=True)
        text += f' — <a href="{safe_url}">читать</a>'

    return text[:4000]


# ─── Главная функция ──────────────────────────────────────────────────────────

async def run_digest():
    logger.info("Starting digest...")
    bot = Bot(token=BOT_TOKEN)

    seen = await load_seen(bot)

    rss_items = await fetch_rss_items(seen)
    logger.info(f"RSS: {len(rss_items)}")

    # Индекс RSS метаданных по URL для обогащения карточек
    rss_index = {item["url"]: item for item in rss_items}

    try:
        posts = await claude_process(rss_items, seen)
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
                    text=post["threads"].replace("—", "-").replace("–", "-")[:500],
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
