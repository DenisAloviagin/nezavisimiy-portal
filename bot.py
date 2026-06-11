import os
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import httpx
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- CONFIG ---
BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SEEN_FILE = Path("seen_ids.json")

# --- RSS SOURCES ---
RSS_FEEDS = [
    # Психоделические медиа
    "https://psychedelicalpha.com/feed",
    "https://lucid.news/feed/",
    "https://thethirdwave.co/feed/",
    "https://doubleblindmag.com/feed/",
    "https://chacruna.net/feed/",
    "https://maps.org/feed/",
    "https://www.iceers.org/feed/",
    # Наркополитика
    "https://drugpolicy.org/feed/",
    "https://transformdrugs.org/feed/",
    "https://www.emcdda.europa.eu/rss/news_en.xml",
    "https://filtermag.org/feed/",
    "https://www.wola.org/feed/",
    "https://insightcrime.org/feed/",
    # Регионы
    "https://meduza.io/rss/all",
    "https://www.bangkokpost.com/rss/data/topstories.xml",
    "https://www.irrawaddy.com/feed",
    "https://www.trimbos.nl/rss.xml",
    "https://www.release.org.uk/feed",
    # Научные журналы
    "https://www.tandfonline.com/feed/rss/rjps20",
    "https://www.liebertpub.com/action/showFeed?type=etoc&feed=rss&jc=psymed",
    "https://www.nature.com/npp/rss.xml",
]

# PubMed RSS по ключевым запросам
PUBMED_QUERIES = [
    "psilocybin",
    "MDMA+therapy",
    "ayahuasca",
    "ibogaine",
    "ketamine+depression",
    "cannabis+policy",
    "drug+decriminalization",
]
for q in PUBMED_QUERIES:
    RSS_FEEDS.append(
        f"https://pubmed.ncbi.nlm.nih.gov/rss/search/?term={q}&limit=5&format=rss"
    )

# Веб-поиск по регионам (запросы для Claude)
SEARCH_QUERIES = [
    "psychedelic therapy research news this week",
    "drug policy reform news 2025",
    "cannabis legalization news world",
    "naркополитика новости",
    "Thailand cannabis drug policy news",
    "China drug policy crackdown news",
    "India psychedelics drug law news",
    "Latin America drug policy ayahuasca news",
    "Israel psychedelic research clinical trial",
    "South Africa drug decriminalization news",
    "psychedelic medicine FDA DEA news",
    "drug trafficking cartel news Latin America",
    "funny weird drug arrest news",
]


def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))


def item_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


async def fetch_rss_items() -> list[dict]:
    items = []
    cutoff = datetime.utcnow() - timedelta(hours=36)
    seen = load_seen()

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                url = entry.get("link", "")
                if not url:
                    continue
                uid = item_id(url)
                if uid in seen:
                    continue
                # Проверяем дату
                published = entry.get("published_parsed")
                if published:
                    pub_dt = datetime(*published[:6])
                    if pub_dt < cutoff:
                        continue
                items.append({
                    "title": entry.get("title", ""),
                    "url": url,
                    "summary": entry.get("summary", "")[:500],
                    "source": feed.feed.get("title", feed_url),
                })
        except Exception as e:
            logger.warning(f"RSS error {feed_url}: {e}")

    return items


async def claude_search_and_filter(rss_items: list[dict]) -> list[dict]:
    """Передаём Claude RSS-items + запросы на веб-поиск, получаем готовые заметки."""

    rss_text = "\n".join(
        f"- [{i['source']}] {i['title']} | {i['url']}" for i in rss_items[:60]
    )

    search_queries_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)

    prompt = f"""Ты редактор новостного Telegram-канала «Независимый Портал» — русскоязычного независимого портала о наркополитике, психоделических исследованиях и связанных темах.

Вот свежие материалы из RSS-лент за последние 36 часов:
{rss_text}

Также сделай веб-поиск по этим темам чтобы найти то, что не попало в RSS:
{search_queries_text}

Задача:
1. Отбери 3-7 самых интересных и актуальных материалов (из RSS + найденных через поиск)
2. По каждому напиши короткую заметку для Telegram на русском языке

Формат каждой заметки (строго):
ЗАГОЛОВОК: [короткий, конкретный, в стиле Vice, двухчастный через точку]
ТЕКСТ: [2-4 предложения с фактами. Живой язык, без шаблонов. Без «Моего видения».]
ИСТОЧНИК: [название издания]
ССЫЛКА: [полный URL]
---

Критерии отбора: реальная новость или исследование (не мнение), мировое значение или яркий курьёз, не дубль уже опубликованного."""

    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "web-search-2025-03-05",
                "content-type": "application/json",
            },
            json={
                "model": "claude-opus-4-5",
                "max_tokens": 4000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        data = response.json()

    # Извлекаем текстовый ответ
    full_text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            full_text += block["text"]

    # Парсим заметки
    posts = []
    for chunk in full_text.split("---"):
        chunk = chunk.strip()
        if not chunk:
            continue
        post = {}
        for line in chunk.split("\n"):
            if line.startswith("ЗАГОЛОВОК:"):
                post["title"] = line.replace("ЗАГОЛОВОК:", "").strip()
            elif line.startswith("ТЕКСТ:"):
                post["text"] = line.replace("ТЕКСТ:", "").strip()
            elif line.startswith("ИСТОЧНИК:"):
                post["source"] = line.replace("ИСТОЧНИК:", "").strip()
            elif line.startswith("ССЫЛКА:"):
                post["url"] = line.replace("ССЫЛКА:", "").strip()
        if post.get("title") and post.get("text"):
            posts.append(post)

    return posts


def format_post(post: dict) -> str:
    text = f"<b>{post['title']}</b>\n\n"
    text += f"{post['text']}\n\n"
    if post.get("source"):
        text += f"📰 {post['source']}"
    if post.get("url"):
        text += f" — <a href='{post['url']}'>читать</a>"
    return text


async def send_digest_to_admin(posts: list[dict]):
    bot = Bot(token=BOT_TOKEN)

    if not posts:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="Независимый Портал: новостей за сегодня не найдено.",
        )
        return

    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"📋 <b>Дайджест — {datetime.utcnow().strftime('%d.%m.%Y')}</b>\nНайдено заметок: {len(posts)}\nОдобри каждую для публикации в канал.",
        parse_mode="HTML",
    )

    for i, post in enumerate(posts):
        text = format_post(post)
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Опубликовать", callback_data=f"pub_{i}"),
                InlineKeyboardButton("❌ Пропустить", callback_data=f"skip_{i}"),
            ]
        ])
        msg = await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
            disable_web_page_preview=False,
        )
        # Сохраняем пост во временное хранилище
        post["msg_id"] = msg.message_id

    # Сохраняем посты в файл для callback handler
    Path("pending_posts.json").write_text(json.dumps(posts, ensure_ascii=False))


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    pending = json.loads(Path("pending_posts.json").read_text()) if Path("pending_posts.json").exists() else []

    if data.startswith("pub_"):
        idx = int(data.split("_")[1])
        if idx < len(pending):
            post = pending[idx]
            text = format_post(post)
            await context.bot.send_message(
                chat_id=CHANNEL_ID,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=False,
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("✅ Опубликовано в канал.")

            # Помечаем как просмотренное
            seen = load_seen()
            if post.get("url"):
                seen.add(item_id(post["url"]))
            save_seen(seen)

    elif data.startswith("skip_"):
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text("⏭ Пропущено.")


async def run_digest():
    logger.info("Starting digest run...")
    rss_items = await fetch_rss_items()
    logger.info(f"RSS items fetched: {len(rss_items)}")
    posts = await claude_search_and_filter(rss_items)
    logger.info(f"Posts prepared: {len(posts)}")
    await send_digest_to_admin(posts)
    logger.info("Digest sent to admin.")


async def main():
    """Запуск бота для обработки кнопок одобрения."""
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot started, waiting for approvals...")
    await app.run_polling()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "digest":
        asyncio.run(run_digest())
    else:
        asyncio.run(main())
