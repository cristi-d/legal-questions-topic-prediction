#!/usr/bin/env python3
"""
Crawl the topic listing of one avocatnet.ro subforum into a CSV file.

Uses the listing endpoint:
    https://www.avocatnet.ro/forum/utile/list_topics?id={subforum}&page={page}

Outputs (default, for --subforum 107):
    threads_107.csv              one row per thread (the dataset)
    raw/107/page_00001.html.gz   raw listing HTML, for --reparse without new requests
    raw/107/progress.json        crawl progress, so a stopped run can resume

The listing is sorted by last reply, newest first (stickies on page 1 excepted).
If every non-sticky entry on a page was last active before --since, no later
page can hold a thread created after --since, so the crawl stops there.

Usage:
    pip install httpx selectolax
    python crawl_subforum.py --subforum 107 --since 2013-01-01
    python crawl_subforum.py --subforum 107 --since 2013-01-01 --max-pages 5   # quick test
    python crawl_subforum.py --subforum 107 --reparse                          # rebuild CSV from raw HTML
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import logging
import os
import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from selectolax.lexbor import LexborHTMLParser as HTMLParser, LexborNode as Node

LIST_URL = "https://www.avocatnet.ro/forum/utile/list_topics?id={sid}&page={page}"
BASE = "https://www.avocatnet.ro"
USER_AGENT = "RoLegalDatasetBot/0.1 (academic project; contact: cristian.dascalu@stud.acs.upb.ro)"

log = logging.getLogger("crawler")

# --------------------------------------------------------------------------- parsing

MONTHS = {
    "ianuarie": 1, "februarie": 2, "martie": 3, "aprilie": 4, "mai": 5, "iunie": 6,
    "iulie": 7, "august": 8, "septembrie": 9, "octombrie": 10, "noiembrie": 11,
    "decembrie": 12,
}
RE_THREAD = re.compile(r"/forum/discutie_(\d+)/")
RE_USER = re.compile(r"/(consultant|utilizator)/[^*\"']+\*(\d+)")
RE_SUBFORUM = re.compile(r"/forum/(\d+)/")
RE_DATE = re.compile(r"(\d{1,2}):(\d{2}),\s*(\d{1,2})\s+([A-Za-zăâîșşțţĂÂÎȘŞȚŢ]+)\s+(\d{4})")
RE_TOTAL = re.compile(r"\d+\s+din\s+(\d+)")
RE_LAST_POST = re.compile(r"/forum/discutie_\d+/[^#*\"']*(?:\*(\d+))?#(\d+)")


def ro_date(text: str | None) -> tuple[str | None, str | None]:
    """'15:32, 6 Noiembrie 2026' -> ('2026-11-06T15:32', raw)."""
    if not text:
        return None, None
    m = RE_DATE.search(text)
    if not m:
        return None, None
    hh, mm, d, month, y = m.groups()
    mon = MONTHS.get(month.lower())
    if mon is None:
        return None, m.group(0)
    return f"{y}-{mon:02d}-{int(d):02d}T{int(hh):02d}:{mm}", m.group(0)


def _href(node: Node | None) -> str:
    return (node.attributes.get("href") or "") if node is not None else ""


def _int(text: str | None) -> int | None:
    m = re.search(r"\d+", (text or "").replace(".", ""))
    return int(m.group(0)) if m else None


def parse_entry(box: Node, page: int) -> dict | None:
    """Parse one <div class="listing-forum"> block."""
    title_a = box.css_first("h5 a[href*='discutie_']")
    m = RE_THREAD.search(_href(title_a))
    if not m:
        return None
    tid = int(m.group(1))

    # counts: <span class="cifre" title="Mesaje în discuție"> / title="Vizualizări discuție"
    nr_messages = nr_views = None
    for span in box.css(".statistici-discutie span.cifre"):
        t = (span.attributes.get("title") or "").lower()
        if t.startswith("mesaje"):
            nr_messages = _int(span.text())
        elif t.startswith("vizualiz"):
            nr_views = _int(span.text())

    created, created_raw = ro_date(box.css_first(".tip-rezultat .afiseaza-data").text()
                                   if box.css_first(".tip-rezultat .afiseaza-data") else None)

    author_type = author_id = author_name = None
    subforum_id = None
    info = box.css_first(".info-user-topic")
    if info is not None:
        for a in info.css("a[href]"):
            h = _href(a)
            if author_type is None and (um := RE_USER.search(h)):
                author_type, author_id = um.group(1), int(um.group(2))
                author_name = a.text(strip=True)
            elif subforum_id is None and (sm := RE_SUBFORUM.search(h)):
                subforum_id = int(sm.group(1))

    # last reply block (absent when nobody has replied)
    last_reply = last_reply_raw = None
    last_type = last_id = last_label = last_post_id = None
    last_reply_page = None
    part = box.css_first(".participare-discutie")
    if part is not None and "Ultimul răspuns" in part.text():
        last_reply, last_reply_raw = ro_date(part.text())
        for a in part.css("a[href]"):
            h = _href(a)
            if (um := RE_USER.search(h)):
                last_type, last_id = um.group(1), int(um.group(2))
                # "<strong>Name</strong>, Avocat" -> label after the last comma
                full = a.text(separator=" ", strip=True)
                last_label = full.rsplit(",", 1)[1].strip() if "," in full else None
            elif (tm := RE_LAST_POST.search(h)):
                last_reply_page = int(tm.group(1)) if tm.group(1) else 1
                last_post_id = int(tm.group(2))

    return {
        "thread_id": tid,
        "first_post_id": _int(box.attributes.get("data-id")),
        "subforum_id": subforum_id,
        "title": " ".join(title_a.text().split()),
        "url": BASE + _href(title_a).split("#")[0],
        "is_locked": int(title_a.css_first("i.fa-lock") is not None),
        "creation_date": created,
        "creation_date_raw": created_raw,
        "last_reply_date": last_reply,
        "last_reply_date_raw": last_reply_raw,
        "nr_messages": nr_messages,
        "nr_views": nr_views,
        "author_type": author_type,
        "author_id": author_id,
        "last_replier_type": last_type,
        "last_replier_id": last_id,
        "last_replier_label": last_label,
        "last_replier_is_author": (int(last_id == author_id)
                                   if last_id is not None and author_id is not None else None),
        "last_reply_post_id": last_post_id,
        "last_reply_page": last_reply_page,
        "listing_page": page,
    }


def parse_listing(html: str, page: int) -> tuple[list[dict], int | None]:
    tree = HTMLParser(html)
    entries, seen = [], set()
    for box in tree.css("div.listing-forum"):
        e = parse_entry(box, page)
        if e is None or e["thread_id"] in seen:
            continue
        seen.add(e["thread_id"])
        entries.append(e)

    if page == 1:
        mark_stickies(entries)
    for e in entries:
        e.setdefault("is_sticky", 0)

    total = None
    mid = tree.css_first(".betweenpages .midsection")
    m = RE_TOTAL.search(mid.text(separator=" ") if mid is not None else
                        (tree.body.text(separator=" ") if tree.body else html))
    if m:
        total = int(m.group(1))
    return entries, total


def activity(e: dict) -> str:
    return e["last_reply_date"] or e["creation_date"] or ""


def mark_stickies(entries: list[dict]) -> None:
    """Stickies sit at the top of page 1 out of activity order: an entry is
    flagged if a later entry on the page is more recently active."""
    later_max = ""
    flags = []
    for e in reversed(entries):
        flags.append(activity(e) < later_max)
        later_max = max(later_max, activity(e))
    flags.reverse()
    # Only a leading run counts; anything after the first in-order entry is regular.
    leading = True
    for e, f in zip(entries, flags):
        leading = leading and f
        e["is_sticky"] = int(leading)


def activity(e: dict) -> str:
    return e["last_reply_date"] or e["creation_date"] or ""


def mark_stickies(entries: list[dict]) -> None:
    """Stickies sit at the top of page 1 out of activity order: an entry is
    flagged if a later entry on the page is more recently active."""
    later_max = ""
    flags = []
    for e in reversed(entries):
        flags.append(activity(e) < later_max)
        later_max = max(later_max, activity(e))
    flags.reverse()
    # Only a leading run counts; anything after the first in-order entry is regular.
    leading = True
    for e, f in zip(entries, flags):
        leading = leading and f
        e["is_sticky"] = int(leading)


# --------------------------------------------------------------------------- output

COLUMNS = [
    "thread_id", "first_post_id", "subforum_id", "title", "url", "is_locked",
    "creation_date", "creation_date_raw", "last_reply_date", "last_reply_date_raw",
    "nr_messages", "nr_views", "author_type", "author_id", "last_replier_type",
    "last_replier_id", "last_replier_label", "last_replier_is_author",
    "last_reply_post_id", "last_reply_page", "is_sticky", "listing_page", "listed_at",
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_csv(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        return {int(r["thread_id"]): r for r in csv.DictReader(f)}


def write_csv(path: Path, rows: dict[int, dict]) -> None:
    """Rewrite the whole file atomically, newest threads first."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8-sig", newline="") as f:   # BOM: Excel shows diacritics
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for tid in sorted(rows, reverse=True):
            w.writerow({k: ("" if v is None else v) for k, v in rows[tid].items()})
    os.replace(tmp, path)


def load_progress(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_progress(path: Path, **kw) -> None:
    path.write_text(json.dumps({**kw, "updated_at": now()}, indent=2))


def paths(args) -> tuple[Path, Path, Path]:
    out = Path(args.out or f"threads_{args.subforum}.csv")
    raw = Path(args.raw_dir) / str(args.subforum)
    return out, raw, raw / "progress.json"


def keep(entries: list[dict], since: str, sid: int, listed_at: str) -> list[dict]:
    kept = []
    for e in entries:
        e["listed_at"] = listed_at
        e["subforum_id"] = e["subforum_id"] or sid
        if (e["creation_date"] or "") >= since:
            kept.append(e)
    return kept


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

def crawl(args: argparse.Namespace) -> None:
    sid, since = args.subforum, args.since
    out, raw, prog_path = paths(args)
    raw.mkdir(parents=True, exist_ok=True)

    prog = {} if args.restart else load_progress(prog_path)
    if prog.get("since") not in (None, since):
        log.warning("--since changed (%s -> %s); starting from page 1", prog["since"], since)
        prog = {}
    if prog.get("finished"):
        log.info("subforum %d already finished (use --restart to crawl again)", sid)
        return
    start_page = prog.get("last_page", 0) + 1
    total_pages = prog.get("total_pages")
    if start_page > 1:
        log.info("resuming subforum %d from page %d", sid, start_page)

    rows = {} if args.restart else load_csv(out)
    kept_total = 0

    def checkpoint(page: int, finished: bool) -> None:
        write_csv(out, rows)
        save_progress(prog_path, subforum_id=sid, since=since, total_pages=total_pages,
                      last_page=page, finished=finished)

    headers = {"User-Agent": USER_AGENT, "Accept-Language": "ro-RO,ro;q=0.9"}
    with httpx.Client(headers=headers, timeout=30, follow_redirects=True) as client:
        page = start_page
        while True:
            if args.max_pages and page >= start_page + args.max_pages:
                log.info("reached --max-pages, stopping (resume later from page %d)", page)
                break
            if args.max_threads and kept_total >= args.max_threads:
                log.info("reached --max-threads %d, stopping", kept_total)
                break

            if total_pages and page > total_pages:
                log.info("past last page (%d)", total_pages)
                checkpoint(page - 1, True)
                break

            r = fetch(client, LIST_URL.format(sid=sid, page=page))
            if r is None:
                log.error("giving up on page %d; rerun to resume", page)
                break
            if r.status_code != 200:
                log.info("HTTP %d on page %d, treating as end of listing", r.status_code, page)
                checkpoint(page, True)
                break

            (raw / f"page_{page:05d}.html.gz").write_bytes(gzip.compress(r.content))
            entries, tp = parse_listing(r.text, page)
            total_pages = total_pages or tp
            if not entries:
                log.info("no entries on page %d, end of listing", page)
                checkpoint(page, True)
                break

            kept = keep(entries, since, sid, now())
            for e in kept:
                rows[e["thread_id"]] = e
            kept_total += len(kept)

            regular = [e for e in entries if not e["is_sticky"]]
            newest = max((activity(e) for e in regular), default="")
            unparsed = sum(1 for e in entries if not e["creation_date"])
            log.info("page %d/%s: %d entries, kept %d, newest activity %s%s, %d entries in total so far",
                     page, total_pages or "?", len(entries), len(kept), newest[:10] or "-",
                     f", {unparsed} without creation date!" if unparsed else "", kept_total)

            stop = bool(regular and newest and newest < since)
            checkpoint(page, stop)
            if stop:
                log.info("all threads on page %d inactive since before %s, stopping", page, since)
                break

            page += 1
            time.sleep(args.delay + random.uniform(0, args.delay / 2))

    log.info("done: kept %d threads this run, %d in %s", kept_total, len(rows), out)


def reparse(args: argparse.Namespace) -> None:
    """Rebuild the CSV from the saved raw HTML, without any network access."""
    out, raw, _ = paths(args)
    files = sorted(raw.glob("page_*.html.gz"))
    rows: dict[int, dict] = {}
    for f in reversed(files):            # older pages first, so newer snapshots win
        page = int(f.name[5:10])
        listed_at = datetime.fromtimestamp(f.stat().st_mtime, timezone.utc).isoformat(timespec="seconds")
        entries, _ = parse_listing(gzip.decompress(f.read_bytes()).decode("utf-8", "replace"), page)
        for e in keep(entries, args.since, args.subforum, listed_at):
            rows[e["thread_id"]] = e
    write_csv(out, rows)
    log.info("re-parsed %d pages, %d threads -> %s", len(files), len(rows), out)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subforum", type=int, required=True, help="subforum id, e.g. 107")
    p.add_argument("--since", default="2013-01-01",
                   help="keep threads created on/after this date (YYYY-MM-DD); default 2013-01-01")
    p.add_argument("--out", help="output CSV (default: threads_{subforum}.csv)")
    p.add_argument("--raw-dir", default="raw", help="where raw HTML and progress go (default: raw/)")
    p.add_argument("--delay", type=float, default=1.5, help="base delay between requests, seconds")
    p.add_argument("--max-pages", type=int, default=0, help="stop after N pages this run (0 = no limit)")
    p.add_argument("--max-threads", type=int, default=0, help="stop after N threads this run (0 = no limit)")
    p.add_argument("--restart", action="store_true", help="ignore saved progress and existing CSV")
    p.add_argument("--reparse", action="store_true", help="rebuild the CSV from raw HTML instead of crawling")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    datetime.strptime(args.since, "%Y-%m-%d")  # validate
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    (reparse if args.reparse else crawl)(args)


if __name__ == "__main__":
    main()