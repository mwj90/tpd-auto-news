#!/usr/bin/env python3
import os
import re
import json
import requests
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from dateutil.parser import parse as dtparse

PUBLISH_NOW = os.getenv("PUBLISH_NOW", "false").lower() == "true"
OUTPUT_DIR = "_posts/automated-news" if PUBLISH_NOW else "drafts"
os.makedirs(OUTPUT_DIR, exist_ok=True)   # <— add this once near the top

# -------------------------------------------------------------------
# Config
# -------------------------------------------------------------------
FEED_URL = os.getenv("INOREADER_JSON_URL", "").strip()
HOURS = int(os.getenv("HOURS", "6"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))
PUBLISH_NOW = os.getenv("PUBLISH_NOW", "false").lower() == "true"
LOCAL_TZ = ZoneInfo("Asia/Dubai")  # GMT+4

OUTPUT_DIR = "_posts/automated-news" if PUBLISH_NOW else "drafts"

# -------------------------------------------------------------------
# Helpers
# -------------------------------------------------------------------
def parse_item_dt(it: dict):
    """Return UTC datetime from known fields or None."""
    for k in ("date_published", "published", "updated"):
        v = it.get(k)
        if v:
            try:
                return dtparse(v).astimezone(timezone.utc)
            except Exception:
                pass
    return None


def strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def clean_title(title: str) -> str:
    """Remove quotes that break YAML front matter."""
    return (title or "Untitled").replace('"', "'").strip()


def pretty_reason(keep: bool, title: str, reason: str):
    print(f"[{'KEEP' if keep else 'DROP'}] {title[:100]} — {reason}")


def write_post(item, pub_local: datetime, body: str):
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    title = clean_title(item.get("title") or item.get("summary") or "Untitled")
    slug = re.sub(r"[^a-z0-9\-]+", "-", title.lower()).strip("-")
    date_prefix = pub_local.strftime("%Y-%m-%d")
    filename = f"{date_prefix}-{slug[:50]}.md"
    path = os.path.join(OUTPUT_DIR, filename)

    lines = []
    lines.append("---")
    lines.append(f'title: "{title}"')
    lines.append(f"date: {pub_local.strftime('%Y-%m-%d %H:%M:%S %z')}")
    lines.append("---")
    lines.append("")
    lines.append(body.strip())

    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"Wrote draft: {path}")


# -------------------------------------------------------------------
# Main
# -------------------------------------------------------------------
def main():
    if not FEED_URL:
        raise SystemExit("❌ No INOREADER_JSON_URL set in env/config.")

    print(f"Fetching: {FEED_URL}")
    r = requests.get(FEED_URL)
    r.raise_for_status()
    feed = r.json()

    items = feed.get("items", [])
    print(f"Items in feed: {len(items)}")

    cutoff_utc = datetime.now(timezone.utc) - timedelta(hours=HOURS)
    print(f"Cutoff (UTC): {cutoff_utc.isoformat()}")

    kept = []
    for it in items:
        title = it.get("title", "(no title)")
        pub_utc = parse_item_dt(it)
        if not pub_utc:
            pretty_reason(False, title, "no parseable date")
            continue

        if pub_utc < cutoff_utc:
            pretty_reason(False, title, f"too old: {pub_utc.isoformat()}")
            continue

        content = (
            it.get("content_html")
            or it.get("content")
            or it.get("summary")
            or ""
        )
        text = strip_html(content)
        words = len(re.findall(r"\w+", text))
        if words < 30:
            pretty_reason(False, title, f"too short ({words} words)")
            continue

        pub_local = pub_utc.astimezone(LOCAL_TZ)
        kept.append((it, pub_local, text))
        pretty_reason(True, title, f"pub={pub_local.isoformat()} words={words}")

        if len(kept) >= MAX_POSTS:
            break

    print(f"Candidates kept: {len(kept)}")

    created = []
    for it, pub_local, text in kept:
        write_post(it, pub_local, text)
        created.append(it.get("id", it.get("url", "unknown")))

    print(json.dumps({"created": created}, indent=2))


if __name__ == "__main__":
    main()
