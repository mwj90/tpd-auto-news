import os, json, re, time, textwrap
from pathlib import Path
import yaml
import requests
import feedparser

cfg = yaml.safe_load(open("config.yaml", "r"))
STATE = Path(cfg["state_file"]); STATE.parent.mkdir(parents=True, exist_ok=True)
seen = set(json.load(open(STATE)) if STATE.exists() else [])

def clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()

def mk_slug(t: str) -> str:
    import unicodedata
    t = unicodedata.normalize("NFKD", t).encode("ascii","ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t.lower()).strip("-")
    return t[:80] or "post"

def parse_inoreader_json(url: str):
    """Return list of dicts with keys: id,title,link,summary,published_parsed."""
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    items = data.get("items") or data  # tolerate array form
    out = []
    for it in items:
        _id = it.get("id") or it.get("url") or it.get("link") or it.get("title")
        title = it.get("title") or ""
        link  = it.get("url") or it.get("link") or ""
        summary = it.get("summary") or it.get("content") or ""
        if isinstance(summary, list) and summary and isinstance(summary[0], dict):
            summary = summary[0].get("content", "")
        published = it.get("published") or it.get("date_published") or it.get("updated")
        if isinstance(published, (int, float)):
            published_parsed = time.gmtime(int(published))
        else:
            published_parsed = feedparser._parse_date(published) or time.gmtime(0)
        out.append({
            "id": _id, "title": title, "link": link,
            "summary": clean(summary),
            "published_parsed": published_parsed
        })
    return out

def parse_feed(url: str):
    if "/json" in url or url.endswith("json") or "view/json" in url:
        return parse_inoreader_json(url)
    fp = feedparser.parse(url)
    rows = []
    for e in fp.entries:
        rows.append({
            "id": e.get("id") or e.get("link") or e.get("title"),
            "title": e.get("title",""),
            "link": e.get("link",""),
            "summary": clean(e.get("summary") or e.get("description") or ""),
            "published_parsed": e.get("published_parsed") or time.gmtime(0)
        })
    return rows

entries = parse_feed(cfg["rss_url"])

# Unseen → newest first → cap
fresh = [e for e in entries if e["id"] and (e["id"] not in seen)]
fresh.sort(key=lambda x: x["published_parsed"] or time.gmtime(0), reverse=True)
fresh = fresh[: cfg["max_items_per_run"]]

if not fresh:
    print("No new items.")
    raise SystemExit(0)

# -------- Open-source summarizer (no API keys needed) --------
from transformers import pipeline
summarizer = pipeline(
    "summarization",
    model="sshleifer/distilbart-cnn-12-6",
    tokenizer="sshleifer/distilbart-cnn-12-6"
)

def summarize(txt: str) -> str:
    txt = clean(txt)[:4000]
    out = summarizer(txt, max_length=220, min_length=120, do_sample=False)
    return clean(out[0]["summary_text"])

def headline_from(summary: str) -> str:
    out = summarizer("Headline, 10 words max, punchy: " + summary,
                     max_length=30, min_length=10, do_sample=False)
    return clean(out[0]["summary_text"]).rstrip(".")

def make_article(summary: str, orig: str) -> str:
    segs = re.split(r'(?<=[.!?])\s+', summary)[:5]
    bullets = "\n".join(f"- {s}" for s in segs if s)
    body = f"""{summary}

**Key points**
{bullets}

Why it matters: Signals and second-order effects for policy, markets, and development."""
    if len(body.split()) < cfg["min_length"]:
        more = clean(orig)[:1500]
        if more:
            body += "\n\n**Context (source)**\n" + more
    return body

out_dir = Path(cfg["output_dir"]); out_dir.mkdir(parents=True, exist_ok=True)
written = 0

for e in fresh:
    uid = e["id"]
    title = clean(e["title"] or "(no title)")
    link  = e["link"] or ""
    source_txt = e["summary"] or ""

    base = f"{title}. {source_txt}"
    summ = summarize(base)
    head = headline_from(summ) or title
    article = make_article(summ, source_txt)

    pub = e["published_parsed"] or time.gmtime()
    date = time.strftime("%Y-%m-%d", pub)
    slug = mk_slug(head or title)
    fname = f"{date}-{slug}.md"
    fpath = out_dir / fname

    front = textwrap.dedent(f"""\
    ---
    layout: post
    title: "{head.replace('"','\\"')}"
    date: {date}
    author: "{cfg['author']}"
    original_link: "{link}"
    ---
    """)

    fpath.write_text(front + "\n" + article + (f"\n\n[Original link]({link})" if link else ""),
                     encoding="utf-8")

    written += 1
    seen.add(uid)

json.dump(sorted(list(seen)), open(STATE, "w"))
print(f"Wrote {written} post(s).")
