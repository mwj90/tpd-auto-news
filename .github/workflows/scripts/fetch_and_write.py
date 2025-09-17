#!/usr/bin/env python3
import os, json, re, time, hashlib, pathlib, textwrap
from datetime import datetime, timezone
import requests, feedparser, yaml
from markdownify import markdownify as md
from sumy.parsers.plaintext import PlaintextParser
from sumy.nlp.tokenizers import Tokenizer
from sumy.summarizers.lsa import LsaSummarizer

ROOT = pathlib.Path(__file__).resolve().parents[1]
POSTS = ROOT / "_posts"
STATE = ROOT / "state" / "seen.json"
CFG = ROOT / "config.yaml"

POSTS.mkdir(exist_ok=True, parents=True)
STATE.parent.mkdir(exist_ok=True, parents=True)
if not STATE.exists(): STATE.write_text("[]", encoding="utf-8")

cfg = yaml.safe_load(CFG.read_text(encoding="utf-8"))
source_url = cfg.get("inoreader_json")
max_posts = int(cfg.get("max_posts", 5))

def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s).strip().lower()
    return re.sub(r"[\s_-]+", "-", s)

def summarize(text, max_sentences=4):
    text = text.strip()
    if not text:
        return ""
    try:
        parser = PlaintextParser.from_string(text, Tokenizer("english"))
        summ = LsaSummarizer()
        sentences = summ(parser.document, max_sentences)
        out = " ".join(str(s) for s in sentences)
        if len(out) < 120:  # fallback if too short
            out = " ".join(text.split()[:120])
        return out
    except Exception:
        return " ".join(text.split()[:160])

def clean_html_to_md(html):
    # Convert HTML to markdown, collapse long whitespace
    m = md(html or "", strip=["script","style"])
    m = re.sub(r"\n{3,}","\n\n", m).strip()
    return m

def write_post(item):
    title = item.get("title") or "Untitled"
    link = item.get("link") or item.get("origin_id") or ""
    published = item.get("published") or item.get("published_parsed")
    # date handling
    try:
        if isinstance(published, str):
            dt = datetime.fromtimestamp(feedparser._parse_date_w3dtf(published).tm_sec, tz=timezone.utc)
        elif published:
            dt = datetime.fromtimestamp(time.mktime(published), tz=timezone.utc)
        else:
            dt = datetime.now(tz=timezone.utc)
    except Exception:
        dt = datetime.now(tz=timezone.utc)

    date_str = dt.strftime("%Y-%m-%d")
    slug = slugify(title)[:80] or hashlib.md5(title.encode()).hexdigest()[:12]
    filename = f"{date_str}-{slug}.md"
    path = POSTS / filename

    # content
    summary_source = item.get("summary") or item.get("content", [{}])[0].get("content") or ""
    text_md = clean_html_to_md(summary_source)
    short = summarize(text_md, 4)

    fm = []
    fm.append("---")
    fm.append("layout: post")
    fm.append(f'title: "{title.replace("\"","\\\"")}"')
    fm.append(f"date: {dt.strftime('%Y-%m-%d %H:%M:%S %z')}")
    if link:
        fm.append(f'original_link: "{link}"')
    fm.append("---\n")

    body = textwrap.dedent(f"""
    *AI summary:*

    {short}

    ---

    *Details:*

    {text_md}
    """).strip()+"\n"

    path.write_text("\n".join(fm) + body, encoding="utf-8")
    return path

def main():
    seen = set(json.loads(STATE.read_text(encoding="utf-8")))
    print(f"Fetching: {source_url}")
    r = requests.get(source_url, timeout=30)
    r.raise_for_status()
    data = r.json()

    entries = data.get("items") or data  # handle raw list or object
    created = []
    for item in entries[: max_posts * 3]:  # overfetch a bit, we may skip some
        uid = item.get("id") or item.get("origin_id") or item.get("link") or hashlib.md5(json.dumps(item, sort_keys=True).encode()).hexdigest()
        if uid in seen:
            continue
        p = write_post(item)
        seen.add(uid)
        created.append(p)
        if len(created) >= max_posts:
            break

    STATE.write_text(json.dumps(sorted(list(seen))[-2000:]), encoding="utf-8")
    if created:
        print("Created posts:")
        for p in created: print(" -", p)
    else:
        print("No new posts created.")

if __name__ == "__main__":
    main()
