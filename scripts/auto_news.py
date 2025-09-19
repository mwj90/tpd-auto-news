#!/usr/bin/env python3
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
import yaml
from dateutil import parser as dtparse

# Optional but present in your env
import nltk
from nltk.tokenize import sent_tokenize

try:
    import trafilatura
except Exception:
    trafilatura = None


ROOT = Path(__file__).resolve().parents[1]  # repo root
CFG_FILE = ROOT / "config.yaml"
SEEN_FILE = ROOT / "state" / "seen.json"
DRAFTS_DIR = ROOT / "drafts"


def log(msg, *, debug=False, force=False):
    if force or debug:
        print(msg, flush=True)


def load_cfg():
    with open(CFG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen):
    SEEN_FILE.write_text(json.dumps(sorted(seen)), encoding="utf-8")


def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s-]+", "", s)
    s = re.sub(r"\s+", "-", s).strip("-")
    return s[:80] or "post"


def fetch_inoreader_items(url: str, debug=False):
    headers = {"User-Agent": "tpd-auto-news/1.0"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json()
    items = data.get("items", [])
    log(f"DEBUG: items fetched = {len(items)}", debug=debug)
    return items


def pick_id(item):
    # Prefer stable ID; fall back to URL
    return item.get("id") or item.get("url") or item.get("origin_id") or item.get("title")


def pick_url(item):
    return item.get("url") or item.get("homepage_url") or item.get("origin_id")


def pick_title(item):
    return (item.get("title") or "").strip()


def pick_when(item):
    # Inoreader JSON uses ISO8601 in "date_published"
    ts = item.get("date_published") or item.get("published")
    if not ts:
        return None
    try:
        return dtparse.parse(ts)
    except Exception:
        return None


def extract_text(url, html_fallback=None, debug=False):
    text = None
    if trafilatura:
        try:
            downloaded = trafilatura.fetch_url(url, timeout=20)
            if downloaded:
                text = trafilatura.extract(downloaded, include_comments=False, include_tables=False)
        except Exception as e:
            log(f"DEBUG: trafilatura failed: {e}", debug=debug)
    if not text and html_fallback:
        # Make a very light fallback from content_html: strip tags crudely
        text = re.sub(r"<[^>]+>", " ", html_fallback)
        text = re.sub(r"\s+", " ", text).strip()
    return text


def summarize_to_target(text, target_words=300):
    # Very naive: first N sentences to hit ~target words
    if not text:
        return ""
    sentences = sent_tokenize(text)
    out = []
    count = 0
    for s in sentences:
        w = len(s.split())
        if count + w > target_words and out:
            break
        out.append(s)
        count += w
        if count >= target_words:
            break
    # If extremely short, just return the text up to ~target
    if not out:
        return " ".join(text.split()[:target_words])
    return " ".join(out)


def write_draft(title, url, published_dt, body, author="Automated", base_path=DRAFTS_DIR):
    date_str = published_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    slug = slugify(title) or slugify(url)
    filename = f"{date_str}-{slug}.md"
    path = base_path / filename

    fm = {
        "layout": "post",
        "title": title,
        "date": published_dt.astimezone(timezone.utc).isoformat(),
        "author": author,
        "source": url,
    }

    # Front matter + body
    lines = ["---"]
    for k, v in fm.items():
        lines.append(f"{k}: \"{str(v).replace('\"','\\\"')}\"")
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--max-posts", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    cfg = load_cfg()
    feed_url = cfg.get("inoreader_json_url")
    if not feed_url:
        print("ERROR: `inoreader_json_url` missing in config.yaml", file=sys.stderr)
        sys.exit(1)

    author = cfg.get("author", "Automated News")
    window = timedelta(hours=args.hours)
    now = datetime.now(timezone.utc)

    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)

    seen = load_seen()
    items = fetch_inoreader_items(feed_url, debug=args.debug)

    kept = []
    for idx, it in enumerate(items, 1):
        iid = pick_id(it)
        title = pick_title(it)
        url = pick_url(it)
        published = pick_when(it)
        if args.debug:
            log(f"DEBUG[{idx}]: title={title!r}", debug=True)
            log(f"DEBUG[{idx}]: id={iid}", debug=True)
            log(f"DEBUG[{idx}]: url={url}", debug=True)
            log(f"DEBUG[{idx}]: published={published}", debug=True)

        if not iid or not title or not url or not published:
            log(f"DROP: missing essentials (id/title/url/date)", debug=args.debug)
            continue

        # Freshness
        if now - published > window:
            if args.debug:
                age = now - published
                log(f"DROP: too old ({age})", debug=True)
            continue

        # Seen?
        if iid in seen:
            log("DROP: already seen", debug=args.debug)
            continue

        kept.append(it)

        if len(kept) >= args.max_posts:
            break

    print(f"Candidates kept: {len(kept)}")
    created = []

    for it in kept:
        iid = pick_id(it)
        url = pick_url(it)
        title = pick_title(it)
        published = pick_when(it)
        html_fallback = it.get("content_html")

        article_text = extract_text(url, html_fallback=html_fallback, debug=args.debug)
        if not article_text or len(article_text.split()) < 120:
            # Use title + minimal stub if extraction is bad
            article_text = f"{title}\n\n(Quick note) Source: {url}"

        body = summarize_to_target(article_text, target_words=320)

        if args.dry_run:
            print(f"[DRY] Would write: {title} -> {url}")
        else:
            path = write_draft(title, url, published, body, author=author)
            created.append(str(path.relative_to(ROOT)))
            seen.add(iid)

    if not args.dry_run and created:
        save_seen(seen)

    # Emit summary for workflow logs
    print(json.dumps({"created": created}, indent=2))


if __name__ == "__main__":
    # Ensure punkt is present (the workflow downloads it, but this is a guard)
    try:
        nltk.data.find("tokenizers/punkt")
    except LookupError:
        try:
            nltk.download("punkt", quiet=True)
        except Exception:
            pass
    main()
