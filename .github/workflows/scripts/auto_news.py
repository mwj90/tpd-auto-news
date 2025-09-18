#!/usr/bin/env python3
import os, re, json, time, hashlib, textwrap, pathlib
from datetime import datetime, timezone, timedelta
import requests, feedparser, yaml
from bs4 import BeautifulSoup as BS
from readability import Document
from markdownify import markdownify as md
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lsa import LsaSummarizer

ROOT = pathlib.Path(__file__).resolve().parents[1]
POSTS = ROOT / "_posts"          # final publish destination (after you merge)
DRAFTS = ROOT / "drafts"         # where we stage drafts inside the PR
STATE = ROOT / "state" / "seen.json"
CFG = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

INOREADER = CFG["inoreader_json"]
HOURS = int(CFG.get("hours_window", 6))
MAX_ART = int(CFG.get("max_articles", 3))
MIN_TLEN = int(CFG.get("min_title_len", 40))
TARGET_WORDS = int(CFG.get("target_words", 300))
SITE_URL = CFG.get("site_url", "")
AUTHOR = CFG.get("author", "Automated")
CATEGORY = CFG.get("category", "automated")
TAGS = CFG.get("tags", [])

UA = {"User-Agent": "TPD-AutoNews/1.0 (+https://thepolicydispatch.com)"}

POSTS.mkdir(exist_ok=True); DRAFTS.mkdir(exist_ok=True); STATE.parent.mkdir(exist_ok=True)
if not STATE.exists(): STATE.write_text("[]", encoding="utf-8")

def now_utc(): return datetime.now(timezone.utc)

def slugify(s: str) -> str:
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    return re.sub(r"[\s_-]+", "-", s)[:90] or hashlib.md5(s.encode()).hexdigest()[:12]

def minutes_ago(ts): return (now_utc() - ts).total_seconds() / 60.0

def parse_time(entry):
    # feedparser structures
    if entry.get("published_parsed"):
        return datetime.fromtimestamp(time.mktime(entry["published_parsed"]), tz=timezone.utc)
    if entry.get("updated_parsed"):
        return datetime.fromtimestamp(time.mktime(entry["updated_parsed"]), tz=timezone.utc)
    return now_utc()

def clean_html_to_text(html):
    try:
        doc = Document(html)
        html = doc.summary()
    except Exception:
        pass
    soup = BS(html or "", "lxml")
    # drop scripts/styles/navs
    for tag in soup(["script","style","noscript"]): tag.decompose()
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()

def summarize_text(text, target_words=300):
    if not text: return ""
    # LSA summarizer by sentences; fallback to clipping if short
    parser = PlaintextParser.from_string(text, Tokenizer("english"))
    summ = LsaSummarizer()
    # aim for ~12 sentences ~= 250–350w depending on sentence length
    sentences = list(summ(parser.document, 12))
    merged = " ".join(str(s) for s in sentences) or text
    # length control
    words = merged.split()
    if len(words) > target_words + 60:
        merged = " ".join(words[:target_words + 40])
    elif len(words) < target_words - 80 and len(text.split()) > target_words:
        merged = " ".join(text.split()[:target_words])
    return merged

def fetch(url, raw=False):
    r = requests.get(url, headers=UA, timeout=20)
    r.raise_for_status()
    return r.content if raw else r.text

def bing_news_sources(title, limit=3):
    # free RSS query against Bing News
    q = requests.utils.quote(title)
    rss_url = f"https://www.bing.com/news/search?q={q}&format=rss"
    try:
        feed = feedparser.parse(fetch(rss_url, raw=True))
        links = []
        for e in feed.entries[: limit * 2]:
            link = e.get("link"); 
            if link and "bing.com" not in link and "microsoft.com" not in link:
                links.append(link)
            if len(links) >= limit: break
        return links
    except Exception:
        return []

def compose_article(title, primary_link, sources_texts):
    # join all sources into one doc, then summarize
    joined = " ".join(sources_texts)
    body = summarize_text(joined, TARGET_WORDS)
    # light polish
    body = body.strip()
    attributions = ""
    if primary_link:
        attributions += f"\n\n**Primary source:** {primary_link}\n"
    return body + attributions

def write_markdown(title, dt, body, primary_link, extra_links):
    date_str = dt.strftime("%Y-%m-%d %H:%M:%S %z")
    slug = slugify(title)
    fname = f"{dt.strftime('%Y-%m-%d')}-{slug}.md"
    path = DRAFTS / fname  # draft into PR
    fm = {
        "layout": "post",
        "title": title,
        "date": date_str,
        "author": AUTHOR,
        "categories": [CATEGORY],
        "tags": TAGS,
        "original_link": primary_link,
        "sources": extra_links,
    }
    front = "---\n" + "\n".join(
        f'{k}: {json.dumps(v, ensure_ascii=False)}' for k,v in fm.items()
    ) + "\n---\n\n"
    # teaser for feed previews
    body_md = textwrap.dedent(f"""{body}

---
*Editor note:* This article was auto-drafted from curated sources in the last {HOURS} hours and awaits manual approval.
""").strip() + "\n"
    path.write_text(front + body_md, encoding="utf-8")
    return path

def main():
    seen = set(json.loads(STATE.read_text(encoding="utf-8")))
    print("Fetching Inoreader JSON…")
    data = requests.get(INOREADER, headers=UA, timeout=30).json()
    entries = data.get("items") or data

    cutoff = now_utc() - timedelta(hours=HOURS)
    candidates = []
    for e in entries:
        dt = parse_time(e)
        if dt < cutoff: continue
        title = e.get("title","").strip()
        if len(title) < MIN_TLEN: continue
        link = e.get("canonical",[{}])[0].get("href") or e.get("link") or ""
        uid = e.get("id") or link or title
        if uid in seen: continue
        # simple score: newer + longer title
        score = (dt - cutoff).total_seconds() + len(title)*3
        candidates.append((score, dt, title, link, uid))

    candidates.sort(reverse=True)
    created = []

    for _, dt, title, primary_link, uid in candidates[: MAX_ART * 3]:
        print("Processing:", title)
        # web search for corroboration
        links = [primary_link] if primary_link else []
        links += [u for u in bing_news_sources(title, limit=3) if u not in links]

        texts = []
        for url in links[:3]:
            try:
                html = fetch(url)
                text = clean_html_to_text(html)
                if len(text.split()) > 120:
                    texts.append(text)
            except Exception as ex:
                print("  ! source failed:", url, ex)

        if not texts:
            # fallback to Inoreader summary if present
            summary_html = (e.get("summary") if 'e' in locals() else "") or ""
            texts = [clean_html_to_text(summary_html)] if summary_html else []

        if not texts: 
            print("  ! no usable text; skipping")
            seen.add(uid); 
            continue

        body = compose_article(title, primary_link, texts)
        path = write_markdown(title, dt, body, primary_link, links[1:])
        created.append(str(path))
        seen.add(uid)
        if len(created) >= MAX_ART: break

    STATE.write_text(json.dumps(sorted(list(seen))[-2000:]), encoding="utf-8")
    if created:
        print("Drafts created:")
        for p in created: print(" -", p)
    else:
        print("No drafts created.")

if __name__ == "__main__":
    main()
