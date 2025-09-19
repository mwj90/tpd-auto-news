#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Auto news drafter for The Policy Dispatch.

Inputs (from config.yaml at repo root):
  inoreader_json_url: <required> public Inoreader "view/json" URL
  hours:               how far back to look (e.g., 6)
  max_posts:           maximum drafts to create (e.g., 3)
  dry:                 if true, do not write files (default: false)

Outputs:
  - Jekyll draft files in drafts/YYYY-MM-DD-<slug>.md
  - state/seen.json keeps track of already processed item ids/urls

Dependencies (installed by workflow):
  PyYAML, python-dateutil, requests, beautifulsoup4, lxml,
  trafilatura, readability-lxml, markdownify, nltk (punkt)
"""

import json
import os
import re
import sys
import time
import math
import yaml
import html
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dtparse
from markdownify import markdownify as md
try:
    import trafilatura
except Exception:
    trafilatura = None

try:
    from readability import Document
except Exception:
    Document = None

# Try NLTK sentence tokenizer if available; fall back to regex.
try:
    import nltk
    from nltk.tokenize import sent_tokenize
    _HAVE_NLTK = True
except Exception:
    _HAVE_NLTK = False

ROOT = Path(__file__).resolve().parents[1]          # repo root
CFG_PATH = ROOT / "config.yaml"                     # your repo-level config.yaml
DRAFTS_DIR = ROOT / "drafts"
STATE_DIR = ROOT / "state"
SEEN_PATH = STATE_DIR / "seen.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)


# -----------------------------
# Helpers
# -----------------------------

def load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(str(path))
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_seen(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return set(data)
        if isinstance(data, dict) and "seen" in data:
            return set(data["seen"])
    except Exception:
        pass
    return set()


def save_seen(path: Path, ids: set) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")


def slugify(text: str, max_len: int = 80) -> str:
    text = html.unescape(text or "").lower().strip()
    text = re.sub(r"[’'“”]", "", text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    if len(text) > max_len:
        text = text[:max_len].rstrip("-")
    return text or "post"


def as_dt(value) -> datetime:
    """Parse timestamp (epoch or iso) into aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        dt = dtparse.parse(str(value))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def sentence_split(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    if _HAVE_NLTK:
        try:
            return sent_tokenize(text)
        except Exception:
            pass
    # fallback: rough splitter
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"\'])", text)
    return [p.strip() for p in parts if p.strip()]


def summarize_extract(content: str, target_words: int = 300) -> str:
    """Simple extractive summary: take first N good sentences up to ~target_words."""
    sents = sentence_split(content)
    if not sents:
        return ""
    out, count = [], 0
    for s in sents:
        w = len(s.split())
        if w < 5:
            continue
        out.append(s)
        count += w
        if count >= target_words:
            break
    # Tidy
    joined = " ".join(out)
    # ensure not too long
    words = joined.split()
    if len(words) > target_words + 60:
        joined = " ".join(words[:target_words + 60]) + "…"
    return joined


def choose_best_url(item: dict) -> str:
    # inoreader JSON usually has 'canonical' or 'alternate' arrays with href
    for key in ("canonical", "alternate"):
        arr = item.get(key)
        if isinstance(arr, list) and arr:
            href = arr[0].get("href")
            if href:
                return href
    # fallback: maybe 'originId' or 'id' is a URL
    for key in ("originId", "id", "url"):
        val = item.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None


def fetch_article(url: str) -> tuple[str, str]:
    """
    Return (title, clean_text) by trying trafilatura first, then readability+BS4.
    """
    headers = {
        "user-agent": "Mozilla/5.0 (compatible; TPD-Bot/1.0; +https://thepolicydispatch.com)"
    }
    try:
        resp = requests.get(url, timeout=25, headers=headers)
        resp.raise_for_status()
        html_text = resp.text
    except Exception as e:
        logging.warning("Fetch failed: %s (%s)", url, e)
        return None, ""

    # Try trafilatura
    if trafilatura:
        try:
            extracted = trafilatura.extract(html_text, include_comments=False, include_tables=False)
            if extracted:
                # Title attempt
                title = None
                soup = BeautifulSoup(html_text, "lxml")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
                return title, extracted.strip()
        except Exception:
            pass

    # Fallback readability
    if Document:
        try:
            doc = Document(html_text)
            title = (doc.title() or "").strip()
            content_html = doc.summary(html_partial=True)
            soup = BeautifulSoup(content_html, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            text = soup.get_text(separator=" ", strip=True)
            return title, text.strip()
        except Exception:
            pass

    # Last fallback: plain text
    try:
        soup = BeautifulSoup(html_text, "lxml")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        title = (soup.title.string.strip() if soup.title and soup.title.string else None)
        text = soup.get_text(separator=" ", strip=True)
        return title, text.strip()
    except Exception:
        return None, ""


def score_item(item: dict, now: datetime) -> float:
    """
    Very simple ranking: newer + a tiny bump for longer titles.
    """
    published = as_dt(item.get("published") or item.get("published_at") or item.get("updateTime")) or now
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    title = (item.get("title") or "").strip()
    title_len = len(title.split())
    # score: more recent => higher; title length 6-14 gets a bonus
    length_bonus = 0.5 if 6 <= title_len <= 14 else 0.0
    return (100.0 / (1.0 + age_hours)) + length_bonus


def front_matter(**kwargs) -> str:
    return (
        "---\n" +
        yaml.safe_dump(kwargs, sort_keys=False, allow_unicode=True) +
        "---\n\n"
    )


def write_draft(title: str, date: datetime, url: str, summary_md: str, tags=None, dry=False) -> Path | None:
    tags = tags or []
    date_local = date.astimezone(timezone.utc)  # keep UTC for consistency
    slug = slugify(title or "article")
    fname = f"{date_local.date()}-{slug}.md"
    path = DRAFTS_DIR / fname

    fm = front_matter(
        layout="post",
        title=title or "Untitled",
        date=date_local.isoformat(),
        draft=True,
        tags=tags,
        source=url,
        canonical_url=url,
    )
    body = fm + summary_md.strip() + "\n"
    if dry:
        logging.info("[dry] Would write: %s", path)
        return None
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    logging.info("Draft written: %s", path.name)
    return path


# -----------------------------
# Main
# -----------------------------

def main():
    cfg = load_yaml(CFG_PATH)

    feed_url = cfg.get("inoreader_json_url")
    if not feed_url:
        raise KeyError("Missing 'inoreader_json_url' in config.yaml")

    hours = int(cfg.get("hours", 6))
    max_posts = int(cfg.get("max_posts", 3))
    dry = bool(cfg.get("dry", False))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen(SEEN_PATH)

    logging.info("Fetching feed: %s", feed_url)
    r = requests.get(feed_url, timeout=30)
    r.raise_for_status()
    data = r.json()

    # Inoreader "view/json" -> items list
    entries = data.get("items", [])
    logging.info("Items in feed: %d", len(entries))

    # Keep items with acceptable time & not seen
    candidates = []
    for it in entries:
        pid = it.get("id") or it.get("originId") or choose_best_url(it)
        if not pid:
            # generate a stable id from title+time as last resort
            base = (it.get("title") or "") + str(it.get("published"))
            pid = hashlib.sha1(base.encode("utf-8")).hexdigest()
        if pid in seen:
            continue

        published = as_dt(it.get("published") or it.get("published_at"))
        if not published or published < cutoff:
            continue

        candidates.append((pid, it, published))

    logging.info("Candidates kept (not seen, within %dh): %d", hours, len(candidates))

    # Rank & trim
    ranked = sorted(
        candidates,
        key=lambda t: score_item(t[1], now),
        reverse=True,
    )[:max_posts]

    created = []
    for pid, item, published in ranked:
        url = choose_best_url(item)
        title = (item.get("title") or "").strip()

        if not url:
            logging.info("Skip (no URL): %s", title or pid)
            seen.add(pid)
            continue

        logging.info("Fetching article: %s", url)
        art_title, text = fetch_article(url)
        if not text or len(text.split()) < 120:
            logging.info("Skip (content too short): %s", url)
            seen.add(pid)
            continue

        # Prefer on-page title if reasonable
        if art_title and len(art_title.split()) >= 4:
            title = art_title

        # Summarize to ~300 words
        summary_txt = summarize_extract(text, target_words=300)
        if not summary_txt or len(summary_txt.split()) < 120:
            # fallback: first 120+ words of full text
            words = text.split()
            summary_txt = " ".join(words[:320])

        # Convert to Markdown (keep it simple)
        summary_md = md(summary_txt)

        # Add a short header sentence linking back
        header = f"*Summary of:* [{title}]({url})\n\n"
        summary_md = header + summary_md

        # tags: try origin/source title if present
        tags = []
        origin = item.get("origin", {})
        if isinstance(origin, dict):
            src = origin.get("title")
            if src:
                tags.append(slugify(src, 24))

        draft_path = write_draft(title, published, url, summary_md, tags=tags, dry=dry)
        if draft_path:
            created.append(draft_path.name)

        seen.add(pid)

    # Save seen state
    if not dry:
        save_seen(SEEN_PATH, seen)

    # Output a tiny JSON for the workflow to read if it wants
    result = {"created": created}
    print(json.dumps(result, ensure_ascii=False))
    logging.info("Done. Created %d draft(s).", len(created))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("Fatal error: %s", e)
        sys.exit(1)
