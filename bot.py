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
    "https://filtermag.org/feed/",
    "https://www.wola.org/feed/",
    "https://insightcrime.org/feed/",
    "https://www.release.org.uk/feed",
    # Региональные
    "https://meduza.io/rss/all",
    "https://www.bangkokpost.com/rss/data/topstories.xml",
    "https://www.irrawaddy.com/feed",
    # Vice
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

# Научные журналы RSS напрямую
JOURNAL_FEEDS = [
    "https://www.tandfonline.com/feed/rss/rjps20",  # Journal of Psychedelic Studies
    "https://www.nature.com/npp/rss.xml",           # Neuropsychopharmacology
    "https://www.sciencedirect.com/journal/drug-and-alcohol-dependence/rss",
    "https://jamanetwork.com/rss/site_3/67.xml",    # JAMA Psychiatry
]
RSS_FEEDS.extend(JOURNAL_FEEDS)


def get_date_range() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    two_days_ago = now - timedelta(hours=48)
    return two_days_ago.strftime("%B %d, %Y"), now.strftime("%B %d, %Y")


SEARCH_QUERIES = [
    # Психоделики
    "psilocybin research news",
    "MDMA therapy FDA news",
    "ayahuasca ibogaine news",
    "ketamine therapy depression news",
    "psychedelic medicine clinical trial news",
    "LSD microdosing research news",
    "DMT research news",
    # Каннабис
    "cannabis legalization news",
    "THC edibles products news",
    "marijuana law reform news",
    "cannabis business news",
    # Наркополитика и реформы
    "drug policy reform decriminalization news",
    "harm reduction naloxone news",
    "drug safe consumption site news",
    "drug war policy news",
    # Кокаин фентанил опиоиды
    "cocaine bust arrest seizure news",
    "fentanyl overdose crisis news",
    "opioid epidemic news",
    "heroin drug trafficking news",
    # Синтетика
    "methamphetamine bust news",
    "designer drugs psychoactive news",
    "synthetic drugs seized news",
    # Картели и трафик
    "drug cartel Latin America news",
    "drug trafficking arrest news",
    "narco Mexico Colombia news",
    # Резонанс
    "celebrity drug arrest news",
    "politician drugs scandal news",
    "drug court case verdict news",
    # Нейронаука и фармакология
    "neuroscience psychopharmacology research news",
    "brain consciousness substances research news",
    "addiction treatment breakthrough news",
    "substance use disorder treatment news",
    "pharma company drug scandal news",
    # Научные журналы — специально
    "psychedelic study published journal news",
    "psilocybin clinical trial results published",
    "MDMA PTSD study results published",
    "cannabis research published journal",
    "drug addiction study published",
    # Регионы
    "наркополитика новости",
    "drug policy Asia Thailand China news",
    "drug policy Europe news",
    "Israel psychedelic research news",
    "Africa drug policy news",
    "Australia drug reform news",
    "India drug law news",
    # Передозировки
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
}


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
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
    seen = load_seen()

    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:15]:
                url = entry.get("link", "")
                if not url or item_id(url) in seen:
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


async def claude_process(rss_items: list[dict]) -> list[dict]:
    rss_text = "\n".join(
        f"- [{i['source']}] {i['title']} | {i['url']}"
        for i in rss_items[:80]
    )
    search_queries_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)

    date_from, date_to = get_date_range()

    prompt = f"""Ты редактор Telegram-канала «Независимый Портал» — русскоязычного новостного портала обо всём что связано с веществами.

ВАЖНО: Сегодня {date_to}. Ищи ТОЛЬКО новости и исследования опубликованные после {date_from}. Всё что старше — игнорируй полностью.

Тематика канала:
- Психоделики и психоделическая медицина (псилоцибин, MDMA, аяуаска, ибогаин, кетамин, ЛСД)
- Каннабис — легализация, продукты, бизнес, законы
- Наркополитика — реформы, декриминализация, законы по всему миру
- Кокаин, фентанил, героин, метамфетамин — трафик, аресты, кризисы
- Картели и наркотрафик
- Снижение вреда
- Нейронаука и психофармакология
- Лечение зависимости
- Фармацевтика
- Новые научные исследования из журналов (Journal of Psychedelic Studies, Neuropsychopharmacology, JAMA Psychiatry и др.)
- Резонансные истории

Сделай веб-поиск по этим темам — ТОЛЬКО за последние 48 часов (после {date_from}):
{search_queries_text}

Также вот материалы из RSS-лент (уже отфильтрованы по дате):
{rss_text}

Найди ВСЕ интересные материалы строго за последние 48 часов. Для научных исследований допускается публикация за последний месяц если это важное исследование. Не фильтруй агрессивно — 10-15 заметок лучше чем пропустить важное.

Формат каждой заметки СТРОГО:
ЗАГОЛОВОК: [короткий, конкретный, двухчастный через точку]
ТЕКСТ: [2-4 предложения, только факты, живой язык]
ДАТА: [дата публикации ДД.ММ.ГГГГ]
КАТЕГОРИЯ: [исследование / политика / психоделики / регион / резонанс / криминал / каннабис / опиоиды / нейронаука / фармакология / зависимость]
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
                "model": "claude-opus-4-5",
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

    logger.info(f"Claude response preview: {full_text[:500]}")

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
        if post.get("title") and post.get("text"):
            posts.append(post)

    posts.sort(key=lambda x: int(x.get("importance", "3") or "3"), reverse=True)

    logger.info(f"Parsed posts: {len(posts)}")
    return posts


async def run_digest():
    logger.info("Starting digest...")
    bot = Bot(token=BOT_TOKEN)

    rss_items = await fetch_rss_items()
    logger.info(f"RSS items: {len(rss_items)}")

    posts = await claude_process(rss_items)
    logger.info(f"Posts ready: {len(posts)}")

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
            f"Проверено RSS-источников: {len(RSS_FEEDS)}\n"
            f"Найдено из RSS: {len(rss_items)}\n"
            f"Подготовлено заметок: {len(posts)}\n\n"
            f"Перешли нужные в канал вручную."
        ),
        parse_mode="HTML",
    )

    seen = load_seen()
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
