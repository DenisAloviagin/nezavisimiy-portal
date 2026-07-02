import os
import json
import asyncio
import hashlib
import logging
from datetime import datetime, timedelta, timezone
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
    "https://www.release.org.uk/feed",
    "https://meduza.io/rss/all",
    "https://www.bangkokpost.com/rss/data/topstories.xml",
    "https://www.irrawaddy.com/feed",
    "https://www.vice.com/en/rss",
]

PUBMED_QUERIES = [
    "psilocybin", "MDMA+therapy", "ayahuasca", "ibogaine",
    "ketamine+depression", "cannabis+policy", "fentanyl",
    "opioid+crisis", "harm+reduction", "drug+decriminalization",
    "neuropsychopharmacology", "addiction+treatment", "substance+use+disorder",
    "psychedelic+neuroscience", "drug+dependence"
]
for q in PUBMED_QUERIES:
    RSS_FEEDS.append(f"https://pubmed.ncbi.nlm.nih.gov/rss/search/?term={q}&limit=10&format=rss")

JOURNAL_FEEDS = [
    "https://www.tandfonline.com/feed/rss/rjps20",
    "https://www.nature.com/npp/rss.xml",
    "https://www.sciencedirect.com/journal/drug-and-alcohol-dependence/rss",
    "https://jamanetwork.com/rss/site_3/67.xml",
]
RSS_FEEDS.extend(JOURNAL_FEEDS)


def get_date_range() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    two_days_ago = now - timedelta(hours=48)
    return two_days_ago.strftime("%B %d, %Y"), now.strftime("%B %d, %Y")


SEARCH_QUERIES = [
    "psilocybin research news",
    "MDMA therapy FDA news",
    "ayahuasca ibogaine news",
    "ketamine therapy depression news",
    "psychedelic medicine clinical trial news",
    "LSD microdosing research news",
    "DMT research news",
    "cannabis legalization news",
    "THC edibles products news",
    "marijuana law reform news",
    "drug policy reform decriminalization news",
    "harm reduction naloxone news",
    "cocaine bust arrest seizure news",
    "fentanyl overdose crisis news",
    "opioid epidemic news",
    "heroin drug trafficking news",
    "methamphetamine bust news",
    "designer drugs psychoactive news",
    "drug cartel Latin America news",
    "drug trafficking arrest news",
    "narco Mexico Colombia news",
    "celebrity drug arrest news",
    "politician drugs scandal news",
    "drug court case verdict news",
    "neuroscience psychopharmacology research news",
    "addiction treatment breakthrough news",
    "pharma company drug scandal news",
    "psychedelic study published journal news",
    "psilocybin clinical trial results published",
    "MDMA PTSD study results published",
    "наркополитика новости",
    "drug policy Asia Thailand China news",
    "drug policy Europe news",
    "Israel psychedelic research news",
    "Australia drug reform news",
    "new psilocybin mushroom species discovered",
    "new psychedelic compound discovered research",
    "novel psychoactive substance ethnobotany",
    "drug overdose death news",
    "drug poisoning contamination news",
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


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    # Храним максимум 3000 последних URL
    seen_list = list(seen)[-3000:]
    SEEN_FILE.write_text(json.dumps(seen_list))


def item_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


async def fetch_rss_items(seen: set) -> list[dict]:
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

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
                published = entry.get("published_parsed")
                if published:
                    pub_dt = datetime(*published[:6], tzinfo=timezone.utc)
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


async def claude_process(rss_items: list[dict], seen: set) -> list[dict]:
    rss_text = "\n".join(
        f"- [{i['source']}] {i['title']} | {i['url']}"
        for i in rss_items[:80]
    )
    search_queries_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)
    date_from, date_to = get_date_range()

    prompt = f"""Ты редактор Telegram-канала «Независимый Портал» — русскоязычного новостного портала обо всём что связано с веществами.

ВАЖНО: Сегодня {date_to}. Ищи ТОЛЬКО новости опубликованные после {date_from}. Всё что старше — игнорируй полностью.

Тематика: психоделики и психоделическая медицина, каннабис, наркополитика, кокаин/фентанил/героин/метамфетамин, картели, снижение вреда, нейронаука и психофармакология, лечение зависимости, фармацевтика, новые виды грибов и этноботаника, резонансные истории.

Сделай веб-поиск по этим темам строго за последние 48 часов (после {date_from}):
{search_queries_text}

Материалы из RSS за последние 48 часов:
{rss_text}

Найди ВСЕ интересные материалы за последние 48 часов. Для научных исследований допускается последний месяц если это важное открытие. Присылай до 15 заметок — лучше больше чем пропустить важное.

Формат СТРОГО (без звёздочек, без markdown, без жирного):
ЗАГОЛОВОК: [короткий, конкретный, двухчастный через точку]
ТЕКСТ: [2-4 предложения, только факты, живой язык]
ДАТА: [ДД.ММ.ГГГГ]
КАТЕГОРИЯ: [исследование / политика / психоделики / регион / резонанс / криминал / каннабис / опиоиды / нейронаука / фармакология / зависимость / этноботаника]
ВАЖНОСТЬ: [1-5]
РЕГИОН: [США / Европа / Латинская Америка / Азия / Россия / Ближний Восток / Африка / Глобально]
ИСТОЧНИК: [название]
ССЫЛКА: [URL]
---"""

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
                "model": "claude-sonnet-4-6",
                "max_tokens": 8000,
                "tools": [{"type": "web_search_20250305", "name": "web_search"}],
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        data = response.json()

    full_text = ""
    for block in data.get("content", []):
        if block.get("type") == "text":
            full_text += block["text"]

    logger.info(f"Claude response preview: {full_text[:300]}")

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

        # Фильтр дублей по URL
        if post.get("url") and item_id(post["url"]) in seen:
            logger.info(f"Duplicate skipped: {post['title'][:50]}")
            continue

        # Фильтр по дате
        date_str = post.get("date", "")
        if date_str:
            try:
                post_date = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=timezone.utc)
                cutoff_30 = datetime.now(timezone.utc) - timedelta(days=30)
                if post_date < cutoff_30:
                    logger.info(f"Old post filtered: {date_str}")
                    continue
            except ValueError:
                pass

        posts.append(post)

    posts.sort(key=lambda x: int(x.get("importance", "3") or "3"), reverse=True)
    logger.info(f"Posts ready: {len(posts)}")
    return posts


async def run_digest():
    logger.info("Starting digest...")
    bot = Bot(token=BOT_TOKEN)

    seen = load_seen()
    logger.info(f"Loaded {len(seen)} seen URLs")

    rss_items = await fetch_rss_items(seen)
    logger.info(f"RSS items: {len(rss_items)}")

    posts = await claude_process(rss_items, seen)
    logger.info(f"Posts to send: {len(posts)}")

    if not posts:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="Независимый Портал: свежих новостей за последние 48 часов не найдено."
        )
        return

    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📋 <b>Дайджест {datetime.now(timezone.utc).strftime('%d.%m.%Y')}</b>\n"
            f"Проверено RSS: {len(RSS_FEEDS)} источников\n"
            f"Новых из RSS: {len(rss_items)}\n"
            f"Заметок: {len(posts)}\n\n"
            f"Перешли нужные в канал вручную."
        ),
        parse_mode="HTML",
    )

    for post in posts:
        icon = CATEGORY_ICONS.get(post.get("category", ""), "📌")
        try:
            importance = "⭐" * int(post.get("importance", "3") or "3")
        except ValueError:
            importance = "⭐⭐⭐"

        meta = []
        if post.get("date"):
            meta.append(f"📅 {post['date']}")
        if post.get("region"):
            meta.append(f"🌐 {post['region']}")
        meta.append(importance)

        text = f"{icon} <b>{post['title']}</b>\n\n"
        text += f"{post['text']}\n\n"
        text += " · ".join(meta) + "\n"
        if post.get("source"):
            text += f"📰 {post['source']}"
        if post.get("url"):
            text += f" — <a href='{post['url']}'>читать</a>"

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

        if post.get("url"):
            seen.add(item_id(post["url"]))
        await asyncio.sleep(1)

    save_seen(seen)
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run_digest())
