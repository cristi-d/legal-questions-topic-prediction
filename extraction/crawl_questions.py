#!/usr/bin/env python3
"""
Fetch the full title and the opening question of every thread in a threads_X.csv
(produced by crawl_subforum.py) into questions_X.csv.

For each thread, page 1 of the thread is fetched; the question is the
<div class="raspuns-forum" data-id="{first_post_id}"> post, its text taken from
the inner .text-raspuns element. The title comes from h1.topic_title.

Outputs (for threads_10.csv):
    questions_10.csv                 subforum_id, thread_id, first_post_id, full_title, full_question
    raw/questions_10/{thread_id}.html.gz   raw thread HTML; a thread with a raw file
                                           is never fetched again (resume / --reparse)

Usage:
    python crawl_questions.py threads_10.csv
    python crawl_questions.py threads_10.csv --max-threads 5     # quick test
    python crawl_questions.py threads_10.csv --reparse           # rebuild CSV from raw HTML only
"""
from __future__ import annotations

import argparse
import csv
import gzip
import logging
import os
import random
import re
import sys
import time
from pathlib import Path

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

USER_AGENT = "RoLegalDatasetBot/0.1 (academic project; contact: cristian.dascalu@stud.acs.upb.ro)"
COLUMNS = ["subforum_id", "thread_id", "first_post_id", "full_title", "full_question"]
BLOCK_TAGS = {"p", "div", "li", "ul", "ol", "blockquote", "table", "tr", "h1", "h2", "h3",
              "h4", "h5", "h6", "pre"}

log = logging.getLogger("questions")

# --------------------------------------------------------------------------- parsing


def clean_text(text: str) -> str:
    """Normalise spaces inside lines, keep line breaks, drop blank-line runs."""
    text = text.replace("\xa0", " ").replace("\r", "")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    out: list[str] = []
    for line in lines:
        if line or (out and out[-1]):
            out.append(line)
    return "\n".join(out).strip()


def node_text(node: Node) -> str:
    """Text of a post body: <br> and block elements become line breaks."""
    for br in node.css("br"):
        br.replace_with("\n")
    for tag in BLOCK_TAGS:
        for el in node.css(tag):
            el.insert_before("\n")
            el.insert_after("\n")
    return clean_text(node.text(deep=True, separator=""))


def parse_thread(html: str, first_post_id: str) -> tuple[str | None, str | None]:
    tree = HTMLParser(html)
    title_node = (tree.css_first("h1.topic_title span[itemprop='name']")
                  or tree.css_first("h1.topic_title"))
    title = " ".join(title_node.text().split()) if title_node is not None else None

    question = None
    post = tree.css_first(f".raspuns-forum[data-id='{first_post_id}']")
    if post is not None:
        body = post.css_first(".text-raspuns")
        if body is not None:
            question = node_text(body)
    return title, question


# --------------------------------------------------------------------------- input / output


def read_threads(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    missing = {"thread_id", "first_post_id", "subforum_id", "url"} - set(rows[0] if rows else {})
    if missing:
        sys.exit(f"{path}: missing columns {sorted(missing)}")
    return rows


def subforum_of(path: Path, rows: list[dict]) -> str:
    ids = {r["subforum_id"] for r in rows if r["subforum_id"]}
    if len(ids) == 1:
        return ids.pop()
    if m := re.fullmatch(r"threads_(\d+)\.csv", path.name):
        if ids:
            log.warning("several subforum ids in %s (%s); naming output after the file",
                        path, ", ".join(sorted(ids)))
        return m.group(1)
    sys.exit(f"cannot tell the subforum id of {path}; pass --out")


def write_csv(path: Path, rows: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:   # BOM: Excel shows diacritics
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r[k]) for k in COLUMNS})
    os.replace(tmp, path)


# --------------------------------------------------------------------------- fetching


def fetch(client: httpx.Client, url: str, retries: int = 4) -> httpx.Response | None:
    for attempt in range(1, retries + 1):
        try:
            r = client.get(url)
        except httpx.HTTPError as exc:
            log.warning("network error (%s), attempt %d: %s", type(exc).__name__, attempt, url)
            time.sleep(10 * attempt)
            continue
        if r.status_code in (200, 404, 410):
            return r
        wait = 60 * attempt if r.status_code in (429, 503) else 10 * attempt
        if (ra := r.headers.get("Retry-After")) and ra.isdigit():
            wait = int(ra)
        log.warning("HTTP %d, waiting %ds (attempt %d): %s", r.status_code, wait, attempt, url)
        time.sleep(wait)
    return None


# --------------------------------------------------------------------------- main loop


def run(args: argparse.Namespace) -> None:
    src = Path(args.threads_csv)
    threads = read_threads(src)
    sid = subforum_of(src, threads)
    out = Path(args.out or f"questions_{sid}.csv")
    raw = Path(args.raw_dir) / f"questions_{sid}"
    raw.mkdir(parents=True, exist_ok=True)
    if args.max_threads:
        threads = threads[:args.max_threads]

    results: list[dict] = []
    fetched = failed = 0
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ro-RO,ro;q=0.9"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        for i, t in enumerate(threads, 1):
            tid, pid = t["thread_id"], t["first_post_id"]
            raw_file = raw / f"{tid}.html.gz"
            row = {"subforum_id": t["subforum_id"] or sid, "thread_id": tid, "first_post_id": pid}

            if raw_file.exists():
                html = gzip.decompress(raw_file.read_bytes()).decode("utf-8", "replace")
            elif args.reparse:
                log.warning("[%d/%d] thread %s: no raw HTML, skipped", i, len(threads), tid)
                continue
            else:
                if fetched:
                    time.sleep(args.delay + random.uniform(0, args.delay / 2))
                r = fetch(client, t["url"])
                fetched += 1
                if r is None:
                    log.error("[%d/%d] thread %s: giving up; rerun to retry", i, len(threads), tid)
                    failed += 1
                    continue
                if r.status_code != 200:
                    log.warning("[%d/%d] thread %s: HTTP %d", i, len(threads), tid, r.status_code)
                raw_file.write_bytes(gzip.compress(r.content))
                html = r.text

            row["full_title"], row["full_question"] = parse_thread(html, pid)
            if not row["full_question"]:
                log.warning("[%d/%d] thread %s: post %s not found", i, len(threads), tid, pid)
            else:
                log.info("[%d/%d] thread %s: %d chars", i, len(threads), tid,
                         len(row["full_question"]))
            results.append(row)
            if fetched and fetched % 25 == 0:
                write_csv(out, results)       # checkpoint

    write_csv(out, results)
    missing = sum(1 for r in results if not r["full_question"])
    log.info("done: %d rows in %s (%d fetched now, %d without question, %d fetch failures)",
             len(results), out, fetched, missing, failed)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("threads_csv", help="threads_X.csv produced by crawl_subforum.py")
    p.add_argument("--out", help="output CSV (default: questions_{subforum}.csv)")
    p.add_argument("--raw-dir", default="raw", help="where raw HTML goes (default: raw/)")
    p.add_argument("--delay", type=float, default=1.5, help="base delay between requests, seconds")
    p.add_argument("--max-threads", type=int, default=0, help="only the first N threads (0 = all)")
    p.add_argument("--reparse", action="store_true", help="build the CSV from raw HTML, no requests")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    run(args)


if __name__ == "__main__":
    main()
