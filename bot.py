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
    "fentanyl overdose crisis news",
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


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)[-3000:]))


def item_id(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()


def get_date_range() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    return (now - timedelta(hours=48)).strftime("%B %d, %Y"), now.strftime("%B %d, %Y")


async def fetch_rss_items(seen: set) -> list[dict]:
    items = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=48)
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
                    if item_id(url) in seen:
                        continue
                    journal = r.get("journalTitle", "")
                    articles.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "summary": (r.get("abstractText", "") or r.get("title", ""))[:500],
                        "source": journal or "Europe PMC",
                        "is_science": True,
                        "importance_boost": 1 if journal in HIGH_WEIGHT_JOURNALS else 0,
                    })
                await asyncio.sleep(0.3)
            except Exception as e:
                logger.warning(f"Europe PMC error for {term}: {e}")
    logger.info(f"Science articles: {len(articles)}")
    return articles


async def call_claude_with_retry(payload: dict, timeout: int = 300, retries: int = 2) -> dict:
    """Вызов Claude API с повторной попыткой при таймауте."""
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

ВАЖНО: Сегодня {date_to}. Для новостей — только после {date_from}. Для науки — последние 30 дней.

Тематика: психоделики, каннабис, наркополитика, кокаин/фентанил/опиоиды, картели, снижение вреда, нейронаука, лечение зависимости, фармацевтика, этноботаника, резонансные истории.

Веб-поиск строго за последние 48 часов:
{search_text}

Новости из RSS (48ч):
{rss_text}

Научные статьи из Europe PMC (30 дней):
{science_text}

Напиши 10-15 заметок. Включи 2-3 научные статьи.

Формат СТРОГО (без звёздочек, без markdown):
ЗАГОЛОВОК: [короткий, конкретный, двухчастный через точку]
ТЕКСТ: [2-4 предложения, только факты]
THREADS: [1-2 предложения для Threads с эмодзи в начале — суть новости + ссылка на источник в конце]
ИСТОЧНИК_ТЕКСТ: [1-2 предложения из оригинала на языке источника]
ДАТА: [ДД.ММ.ГГГГ]
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
            elif line.startswith("THREADS:"):
                post["threads"] = line.replace("THREADS:", "").strip()
            elif line.startswith("ИСТОЧНИК_ТЕКСТ:"):
                post["source_text"] = line.replace("ИСТОЧНИК_ТЕКСТ:", "").strip()
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

        date_str = post.get("date", "")
        if date_str:
            try:
                post_date = datetime.strptime(date_str, "%d.%m.%Y").replace(tzinfo=timezone.utc)
                if post_date < datetime.now(timezone.utc) - timedelta(days=30):
                    continue
            except ValueError:
                pass

        posts.append(post)

    posts.sort(key=lambda x: int(x.get("importance", "3") or "3"), reverse=True)
    return posts


async def verify_post(post: dict) -> dict:
    source_text = post.get("source_text", "")
    if not source_text:
        post["verified"] = True
        post["errors"] = []
        return post

    verify_prompt = f"""Ты фактчекер. Найди ошибки в заметке.

Заметка (русский):
{post['title']}
{post['text']}

Исходный текст источника:
{source_text}

Проверь:
1. Имена и топонимы — правильно переведены? (Georgia = штат или страна — решай по контексту)
2. Даты — каждая дата привязана к своему событию?
3. Числа — все цифры совпадают с источником?
4. Внутренняя логика — нет противоречий?

Ответь ТОЛЬКО в JSON без markdown:
{{"errors": ["ошибка 1"], "clean": true/false}}

Если ошибок нет: {{"errors": [], "clean": true}}"""

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-sonnet-4-6",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": verify_prompt}],
                },
            )
            data = response.json()
            result = json.loads(data["content"][0]["text"].strip())
            post["verified"] = result.get("clean", True)
            post["errors"] = result.get("errors", [])
    except Exception as e:
        logger.warning(f"Verify error: {e}")
        post["verified"] = True
        post["errors"] = []

    return post


def format_post(post: dict) -> str:
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
    return text


async def run_digest():
    logger.info("Starting digest...")
    bot = Bot(token=BOT_TOKEN)

    seen = load_seen()
    logger.info(f"Loaded {len(seen)} seen URLs")

    rss_items, science_items = await asyncio.gather(
        fetch_rss_items(seen),
        fetch_science_articles(seen)
    )
    logger.info(f"RSS: {len(rss_items)}, Science: {len(science_items)}")

    try:
        posts = await claude_process(rss_items, science_items, seen)
    except httpx.ReadTimeout:
        logger.error("Claude timed out after retries")
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="Независимый Портал: Claude не ответил вовремя. Попробуй запустить вручную позже."
        )
        return

    logger.info(f"Posts before verify: {len(posts)}")

    posts = list(await asyncio.gather(*[verify_post(p) for p in posts]))
    flagged = sum(1 for p in posts if not p.get("verified", True))
    # Отсеиваем заметки с ошибками — тебе приходят только чистые
    posts = [p for p in posts if p.get("verified", True)]
    logger.info(f"Posts ready: {len(posts)}, filtered out with errors: {flagged}")

    if not posts:
        await bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text="Независимый Портал: свежих материалов не найдено."
        )
        return

    await bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            f"📋 <b>Дайджест {datetime.now(timezone.utc).strftime('%d.%m.%Y')}</b>\n"
            f"RSS источников: {len(RSS_FEEDS)}\n"
            f"Новостей: {len(rss_items)}\n"
            f"Научных статей: {len(science_items)}\n"
            f"Заметок: {len(posts)}\n\n"
            f"Перешли нужные в канал вручную."
        ),
        parse_mode="HTML",
    )

    for post in posts:
        text = format_post(post)
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

        # Второе сообщение — короткая версия для Threads
        if post.get("threads"):
            try:
                await bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=f"🧵 <b>Threads:</b>\n{post['threads']}",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                logger.warning(f"Threads send failed: {e}")

        if post.get("url"):
            seen.add(item_id(post["url"]))
        await asyncio.sleep(1)

    save_seen(seen)
    logger.info("Done.")


if __name__ == "__main__":
    asyncio.run(run_digest())
