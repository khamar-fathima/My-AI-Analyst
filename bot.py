"""Daily AI Intelligence Brief -> Telegram.

Pipeline:
  1. Pull the last ~30h of items from RSS feeds + APIs (HN, Hugging Face, GitHub).
  2. Drop duplicates and anything already sent (seen.json).
  3. Ask Gemini to pick and summarise items into fixed sections, returning JSON
     that references items by ID. Links are attached by THIS code from the
     fetched data, so the model can never invent a URL.
  4. Render as Telegram HTML and send in chunks.

Env vars: GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
Optional: GITHUB_TOKEN, GEMINI_MODEL, LOOKBACK_HOURS, DRY_RUN=1 (print, don't send)
"""
import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

import feedparser
import requests

IST = ZoneInfo("Asia/Kolkata")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "30"))
MAX_ITEMS_TOTAL = 90          # items sent to the LLM (round-robin across sources)
SNIPPET_CHARS = 500           # per-item context given to the LLM
SEEN_FILE = Path(__file__).with_name("seen.json")
SEEN_KEEP_DAYS = 14
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
DRY_RUN = os.environ.get("DRY_RUN") == "1"
UA = "Mozilla/5.0 (compatible; AIDigestBot/1.0)"

# Edit this line to change who the digest is written for.
READER = "a student AI/ML engineer who wants to build and deploy real systems and land AI/ML internships"

# ---------------------------------------------------------------------------
# SOURCES  (name, feed url, category hint, max items per day)
# Hints: launch | research | voice | news | india | tools | community
# Feeds move or die over time; failures are logged and skipped, never fatal.
# ---------------------------------------------------------------------------
FEEDS = [
    # --- your original sources ---
    ("TLDR AI", "https://tldr.tech/api/rss/ai", "news", 4),
    ("Latent Space", "https://www.latent.space/feed", "voice", 3),
    ("Simon Willison", "https://simonwillison.net/atom/entries/", "voice", 5),
    ("Sebastian Raschka", "https://magazine.sebastianraschka.com/feed", "voice", 2),
    ("Chip Huyen", "https://huyenchip.com/feed.xml", "voice", 2),
    ("OpenAI News", "https://openai.com/news/rss.xml", "launch", 5),
    ("OpenAI Research", "https://openai.com/research/rss.xml", "research", 3),
    ("Google DeepMind", "https://deepmind.google/blog/rss.xml", "launch", 4),
    ("Anthropic", "https://www.anthropic.com/feed", "launch", 4),
    ("Anthropic (community feed)",
     "https://raw.githubusercontent.com/Olshansk/rss-feeds/main/feeds/feed_anthropic_news.xml", "launch", 4),
    ("Hugging Face Blog", "https://huggingface.co/blog/feed.xml", "tools", 4),
    ("NVIDIA AI", "https://blogs.nvidia.com/blog/category/deep-learning/feed/", "launch", 3),
    ("Meta AI", "https://ai.meta.com/blog/rss.xml", "launch", 3),
    ("arXiv cs.AI", "http://export.arxiv.org/rss/cs.AI", "research", 4),

    # --- labs and big-tech blogs ---
    ("Google AI", "https://blog.google/technology/ai/rss/", "launch", 4),
    ("Microsoft Research", "https://www.microsoft.com/en-us/research/feed/", "research", 3),
    ("AWS ML Blog", "https://aws.amazon.com/blogs/machine-learning/feed/", "tools", 3),

    # --- people and newsletters worth reading ---
    ("Import AI (Jack Clark)", "https://importai.substack.com/feed", "voice", 2),
    ("Interconnects (Nathan Lambert)", "https://www.interconnects.ai/feed", "voice", 2),
    ("One Useful Thing (Ethan Mollick)", "https://www.oneusefulthing.org/feed", "voice", 2),
    ("The Batch (Andrew Ng)", "https://www.deeplearning.ai/the-batch/feed/", "voice", 2),
    ("Eugene Yan", "https://eugeneyan.com/rss/", "voice", 2),
    ("Lilian Weng", "https://lilianweng.github.io/index.xml", "voice", 2),
    ("Andrej Karpathy", "https://karpathy.bearblog.dev/feed/", "voice", 2),
    ("Ben's Bites", "https://www.bensbites.com/feed", "news", 3),

    # --- news outlets ---
    ("TechCrunch AI", "https://techcrunch.com/category/artificial-intelligence/feed/", "news", 5),
    ("The Verge AI", "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml", "news", 4),
    ("VentureBeat AI", "https://venturebeat.com/category/ai/feed/", "news", 4),
    ("MIT Technology Review AI", "https://www.technologyreview.com/topic/artificial-intelligence/feed", "news", 3),
    ("Ars Technica AI", "https://arstechnica.com/ai/feed/", "news", 3),

    # --- India angle ---
    ("Analytics India Magazine", "https://analyticsindiamag.com/feed/", "india", 5),
    ("YourStory", "https://yourstory.com/feed", "india", 4),
    ("Inc42", "https://inc42.com/feed/", "india", 4),

    # --- communities (Reddit may block cloud IPs; it is skipped if so) ---
    ("r/LocalLLaMA", "https://www.reddit.com/r/LocalLLaMA/top/.rss?t=day", "community", 4),
    ("r/MachineLearning", "https://www.reddit.com/r/MachineLearning/top/.rss?t=day", "community", 3),
]

# key in LLM JSON, emoji, heading, max items
SECTIONS = [
    ("top_stories", "🔥", "TOP 5 — WHAT YOU NEED TO KNOW TODAY", 5),
    ("launches", "🚀", "NEW MODELS & LAUNCHES", 6),
    ("research", "🔬", "RESEARCH WORTH READING", 4),
    ("tools_repos", "🛠️", "OPEN SOURCE, TOOLS & GITHUB", 5),
    ("industry", "🏢", "INDUSTRY, FUNDING & POLICY", 4),
    ("india", "🇮🇳", "INDIA AI", 4),
    ("voices", "🎙️", "WHAT THE PEOPLE YOU FOLLOW ARE SAYING", 4),
    ("deep_read", "📖", "DEEP READ OF THE DAY", 1),
    ("learn_today", "📚", "ONE THING TO LEARN TODAY", 1),
    ("try_today", "💻", "ONE THING TO TRY TODAY", 1),
]

HINT_TITLES = {
    "launch": "🚀 Launches & lab posts", "research": "🔬 Research", "voice": "🎙️ Blogs & newsletters",
    "news": "📰 News", "india": "🇮🇳 India AI", "tools": "🛠️ Tools & open source",
    "community": "💬 Communities",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def clean_text(raw):
    raw = re.sub(r"<[^>]+>", " ", raw or "")
    return re.sub(r"\s+", " ", html.unescape(raw)).strip()


def norm_link(link):
    p = urlparse(link)
    q = [(k, v) for k, v in parse_qsl(p.query) if not k.startswith("utm_") and k not in ("ref", "source")]
    return urlunparse(p._replace(query=urlencode(q), fragment="")).rstrip("/").lower()


def make_item(source, hint, title, link, snippet="", published=None):
    return {
        "source": source, "hint": hint, "title": clean_text(title), "link": link,
        "snippet": clean_text(snippet)[:SNIPPET_CHARS], "published": published,
    }


def get(url, **kw):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20, **kw)
    r.raise_for_status()
    return r


def load_seen():
    try:
        return json.loads(SEEN_FILE.read_text())
    except Exception:
        return {}


def save_seen(seen):
    cutoff = datetime.now(timezone.utc) - timedelta(days=SEEN_KEEP_DAYS)
    kept = {k: v for k, v in seen.items() if datetime.fromisoformat(v) > cutoff}
    SEEN_FILE.write_text(json.dumps(kept, indent=0))


# ---------------------------------------------------------------------------
# fetchers
# ---------------------------------------------------------------------------
def fetch_feed(name, url, hint, cap, cutoff):
    feed = feedparser.parse(get(url).content)
    out = []
    for e in feed.entries:
        t = e.get("published_parsed") or e.get("updated_parsed")
        pub = datetime(*t[:6], tzinfo=timezone.utc) if t else None
        if pub and pub < cutoff:
            continue
        body = e.get("summary") or (e.get("content") or [{}])[0].get("value", "")
        if e.get("title") and e.get("link"):
            out.append(make_item(name, hint, e["title"], e["link"], body, pub))
    out.sort(key=lambda x: x["published"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return out[:cap]


def fetch_hf_papers(cutoff):
    data = get("https://huggingface.co/api/daily_papers", params={"limit": 20}).json()
    rows = []
    for d in data:
        p = d.get("paper", {})
        if p.get("id") and p.get("title"):
            rows.append((p.get("upvotes", 0), make_item(
                "Hugging Face Daily Papers", "research", p["title"],
                f"https://huggingface.co/papers/{p['id']}",
                f"{p.get('upvotes', 0)} upvotes. {p.get('summary', '')}")))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows[:6]]


def fetch_hf_models(cutoff):
    data = get("https://huggingface.co/api/models",
               params={"sort": "trendingScore", "limit": 8}).json()
    out = []
    for m in data:
        mid = m.get("id") or m.get("modelId")
        if mid:
            out.append(make_item(
                "Hugging Face Trending Models", "tools", f"Trending model: {mid}",
                f"https://huggingface.co/{mid}",
                f"Task: {m.get('pipeline_tag', 'n/a')}. Downloads: {m.get('downloads', 0)}. Likes: {m.get('likes', 0)}."))
    return out


def fetch_hacker_news(cutoff):
    since = int(cutoff.timestamp())
    hits = {}
    for q in ("AI", "LLM", "OpenAI", "Anthropic", "Gemini", "machine learning"):
        r = get("https://hn.algolia.com/api/v1/search", params={
            "query": q, "tags": "story", "hitsPerPage": 10,
            "numericFilters": f"created_at_i>{since},points>80"}).json()
        for h in r.get("hits", []):
            hits[h["objectID"]] = h
    top = sorted(hits.values(), key=lambda h: h.get("points", 0), reverse=True)[:6]
    return [make_item(
        "Hacker News", "community", h["title"],
        h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
        f"{h.get('points', 0)} points and {h.get('num_comments', 0)} comments on Hacker News.") for h in top]


def fetch_github(cutoff):
    since = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    headers = {"User-Agent": UA, "Accept": "application/vnd.github+json"}
    if os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
    repos = {}
    for topic in ("llm", "ai-agents", "machine-learning"):
        r = requests.get("https://api.github.com/search/repositories", headers=headers, timeout=20, params={
            "q": f"topic:{topic} created:>{since}", "sort": "stars", "order": "desc", "per_page": 6})
        r.raise_for_status()
        for repo in r.json().get("items", []):
            repos[repo["full_name"]] = repo
    top = sorted(repos.values(), key=lambda x: x["stargazers_count"], reverse=True)[:6]
    return [make_item(
        "GitHub (new & trending)", "tools", f"{r['full_name']}: {r.get('description') or 'no description'}",
        r["html_url"], f"{r['stargazers_count']} stars in under a week. Language: {r.get('language')}.") for r in top]


# ---------------------------------------------------------------------------
# collect
# ---------------------------------------------------------------------------
def collect_items():
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    jobs = [(n, lambda n=n, u=u, h=h, c=c: fetch_feed(n, u, h, c, cutoff)) for n, u, h, c in FEEDS]
    jobs += [
        ("Hugging Face Daily Papers", lambda: fetch_hf_papers(cutoff)),
        ("Hugging Face Trending Models", lambda: fetch_hf_models(cutoff)),
        ("Hacker News", lambda: fetch_hacker_news(cutoff)),
        ("GitHub", lambda: fetch_github(cutoff)),
    ]
    seen = load_seen()
    items, links = [], set()
    print(f"Fetching {len(jobs)} sources (last {LOOKBACK_HOURS}h)...")
    for order, (name, fn) in enumerate(jobs):
        try:
            got = fn()
        except Exception as e:
            print(f"  ! {name}: {type(e).__name__}: {e}")
            continue
        fresh = 0
        for rank, it in enumerate(got):
            key = norm_link(it["link"])
            if key in seen or key in links:
                continue
            links.add(key)
            it.update(order=order, rank=rank, key=key)
            items.append(it)
            fresh += 1
        print(f"  {name}: {fresh} new")
    # round-robin trim so no single source crowds out the rest
    items.sort(key=lambda x: (x["rank"], x["order"]))
    items = items[:MAX_ITEMS_TOTAL]
    items.sort(key=lambda x: (x["order"], x["rank"]))
    for i, it in enumerate(items, 1):
        it["id"] = i
    return items


# ---------------------------------------------------------------------------
# summarise with Gemini
# ---------------------------------------------------------------------------
def build_prompt(items, today):
    lines = [f"Today is {today}. Items from the last {LOOKBACK_HOURS} hours:\n"]
    for it in items:
        lines.append(f"[{it['id']}] source={it['source']} | hint={it['hint']}\n"
                     f"Title: {it['title']}\nSnippet: {it['snippet'] or '(none)'}\n")
    return "\n".join(lines)


def system_prompt():
    spec = "\n".join(f'- "{k}": up to {n} entries' for k, _, _, n in SECTIONS)
    return f"""You are a sharp AI/ML news analyst writing a daily brief for {READER}.

Use ONLY the numbered items provided. Never invent news, numbers, or links.
Return ONE JSON object with these keys; each value is a list of entries:
{spec}
plus "bigger_picture": a string of 3-4 sentences connecting today's items into one trend or takeaway.

Each entry: {{"id": <item number>, "headline": "<clear headline, max 100 chars>",
"summary": "<3-4 sentences, 60-90 words: what happened, the concrete details or numbers, and the context>",
"why_it_matters": "<one sentence on what it means for someone building AI systems>"}}

Rules:
- "id" must be an item number from the input. Pick the most important and useful items; skip fluff and duplicates.
- Do not repeat an item across sections, except learn_today / try_today / deep_read may reuse an item.
- If an item's snippet is thin, summarise only what its title and snippet support. Do not speculate.
- "learn_today": one concept worth studying, taught via an item. "try_today": one hands-on thing to build or run. For both, "summary" should say concretely what to do.
- "deep_read": the single best long-form item for slow reading.
- If nothing fits a section, return an empty list for it.
- Plain text only, no markdown or HTML. Valid JSON only."""


def summarise(items, today):
    from google import genai
    from google.genai import types
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    last = None
    for attempt in range(3):
        try:
            resp = client.models.generate_content(
                model=GEMINI_MODEL, contents=build_prompt(items, today),
                config=types.GenerateContentConfig(
                    system_instruction=system_prompt(), temperature=0.3,
                    response_mime_type="application/json"))
            text = re.sub(r"^```(?:json)?|```$", "", resp.text.strip(), flags=re.M).strip()
            data = json.loads(text)
            if isinstance(data, dict):
                return data
            raise ValueError("model did not return a JSON object")
        except Exception as e:
            last = e
            print(f"  Gemini attempt {attempt + 1} failed: {e}")
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"Gemini failed: {last}")


# ---------------------------------------------------------------------------
# render (Telegram HTML)
# ---------------------------------------------------------------------------
def esc(s):
    return html.escape(str(s or ""), quote=False)


def link_html(item):
    return f'🔗 <a href="{html.escape(item["link"], quote=True)}">{esc(item["source"])}</a>'


def render_digest(data, by_id, today, n_items, n_sources):
    pieces = [f"🤖 <b>DAILY AI INTELLIGENCE BRIEF</b>\n📅 {esc(today)}\n"
              f"<i>{n_items} fresh items from {n_sources} sources, curated for you</i>"]
    for key, emoji, title, cap in SECTIONS:
        entries = [e for e in (data.get(key) or []) if isinstance(e, dict)][:cap]
        header = f"{emoji} <b>{esc(title)}</b>\n━━━━━━━━━━━━━━"
        first = True
        for n, e in enumerate(entries, 1):
            try:
                item = by_id.get(int(e.get("id")))
            except (TypeError, ValueError):
                item = None
            headline = e.get("headline") or (item or {}).get("title") or "Untitled"
            parts = [f"<b>{n}. {esc(headline)}</b>"]
            if e.get("summary"):
                parts.append(esc(str(e["summary"])[:900]))
            if e.get("why_it_matters"):
                parts.append(f"💡 <i>{esc(str(e['why_it_matters'])[:300])}</i>")
            if item:
                parts.append(link_html(item))
            body = "\n".join(parts)
            pieces.append(f"{header}\n\n{body}" if first else body)
            first = False
    if data.get("bigger_picture"):
        pieces.append(f"🌎 <b>BIGGER PICTURE</b>\n━━━━━━━━━━━━━━\n\n{esc(str(data['bigger_picture'])[:1200])}")
    return pieces


def render_raw(items, today):
    """Fallback when the LLM is unavailable: grouped titles + links."""
    pieces = [f"🤖 <b>DAILY AI BRIEF (raw — summaries unavailable)</b>\n📅 {esc(today)}"]
    for hint, title in HINT_TITLES.items():
        group = [i for i in items if i["hint"] == hint]
        if not group:
            continue
        lines = [f"<b>{esc(title)}</b>\n━━━━━━━━━━━━━━"]
        for it in group:
            lines.append(f'• <a href="{html.escape(it["link"], quote=True)}">{esc(it["title"][:120])}</a> — {esc(it["source"])}')
        pieces.append("\n".join(lines))
    return pieces


def chunk(pieces, limit=3800):
    chunks, cur = [], ""
    for p in pieces:
        p = p[:limit]
        if cur and len(cur) + len(p) + 2 > limit:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# telegram
# ---------------------------------------------------------------------------
def to_plain(t):
    t = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2: \1", t)
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def send_telegram(chunks):
    token, chat_id = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i, text in enumerate(chunks, 1):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        for attempt in range(4):
            r = requests.post(url, json=payload, timeout=30)
            if r.status_code == 200:
                print(f"  sent {i}/{len(chunks)}")
                break
            if r.status_code == 429:
                time.sleep(r.json().get("parameters", {}).get("retry_after", 5) + 1)
            elif r.status_code == 400 and "parse" in r.text.lower():
                payload = {"chat_id": chat_id, "text": to_plain(text), "disable_web_page_preview": True}
            else:
                raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")
        else:
            raise RuntimeError(f"Could not send message {i}")
        time.sleep(1)


# ---------------------------------------------------------------------------
def main():
    now = datetime.now(IST)
    today = now.strftime("%A, %d %B %Y")
    items = collect_items()
    if not items:
        print("No new items. Skipping.")
        return
    by_id = {it["id"]: it for it in items}
    n_sources = len({it["source"] for it in items})

    try:
        print(f"Summarising {len(items)} items with {GEMINI_MODEL}...")
        pieces = render_digest(summarise(items, today), by_id, today, len(items), n_sources)
    except Exception as e:
        print(f"LLM step failed, sending raw digest: {e}")
        pieces = render_raw(items, today)

    chunks = chunk(pieces)
    if DRY_RUN:
        print("\n\n=====\n\n".join(chunks))
        return
    send_telegram(chunks)

    seen = load_seen()
    stamp = datetime.now(timezone.utc).isoformat()
    for it in items:
        seen[it["key"]] = stamp
    save_seen(seen)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"FATAL: {e}")
        sys.exit(1)