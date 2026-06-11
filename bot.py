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
    RSS_FEEDS.append(f"https://pubmed.ncbi.nlm.nih.gov/rss/search/?term={q}&limit=5&format=rss")

SEARCH_QUERIES = [
    "psilocybin research news 2025",
    "MDMA therapy FDA news 2025",
    "ayahuasca ibogaine news 2025",
    "ketamine therapy news 2025",
    "psychedelic medicine clinical trial 2025",
    "LSD microdosing research 2025",
    "DMT research news 2025",
    "cannabis legalization news 2025",
    "THC edibles products news 2025",
    "marijuana dispensary cannabis business news 2025",
    "cannabis law reform world 2025",
    "CBD research news 2025",
    "drug policy reform decriminalization 2025",
    "drug legalization country news 2025",
    "harm reduction naloxone drug news 2025",
    "drug safe consumption room news 2025",
    "drug war policy news 2025",
    "cocaine bust arrest seizure news 2025",
    "fentanyl overdose crisis news 2025",
    "opioid epidemic news 2025",
    "heroin drug trafficking news 2025",
    "methamphetamine bust news 2025",
    "designer drugs new psychoactive substances 2025",
    "synthetic drugs seized news 2025",
    "MDMA ecstasy seized news 2025",
    "drug cartel news Latin America 2025",
    "drug trafficking arrest news 2025",
    "narco news Mexico Colombia 2025",
    "drug smuggling bust news 2025",
    "celebrity drug arrest scandal 2025",
    "politician drugs scandal news 2025",
    "drug court case verdict news 2025",
    "neuroscience psychopharmacology research news 2025",
    "brain consciousness drugs research 2025",
    "addiction neuroscience treatment news 2025",
    "antidepressant new research news 2025",
    "substance use disorder treatment breakthrough 2025",
    "pharmaceutical drug addiction treatment news 2025",
    "opioid addiction medication news 2025",
    "drug rehabilitation new method 2025",
    "pharma company drug scandal 2025",
    "наркополитика новости 2025",
    "drug policy Asia Thailand China news 2025",
    "drug policy Europe news 2025",
    "Israel psychedelic research news 2025",
    "Africa drug policy news 2025",
    "India drug law news 2025",
    "Australia drug reform news 2025",
    "drug overdose death news 2025",
    "drug poisoning contamination news 2025",
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
    cutoff = datetime.utcnow() - timedelta(hours=48)
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
    rss_text = "\n".join(
        f"- [{i['source']}] {i['title']} | {i['url']}"
        for i in rss_items[:80]
    )
    search_queries_text = "\n".join(f"- {q}" for q in SEARCH_QUERIES)

    prompt = f"""Ты редактор Telegram-канала «Независимый Портал» — русскоязычного новостного портала обо всём что связано с веществами и их влиянием на человека и общество.

Тематика канала — всё что связано с веществами:
- Психоделики и психоделическая медицина (псилоцибин, MDMA, аяуаска, ибогаин, кетамин, ЛСД)
- Каннабис — легализация, продукты, бизнес, законы
- Наркополитика — реформы, декриминализация, законы по всему миру
- Кокаин, фентанил, героин, метамфетамин — трафик, аресты, кризисы
- Картели и наркотрафик — Латинская Америка, Азия, Европа
- Снижение вреда — налоксон, безопасные комнаты, программы помощи
- Нейронаука и психофармакология — как вещества влияют на мозг и сознание
- Лечение зависимости — новые методы, прорывы, споры
- Фармацевтика — новые препараты, скандалы фармкомпаний, опиоидный кризис
- Резонансные истории — аресты знаменитостей, суды, скандалы

Сделай веб-поиск по всем этим темам — ищи новости строго за последние 48 часов:
{search_queries_text}

Также вот материалы из RSS-лент за последние 48 часов:
{rss_text}

Твоя задача: найти ВСЕ интересные новости по теме за последние 48 часов. Не фильтруй агрессивно — лучше прислать 10-15 заметок чем пропустить что-то важное. Редактор сам выберет что публиковать.

Формат каждой заметки СТРОГО вот такой, без отступлений, без нумерации:
ЗАГОЛОВОК: [короткий, конкретный, двухчастный через точку]
ТЕКСТ: [2-4 предложения, только факты, живой язык]
ДАТА: [дата публикации ДД.ММ.ГГГГ]
КАТЕГОРИЯ: [одно слово: исследование / политика / психоделики / регион / резонанс / криминал / каннабис / опиоиды / нейронаука / фармакология / зависимость]
ВАЖНОСТЬ: [цифра 1-5]
РЕГИОН: [США / Европа / Латинская Америка / Азия / Россия / Ближний Восток / Африка / Глобально]
ИСТОЧНИК: [название издания]
ССЫЛКА: [полный URL]
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
            f"📋 <b>Дайджест {datetime.utcnow().strftime('%d.%m.%Y')}</b>\n"
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
