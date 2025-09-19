#!/usr/bin/env python3
import os
import json
import re
from pathlib import Path
from datetime import datetime, timezone
import requests
from bs4 import BeautifulSoup

# -------- CONFIG (env or defaults) ----------
ROOT = Path(__file__).resolve().parents[1]  # repo root
DRAFTS_DIR = ROOT / "drafts"                 # where we drop drafts
STATE_DIR = ROOT / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)
SEEN_FILE = STATE_DIR / "seen.json"

# Required: your Inoreader JSON URL
FEED_URL = os.getenv(
    "INOREADER_JSON_URL",
    "https://www.inoreader.com/stream/user/1006265339/tag/Google%20News%20Feeds/view/json?n=1000"
)

# How many drafts to create per run
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))

# If true, only log and do not write files
DRY = os.getenv("DRY", "false").lower() == "true"

DEBUG = os.getenv("DEBUG", "true").lower() == "true"

# -------------------------------------------

def log(*args):
    if DEBUG:
        print(*args)

def load_seen():
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()

def save_seen(seen_set):
    try:
        SEEN_FILE.write_text(json.dumps(sorted(list(seen_set)), ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log("WARN: could not save seen.json:", e)

def slugify(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^\w\s-]", "", s, flags=re.UNICODE)
    s = re.sub(r"[\s_-]+", "-", s).strip("-")
    return s[:80] or "post"

def html_to_text(html: str) -> str:
    soup = BeautifulSoup(html, "lxml")
    # remove script/style
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n", strip=True)
    # compact blank lines a bit
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text

def pick_url(entry: dict) -> str:
    """
    Inoreader JSON sometimes has:
      - "url" OR
      - "origin_id" OR
      - "link" OR inside "origin/htmlUrl"
    We try common options.
    """
    for key in ("url", "origin_id", "link"):
        if key in entry and entry[key]:
            return entry[key]
    # try nested
    origin = entry.get("origin") or {}
    if isinstance(origin, dict) and origin.get("htmlUrl"):
        return origin["htmlUrl"]
    return ""

def parse_published(entry: dict) -> datetime:
    """
    Inoreader JSON example field:
      "date_published": "2025-09-19T17:32:08+00:00"
    """
    raw = entry.get("date_published") or entry.get("published") or ""
    try:
        # Python 3.11 handles fromisoformat with offset
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt
    except Exception:
        return datetime.now(timezone.utc)

def write_draft(title, url, published_dt, body, author="Automated"):
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    date_str = published_dt.astimezone(timezone.utc).strftime("%Y-%m-%d")
    slug = slugify(title) or slugify(url)
    filename = f"{date_str}-{slug}.md"
    path = DRAFTS_DIR / filename

    fm = {
        "layout": "post",
        "title": title,
        "date": published_dt.astimezone(timezone.utc).isoformat(),
        "author": author,
        "source": url,
    }

    lines = ["---"]
    # escape double quotes safely
    for k, v in fm.items():
        val = str(v).replace('"', '\\"')
        lines.append(f'{k}: "{val}"')
    lines.append("---")
    lines.append("")
    lines.append(body.strip())
    lines.append("")

    if DRY:
        log(f"[DRY] Would write: {path.name}")
        return path

    path.write_text("\n".join(lines), encoding="utf-8")
    log("Wrote draft:", path)
    return path

def main():
    print(f"Fetching: {FEED_URL}")
    seen = load_seen()

    try:
        r = requests.get(FEED_URL, timeout=30)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print("ERROR: could not fetch/parse feed JSON:", e)
        return

    items = data.get("items") or data.get("entries") or []
    print("Items in feed:", len(items))

    created = 0
    new_seen = set(seen)

    for entry in items:
        if created >= MAX_POSTS:
            break

        title = (entry.get("title") or "").strip()
        url = pick_url(entry)
        body_html = entry.get("content_html") or entry.get("summary") or entry.get("content") or ""
        published_dt = parse_published(entry)

        # a stable ID to dedupe: prefer entry's 'id', else url
        entry_id = entry.get("id") or url or title
        if not entry_id:
            log("SKIP: entry has no usable id or url")
            continue

        if entry_id in seen:
            log("SKIP (seen):", title)
            continue

        # Build a simple body: title + source + extracted text from content_html
        text = html_to_text(body_html) if body_html else ""
        if not text:
            text = f"(No body available. Source: {url})"

        body = f"{text}\n\n—\n*Source:* {url}"

        try:
            write_draft(title or "Untitled", url, published_dt, body)
            created += 1
            new_seen.add(entry_id)
        except Exception as e:
            log("WARN: failed to write draft for:", title, e)

    print(json.dumps({"created": created}, indent=2))
    save_seen(new_seen)

if __name__ == "__main__":
    main()
