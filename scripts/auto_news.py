#!/usr/bin/env python3
"""
Auto-news draft generator for The Policy Dispatch
"""

import os, re, json, time, requests, yaml, hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
from markdownify import markdownify as md
from rake_nltk import Rake
import nltk
# Ensure required datasets are available
nltk.download("punkt")
nltk.download("punkt_tab")
nltk.download("stopwords")
nltk.download("punkt", quiet=True)

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = yaml.safe_load(open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8"))

SEEN_PATH = os.path.join(ROOT, "..", "state", "seen.json")
DRAFTS_DIR = os.path.join(ROOT, "..", "drafts")

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------

def as_dt(val):
    try:
        if not val: return None
        return dtparse.parse(str(val))
    except Exception:
        return None

def get_item_time(item: dict) -> datetime | None:
    for key in ("date_published", "published", "updated", "crawled", "timestampUsec", "crawlTimeMsec"):
        if key in item and item[key]:
            return as_dt(item[key])
    return None

def extract_href_from_content_html(item: dict) -> str | None:
    html_str = item.get("content_html")
    if not isinstance(html_str, str) or not html_str.strip():
        return None
    try:
        soup = BeautifulSoup(html_str, "lxml")
        a = soup.find("a", href=True)
        if a and a["href"]:
            return a["href"]
    except Exception:
        pass
    return None

def choose_best_url(item: dict) -> str | None:
    # 1) JSON Feed-like <a href> inside content_html
    href = extract_href_from_content_html(item)
    if href:
        return href

    # 2) canonical/alternate arrays
    for key in ("canonical", "alternate"):
        arr = item.get(key)
        if isinstance(arr, list) and arr:
            href = arr[0].get("href")
            if href:
                return href

    # 3) url (Inoreader article page)
    url = item.get("url")
    if isinstance(url, str) and url.startswith("http"):
        return url

    # 4) originId/id
    for key in ("originId", "id"):
        val = item.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None

def clean_text(html, min_words=40):
    if not html: return ""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup(["script","style","noscript"]):
        tag.decompose()
    txt = soup.get_text(" ", strip=True)
    return txt if len(txt.split()) >= min_words else ""

# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------

def fetch_feed(url: str) -> dict:
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    return r.json()

def extract_article(url: str) -> str:
    """Try to fetch and extract article text from target page"""
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent":"Mozilla/5.0"})
        r.raise_for_status()
    except Exception:
        return ""
    return clean_text(r.text, min_words=CFG.get("min_words",40))

def summarize(text: str) -> str:
    rake = Rake()
    rake.extract_keywords_from_text(text)
    kws = rake.get_ranked_phrases()[:5]
    paras = text.split(". ")
    return " ".join(paras[:2]) + ("\n\nKeywords: " + ", ".join(kws) if kws else "")

def ensure_dirs():
    os.makedirs(DRAFTS_DIR, exist_ok=True)
    os.makedirs(os.path.dirname(SEEN_PATH), exist_ok=True)
    if not os.path.exists(SEEN_PATH):
        json.dump([], open(SEEN_PATH,"w"))

def main():
    ensure_dirs()
    seen = json.load(open(SEEN_PATH)) if os.path.exists(SEEN_PATH) else []

    feed_url = CFG["inoreader_json_url"]
    hours = int(CFG.get("hours", 6))
    max_posts = int(CFG.get("max_posts", 3))
    min_words = int(CFG.get("min_words", 40))
    verbose = bool(CFG.get("verbose", True))

    data = fetch_feed(feed_url)
    items = data.get("items", [])
    print(f"Fetched {len(items)} items")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    created = []

    for it in items:
        t = get_item_time(it)
        if not t or t < cutoff: continue

        url = choose_best_url(it)
        if not url: continue
        uid = hashlib.md5(url.encode()).hexdigest()
        if uid in seen: continue

        title = it.get("title","(no title)").strip()

        text = extract_article(url)

        # fallback to feed summary/content_html
        if len(text.split()) < min_words:
            fb_html = None
            if isinstance(it.get("content"), dict):
                fb_html = it["content"].get("content")
            if not fb_html and isinstance(it.get("summary"), dict):
                fb_html = it["summary"].get("content")
            if not fb_html and isinstance(it.get("content_html"), str):
                fb_html = it["content_html"]
            if fb_html:
                text = clean_text(fb_html, min_words=min_words)

        if len(text.split()) < min_words:
            if verbose: print(f"SKIP short: {title}")
            continue

        summary = summarize(text)
        body = f"# {title}\n\nSource: {url}\n\n{summary}\n"

        fname = os.path.join(DRAFTS_DIR, f"{uid}.md")
        with open(fname,"w",encoding="utf-8") as f:
            f.write(body)

        seen.append(uid)
        created.append(fname)
        if verbose: print(f"Drafted {fname}")

        if len(created) >= max_posts:
            break

    json.dump(seen, open(SEEN_PATH,"w"))
    print(json.dumps({"created": created}, indent=2))

if __name__ == "__main__":
    main()
