#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os
import re
import sys
import html
import yaml
import time
import math
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

try:
    import nltk
    from nltk.tokenize import sent_tokenize
    _HAVE_NLTK = True
except Exception:
    _HAVE_NLTK = False

ROOT = Path(__file__).resolve().parents[1]
CFG_PATH = ROOT / "config.yaml"
DRAFTS_DIR = ROOT / "drafts"
STATE_DIR = ROOT / "state"
SEEN_PATH = STATE_DIR / "seen.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ---------------- helpers ----------------

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
    return (text[:max_len].rstrip("-") or "post")

def sentence_split(text: str) -> list:
    text = (text or "").strip()
    if not text:
        return []
    if _HAVE_NLTK:
        try:
            return sent_tokenize(text)
        except Exception:
            pass
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\"\']))", text)
    return [p.strip() for p in parts if p.strip()]

def summarize_extract(content: str, target_words: int = 300) -> str:
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
    joined = " ".join(out)
    words = joined.split()
    if len(words) > target_words + 60:
        joined = " ".join(words[:target_words + 60]) + "…"
    return joined

def as_dt(value) -> datetime | None:
    """
    Parse a bunch of possible timestamp shapes to aware UTC datetime.
    Supports:
      - ISO strings
      - seconds epoch
      - micro/milli epoch (string or int) in keys: timestampUsec, crawlTimeMsec, crawled
    """
    if value is None:
        return None
    # micro/milli packed in strings?
    sval = str(value)
    try:
        # microseconds in Google Reader style
        if re.fullmatch(r"\d{15,19}", sval):
            # decide us vs ms by magnitude (rough)
            iv = int(sval)
            if iv > 1_000_000_000_000:  # >= 2001 in ms/us
                # assume usec if looks huge
                secs = iv / 1_000_000.0
            else:
                secs = iv / 1_000.0
            return datetime.fromtimestamp(secs, tz=timezone.utc)
        # plain seconds
        if re.fullmatch(r"\d{9,12}", sval):
            return datetime.fromtimestamp(int(sval), tz=timezone.utc)
    except Exception:
        pass
    # already number?
    if isinstance(value, (int, float)):
        # choose seconds if sane; else assume ms
        if value > 10_000_000_000:
            value = value / 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc)
    # ISO-ish
    try:
        dt = dtparse.parse(sval)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def get_item_time(item: dict) -> datetime | None:
    for key in ("published", "updated", "crawled", "timestampUsec", "crawlTimeMsec"):
        if key in item and item[key] is not None:
            dt = as_dt(item[key])
            if dt:
                return dt
    return None

def choose_best_url(item: dict) -> str | None:
    for key in ("canonical", "alternate"):
        arr = item.get(key)
        if isinstance(arr, list) and arr:
            href = arr[0].get("href")
            if href:
                return href
    for key in ("originId", "id", "url"):
        val = item.get(key)
        if isinstance(val, str) and val.startswith("http"):
            return val
    return None

def fetch_html(url: str) -> str | None:
    headers = {"user-agent": "Mozilla/5.0 (compatible; TPD-Bot/1.0; +https://thepolicydispatch.com)"}
    try:
        r = requests.get(url, timeout=25, headers=headers, allow_redirects=True)
        r.raise_for_status()
        return r.text
    except Exception as e:
        logging.warning("Fetch failed: %s (%s)", url, e)
        return None

def extract_content(html_text: str) -> tuple[str | None, str]:
    # trafilatura first
    if html_text and trafilatura:
        try:
            extracted = trafilatura.extract(html_text, include_comments=False, include_tables=False)
            if extracted:
                title = None
                soup = BeautifulSoup(html_text, "lxml")
                if soup.title and soup.title.string:
                    title = soup.title.string.strip()
                return title, extracted.strip()
        except Exception:
            pass
    # readability
    if html_text and Document:
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
    # plain soup
    if html_text:
        try:
            soup = BeautifulSoup(html_text, "lxml")
            for tag in soup(["script", "style", "noscript"]):
                tag.decompose()
            title = (soup.title.string.strip() if soup.title and soup.title.string else None)
            text = soup.get_text(separator=" ", strip=True)
            return title, text.strip()
        except Exception:
            pass
    return None, ""

def score_item(item: dict, now: datetime) -> float:
    published = get_item_time(item) or now
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    title = (item.get("title") or "").strip()
    title_len = len(title.split())
    length_bonus = 0.5 if 6 <= title_len <= 14 else 0.0
    return (100.0 / (1.0 + age_hours)) + length_bonus

def front_matter(**kwargs) -> str:
    return "---\n" + yaml.safe_dump(kwargs, sort_keys=False, allow_unicode=True) + "---\n\n"

def write_draft(title: str, date: datetime, url: str, summary_md: str, tags=None, dry=False) -> Path | None:
    tags = tags or []
    date_local = date.astimezone(timezone.utc)
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

# -------------- main ----------------

def main():
    cfg = load_yaml(CFG_PATH)
    feed_url = cfg.get("inoreader_json_url")
    if not feed_url:
        raise KeyError("Missing 'inoreader_json_url' in config.yaml")

    hours = int(cfg.get("hours", 6))
    max_posts = int(cfg.get("max_posts", 3))
    min_words = int(cfg.get("min_words", 80))  # NEW
    verbose = bool(cfg.get("verbose", False))  # NEW
    dry = bool(cfg.get("dry", False))

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen = load_seen(SEEN_PATH)

    logging.info("Fetching feed: %s", feed_url)
    data = requests.get(feed_url, timeout=30).json()
    items = data.get("items", [])
    logging.info("Items in feed: %d", len(items))

    # Track skip reasons for visibility
    skip_seen = skip_time = skip_no_url = skip_too_short = 0

    candidates = []
    for it in items:
        pid = it.get("id") or it.get("originId") or choose_best_url(it)
        if not pid:
            base = (it.get("title") or "") + str(it.get("published") or it.get("updated") or "")
            pid = hashlib.sha1(base.encode("utf-8")).hexdigest()
        if pid in seen:
            skip_seen += 1
            continue

        published = get_item_time(it)
        if not published or published < cutoff:
            skip_time += 1
            continue

        url = choose_best_url(it)
        if not url:
            skip_no_url += 1
            seen.add(pid)
            continue

        candidates.append((pid, it, published, url))

    if verbose:
        logging.info("Skip counts — seen:%d time:%d no_url:%d", skip_seen, skip_time, skip_no_url)
    logging.info("Candidates kept: %d", len(candidates))

    ranked = sorted(candidates, key=lambda t: score_item(t[1], now), reverse=True)[:max_posts]

    created = []
    for pid, item, published, url in ranked:
        title = (item.get("title") or "").strip()

        html_text = fetch_html(url)
        art_title, text = extract_content(html_text)

        # Content fallback: feed summary/content if article looks thin
        if len((text or "").split()) < min_words:
            # many Inoreader items have content or summary with html
            fallback_html = None
            if isinstance(item.get("content"), dict):
                fallback_html = item["content"].get("content")
            if not fallback_html and isinstance(item.get("summary"), dict):
                fallback_html = item["summary"].get("content")
            if fallback_html:
                soup = BeautifulSoup(fallback_html, "lxml")
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                feed_text = soup.get_text(separator=" ", strip=True)
                if len(feed_text.split()) >= min_words:
                    text = feed_text

        if not text or len(text.split()) < min_words:
            skip_too_short += 1
            seen.add(pid)
            if verbose:
                logging.info("Skip (too short): %s", url)
            continue

        if art_title and len(art_title.split()) >= 4:
            title = art_title

        summary_txt = summarize_extract(text, target_words=300)
        if not summary_txt or len(summary_txt.split()) < min_words:
            words = text.split()
            summary_txt = " ".join(words[:max(300, min_words + 40)])

        summary_md = md(summary_txt)
        header = f"*Summary of:* [{title}]({url})\n\n"
        summary_md = header + summary_md

        # tag source if present
        tags = []
        origin = item.get("origin", {})
        if isinstance(origin, dict) and origin.get("title"):
            tags.append(slugify(origin["title"], 24))

        draft_path = write_draft(title, published, url, summary_md, tags=tags, dry=dry)
        if draft_path:
            created.append(draft_path.name)

        seen.add(pid)

    if not dry:
        save_seen(SEEN_PATH, seen)

    if verbose:
        logging.info("Skip too short: %d", skip_too_short)

    print(json.dumps({"created": created}, ensure_ascii=False))
    logging.info("Done. Created %d draft(s).", len(created))

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logging.exception("Fatal error: %s", e)
        sys.exit(1)
