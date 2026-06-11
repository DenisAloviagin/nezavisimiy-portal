import os
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta
from pathlib import Path

import feedparser
import httpx
from telegram import Bot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SEEN_FILE = Path("seen_ids.json")

RSS_FEEDS = [
    "https://psychedelicalpha.com/feed",
    "https://lucid.news/feed/",
    "https://thethirdwave.co/feed/",
    "https://doubleblindmag.com/feed/",
    "https://chacruna.net/feed/",
    "https://maps.org/feed/",
    "https://www.iceers.org/feed/",
    "https://drugpolicy.org/feed/",
    "https://transformdrugs.org/feed/",
    "https://filtermag.org/feed/",
    "https://www.wola.org/feed/",
    "https://insightcrime.org/feed/",
    "https://meduza.io/rss/all",
    "https://www.bangkokpost.com/rss/data/topstories.xml",
    "https://www.irrawaddy.com/feed",
    "https://www.release.org.uk/feed",
]

PUBMED_QUERIES = ["psilocybin", "MDMA+therapy", "ayahuasca", "ibogaine", "ketamine+depression", "cannabis+policy"]
for q in PUBMED_QUERIES:
    RSS_FEEDS.append(f"https://pubmed.ncbi.nlm.nih.gov/rss/search/?term={q}&limit=5&format=rss")

SEARCH_QUERIES = [
    "psychedelic therapy research news this week",
    "drug policy reform news 2025",
    "cannabis legalization news world",
    "Thailand cannabis drug policy news",
    "China drug policy news",
    "Latin America drug policy ayahuasca news",
    "Israel psychedelic research news",
    "psychedelic medicine FDA DEA news",
    "наркополитика новости",
    "funny weird drug arrest news 2025",
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
                if not url or item_id(url) in seen:
                    continue
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


async def claude_process(rss_items: list[dict]) -> list[dict]:
    rss_text = "\n".join(f"- [{i['source']}] {i['title']} | {i['url']}" for i in rss_items[:60])
    search_queries_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)

    prompt = f"""Ты редактор Telegram-канала «Независимый Портал» — русскоязычного портала о наркополитике и психоделических исследованиях.

Вот свежие материалы из RSS за последние 36 часов:
{rss_text}

Сделай веб-поиск по этим темам чтобы найти важное что не попало в RSS:
{search_queries_text}

Отбери 4-6 самых интересных материалов и напиши по каждому короткую заметку на русском.

Формат каждой заметки:
ЗАГОЛОВОК: [короткий, в стиле Vice, двухчастный через точку]
ТЕКСТ: [2-4 предложения, только факты, живой язык]
ИСТОЧНИК: [название]
ССЫЛКА: [URL]
---

Критерии: реальная новость или исследование, мировое значение или яркий курьёз."""

    async with httpx.AsyncClient(timeout=180) as client:
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


async def run_digest():
    logger.info("Starting digest...")
    bot = Bot(token=BOT_TOKEN)

    rss_items = await fetch_rss_items()
    logger.info(f"RSS items: {len(rss_items)}")

    posts = await claude_process(rss_items)
    logger.info(f"Posts ready: {len(posts)}")

    if not posts:
        await bot.send_message(chat_id=ADMIN_CHAT_ID, text="Независимый Портал: новостей за сегодня не найдено.")
        return

    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=f"📋 Дайджест {datetime.utcnow().strftime('%d.%m.%Y')} — {len(posts)} заметок\n\nПеречитай и перешли нужные в канал @nezavisimiy_portal вручную.",
        parse_mode="HTML",
    )

    seen = load_seen()
    for post in posts:
        text = f"<b>{post['title']}</b>\n\n{post['text']}\n\n"
        if post.get("source"):
            text += f"📰 {post['source']}"
        if post.get("url"):
            text += f" — <a href='{post['url']}'>читать</a>"

        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
        if post.get("url"):
            seen.add(item_id(post["url"]))
        await asyncio.sleep(1)

    save_seen(seen)
    logger.info("Digest sent.")


if __name__ == "__main__":
    asyncio.run(run_digest())
