#!/usr/bin/env python3
import os, re, json, time, math, hashlib, textwrap, logging, pathlib
from datetime import datetime, timezone, timedelta
import yaml, requests, feedparser
from dateutil import parser as dtparse
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import trafilatura
from readability import Document
from markdownify import markdownify as md
from rake_nltk import Rake
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.text_rank import TextRankSummarizer

# NLTK punkt (first run only)
try:
    import nltk
    nltk.data.find("tokenizers/punkt")
except:
    import nltk
    nltk.download("punkt")

logging.basicConfig(level=logging.INFO, format="%(message)s")
ROOT = pathlib.Path(__file__).resolve().parents[1]
CFG = yaml.safe_load(open(ROOT/"config.yaml", "r", encoding="utf-8"))

DRAFTS_DIR = ROOT / CFG.get("drafts_dir","drafts")
STATE_FILE = ROOT / "state" / "seen.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
DRAFTS_DIR.mkdir(parents=True, exist_ok=True)

def now_utc():
    return datetime.now(timezone.utc)

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return []

def save_state(seen):
    STATE_FILE.write_text(json.dumps(seen, indent=2))

def shorthash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:10]

def within_window(published_iso: str, hours: int) -> bool:
    try:
        dt = dtparse.parse(published_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return True  # keep if unknown
    return (now_utc() - dt) <= timedelta(hours=hours)

def score_item(title: str, url: str) -> float:
    """very simple free scoring: length + newsy keywords + domain signal"""
    title = title or ""
    base = min(len(title)/120, 1.0)*0.3
    newsy = sum(1 for w in ["policy","regulation","law","government","election",
                            "parliament","congress","minister","EU","UN","NATO"]
                if w.lower() in title.lower()) * 0.06
    dom = urlparse(url).netloc
    tier = 0.2 if any(k in dom for k in ["bbc","reuters","apnews","bloomberg","wsj",
                                         "ft.com","economist","nytimes","guardian"]) else 0.05
    return min(1.0, base+newsy+tier)

def fetch_html(url: str, timeout=20) -> str:
    headers = {
        "User-Agent":"Mozilla/5.0 (compatible; PolicyDispatchBot/1.0; +https://thepolicydispatch.com/)"
    }
    r = requests.get(url, timeout=timeout, headers=headers)
    r.raise_for_status()
    return r.text

def extract_text(url: str, html: str) -> str:
    # Try trafilatura first
    try:
        txt = trafilatura.extract(html, include_comments=False, include_tables=False)
        if txt and len(txt.split()) > 80:
            return txt
    except Exception:
        pass
    # Fallback: Readability + BS4
    try:
        doc = Document(html)
        main_html = doc.summary(html_partial=True)
        soup = BeautifulSoup(main_html, "lxml")
        for tag in soup(["script","style","noscript","form","header","footer","nav","aside"]):
            tag.decompose()
        text = soup.get_text("\n")
        text = re.sub(r"\n{2,}", "\n", text).strip()
        if len(text.split()) > 80:
            return text
    except Exception:
        pass
    # final fallback: plain page text
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text("\n")
    return re.sub(r"\n{2,}", "\n", text).strip()

def summarize_textrank(text: str, target_words: int=300) -> str:
    # estimate sentence count for target words (avg 22 words/sentence)
    sent_count = max(5, min(20, round(target_words/22)))
    parser = PlaintextParser.from_string(text, Tokenizer("english"))
    summ = TextRankSummarizer()
    sentences = list(summ(parser.document, sent_count))
    out = " ".join(str(s) for s in sentences)
    return out

def rake_keywords(text: str, topn=6):
    r = Rake(min_length=1, max_length=3)
    r.extract_keywords_from_text(text)
    phrases = [p for p,_ in sorted(r.get_ranked_phrases_with_scores(), reverse=True)]
    return [p for p in phrases[:topn] if len(p.split())<=4]

def clean_title(title: str) -> str:
    title = re.sub(r"\s+\|\s+.*$", "", title).strip()
    title = title.replace(" - Reuters", "").replace(" | Reuters", "")
    return title

def to_markdown(title, url, published_dt, body_md, bullets, cfg):
    fm = {
        "layout": "post",
        "title": title,
        "date": published_dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S %z"),
        "tags": cfg.get("site_tags",["automated-news"]),
        "author": cfg.get("site_author","TPD Automations"),
        "original_link": url,
        "section": cfg.get("section_slug","automated-news"),
    }
    fm_lines = ["---"] + [f'{k}: {json.dumps(v) if not isinstance(v, str) else v!r}'.replace("'", '"') for k,v in fm.items()] + ["---",""]
    bullet_md = ""
    if bullets:
        bullet_md = "\n\n**Key points**\n\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
    return "\n".join(fm_lines) + body_md + bullet_md

def first_dt(s):
    try:
        d = dtparse.parse(s)
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return now_utc()

def make_article(item, cfg):
    title = clean_title(item.get("title") or "")
    url = item.get("alternate", [{}])[0].get("href") or item.get("canonical", [{}])[0].get("href") or item.get("link") or ""
    if not url:
        return None

    # Fetch
    html = fetch_html(url, timeout=cfg.get("timeout_seconds",20))
    text = extract_text(url, html)
    if len(text.split()) < 120:
        logging.info("Skip (too short body) %s", url); return None

    # Summarize
    body = summarize_textrank(text, cfg.get("target_words",300))
    # Tidy to ~300 words
    words = body.split()
    target = cfg.get("target_words",300)
    if len(words) > target + 60:
        body = " ".join(words[:target+60])

    # RAKE points
    kws = rake_keywords(text, topn=6)
    bullets = [kw.capitalize() for kw in kws]

    # Markdown-ify (it’s plain text, but keep consistent)
    body_md = md(body)

    published_raw = item.get("published") or item.get("updated") or item.get("created") or ""
    published_dt = first_dt(published_raw)

    # assemble markdown with front matter
    md_out = to_markdown(title, url, published_dt, body_md, bullets, cfg)
    slug_date = published_dt.strftime("%Y-%m-%d")
    slug_title = re.sub(r"[^a-z0-9]+","-", title.lower()).strip("-")[:60] or shorthash(url)
    filename = f"{slug_date}-{slug_title}.md"
    return filename, md_out

def main():
    seen = load_state()
    feed_url = CFG["inoreader_json_url"]
    logging.info("Fetching feed: %s", feed_url)
    r = requests.get(feed_url, timeout=20, headers={"User-Agent":"PD-AutoNews/1.0"})
    r.raise_for_status()
    data = r.json() if "json" in r.headers.get("Content-Type","") or feed_url.endswith("json") else {}

    items = data.get("items", [])
    logging.info("Items in feed: %d", len(items))

    # filter window + simple keyword filters
    window = CFG.get("window_hours", 6)
    inc = [w.lower() for w in CFG.get("include_keywords",[])]
    exc = [w.lower() for w in CFG.get("exclude_keywords",[])]

    cand = []
    for it in items:
        url = it.get("alternate", [{}])[0].get("href") or it.get("canonical", [{}])[0].get("href") or ""
        if not url: 
            continue
        if url in seen: 
            continue
        pub = it.get("published") or it.get("updated") or it.get("created") or ""
        if window and not within_window(pub, window):
            continue
        title = it.get("title","")
        tl = title.lower()
        if inc and not any(k in tl for k in inc): 
            continue
        if any(k in tl for k in exc): 
            continue
        s = score_item(title, url)
        if s >= CFG.get("min_score", 0.2):
            cand.append((s, it))

    cand.sort(key=lambda x: x[0], reverse=True)
    cand = cand[: CFG.get("max_items",6)]
    logging.info("Candidates kept: %d", len(cand))

    created = []
    for score, it in cand:
        try:
            res = make_article(it, CFG)
            if not res: 
                continue
            filename, content = res
            out_path = DRAFTS_DIR / filename
            out_path.write_text(content, encoding="utf-8")
            created.append(str(out_path.relative_to(ROOT)))
            url = it.get("alternate", [{}])[0].get("href") or it.get("canonical", [{}])[0].get("href") or ""
            seen.append(url)
            logging.info("Draft created: %s", out_path)
        except Exception as e:
            logging.exception("Failed on item: %s", it.get("title"))

    save_state(seen)
    print(json.dumps({"created": created}, indent=2))

if __name__ == "__main__":
    main()
