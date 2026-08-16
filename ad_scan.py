#!/usr/bin/env python3
"""
ad_scan.py - Ad-rate every host in the FMHY link index, in resumable batches.

Why batches: data/links.json holds ~10,900 entries across ~7,800 unique hosts.
A real browser needs 10-20s per page, so a full sweep is many hours - far past
the GitHub Actions job limit. This script therefore:

  1. Builds a host index from data/links.json (one entry per HOST, not per link,
     since 10,900 links collapse to 7,800 hosts and ads are a property of the site).
  2. Loads previous results from data/ad-scan-state.json.
  3. Picks the next N hosts to check - never-scanned first, most-referenced
     first within that, then anything due a re-check.
  4. Scans them with the engine in ad_rater.py.
  5. Merges and writes results back.

Run it on a cron a few times a day and the whole index gets covered in ~2 weeks,
then rolls over into continuous re-checking.

Outputs:
  data/ad-ratings.json      compact host -> rating map, for the Flutter app
  data/ad-ratings.csv       human-readable, sorted worst-first
  data/ad-scan-state.json   scanner state (timestamps, failure counts)

Usage:
  python ad_scan.py --limit 250 --concurrency 4
  python ad_scan.py --limit 50 --page Streaming      # one category only
  python ad_scan.py --starred-only                   # community picks first
  python ad_scan.py --dry-run                        # show the queue, scan nothing
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from ad_rater import (
    AD_DOMAINS, analyse_browser, score_result, Result,
)

DATA = Path("data")
LINKS_FILE = DATA / "links.json"
STATE_FILE = DATA / "ad-scan-state.json"
RATINGS_FILE = DATA / "ad-ratings.json"
CSV_FILE = DATA / "ad-ratings.csv"

# Re-check a healthy host this often; back off hard on repeated failures.
RECHECK_DAYS = 30
FAIL_BACKOFF_DAYS = 45
MAX_FAILS = 3

# Hosts there is no point loading in a browser.
SKIP_SCHEMES = {"magnet", "mailto", "ftp", "irc", "ircs", "tg", "javascript"}

# Cloudflare and friends serve a challenge page to datacentre IPs. It carries no
# ads, so scoring it gives a false A+ — the worst possible failure mode here,
# since it flatters exactly the sites most likely to be ad-heavy.
CHALLENGE_MARKERS = (
    "just a moment", "attention required", "checking your browser",
    "verifying you are human", "verify you are human", "one moment, please",
    "access denied", "security check", "ddos-guard", "are you a robot",
    "please wait...", "cf-challenge", "bot verification",
)
CHALLENGE_STATUSES = {401, 403, 405, 429, 503}

# FMHY's 14 pages are too coarse to filter by — "Streaming" covers films, live
# sport and subtitle tools alike. The wiki's section headings carry the real
# topic, so each host is tagged from page + section instead.
#
# Rules are (substring of section, topic) and are tried in order; the first
# match wins, and anything unmatched falls back to the page's own label.
TOPIC_RULES: dict[str, list[tuple[str, str]]] = {
    "Streaming": [
        ("live tv", "Live TV & Sports"),
        ("sport", "Live TV & Sports"),
        ("smart tv", "Smart TV"),
        ("specialty", "Anime & Specialty"),
        ("anime", "Anime & Specialty"),
        ("subtitle", "Subtitles"),
        ("download", "Video Downloads"),
        ("torrent", "Torrents"),
        ("tracking", "Trackers & Databases"),
        ("database", "Trackers & Databases"),
        ("tool", "Video Tools"),
        ("", "Movies & TV"),
    ],
    "Reading": [
        ("audiobook", "Audiobooks"),
        ("visual media", "Comics & Manga"),
        ("comic", "Comics & Manga"),
        ("manga", "Comics & Manga"),
        ("educational", "Textbooks"),
        ("document", "Articles & Docs"),
        ("article", "Articles & Docs"),
        ("news", "Articles & Docs"),
        ("tracking", "Trackers & Databases"),
        ("database", "Trackers & Databases"),
        ("", "Ebooks"),
    ],
    "Music": [
        ("radio", "Radio"),
        ("rip", "Music Downloads"),
        ("torrent", "Music Downloads"),
        ("download", "Music Downloads"),
        ("tool", "Audio Tools"),
        ("edit", "Audio Tools"),
        ("tracking", "Trackers & Databases"),
        ("database", "Trackers & Databases"),
        ("", "Music Streaming"),
    ],
    "Gaming": [
        ("browser", "Browser Games"),
        ("emulation", "Emulation & ROMs"),
        ("rom", "Emulation & ROMs"),
        ("puzzle", "Browser Games"),
        ("tabletop", "Tabletop Games"),
        ("download", "Game Downloads"),
        ("torrent", "Game Downloads"),
        ("", "Games"),
    ],
    "Educational": [
        ("course", "Courses"),
        ("learn", "Courses"),
        ("language", "Languages"),
        ("science", "Science & Maths"),
        ("math", "Science & Maths"),
        ("", "Learning"),
    ],
    "Mobile": [
        ("ios", "iOS Apps"),
        ("apple", "iOS Apps"),
        ("", "Android Apps"),
    ],
}

# Used when a page has no rules, or no rule matched.
PAGE_TOPICS = {
    "Streaming": "Movies & TV",
    "Reading": "Ebooks",
    "Music": "Music Streaming",
    "Gaming": "Games",
    "Educational": "Learning",
    "Mobile": "Android Apps",
    "Downloading": "Downloads",
    "Torrenting": "Torrents",
    "Artificial-Intelligence": "AI Tools",
    "Linux": "Linux & Mac",
    "Storage": "File Storage",
    "Adblock": "Adblocking",
    "Non-Eng": "Non-English",
    "Misc": "Everything Else",
}


def topic_for(page: str, section: str | None) -> str:
    sec = (section or "").lower()
    for needle, topic in TOPIC_RULES.get(page, []):
        if needle == "" or needle in sec:
            return topic
    return PAGE_TOPICS.get(page, page.replace("-", " "))
SKIP_HOST_SUFFIXES = (".onion", ".i2p", ".loki", ".bit",
                      ".google.com", ".googleusercontent.com")
SKIP_HOSTS = {
    # FMHY's own pages and infrastructure - no ads, no value in scanning.
    "fmhy.net", "fmhy.pages.dev", "api.fmhy.net", "www.fmhy.net",
    # Code hosts / package registries that appear constantly in the index.
    "github.com", "www.github.com", "gist.github.com", "raw.githubusercontent.com",
    "gitlab.com", "codeberg.org", "sourceforge.net", "f-droid.org",
    "play.google.com", "apps.apple.com", "addons.mozilla.org",
    "chromewebstore.google.com", "chrome.google.com", "microsoft.com",
    "apps.microsoft.com", "web.archive.org", "archive.org",
    "discord.com", "discord.gg", "t.me", "reddit.com", "www.reddit.com",
    "old.reddit.com", "redd.it", "en.wikipedia.org", "wikipedia.org",
    "youtube.com", "www.youtube.com", "youtu.be", "x.com", "twitter.com",
    "greasyfork.org", "pypi.org", "npmjs.com", "hub.docker.com",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Host index
# ---------------------------------------------------------------------------


def build_host_index(page_filter: str | None, starred_only: bool) -> dict:
    """host -> {refs, starred, pages[], url (representative), title}"""
    if not LINKS_FILE.exists():
        sys.exit(f"{LINKS_FILE} not found. Run this from the repo root.")

    records = json.loads(LINKS_FILE.read_text(encoding="utf-8"))
    url_counts: dict[str, Counter] = defaultdict(Counter)
    meta: dict[str, dict] = {}

    def consider(url: str, rec: dict):
        if not url or "://" not in url:
            return
        p = urlparse(url)
        if p.scheme.lower() in SKIP_SCHEMES or p.scheme.lower() not in ("http", "https"):
            return
        host = (p.hostname or "").lower().lstrip(".")
        if not host or "." not in host:
            return
        if host in SKIP_HOSTS or host.endswith(SKIP_HOST_SUFFIXES):
            return

        m = meta.setdefault(host, {"refs": 0, "starred": 0, "pages": set(),
                                   "topics": set(),
                                   "title": rec.get("title") or host})
        m["refs"] += 1
        if rec.get("starred"):
            m["starred"] += 1
        if rec.get("page"):
            m["pages"].add(rec["page"])
            m["topics"].add(topic_for(rec["page"], rec.get("section")))
        url_counts[host][url] += 1

    for rec in records:
        if page_filter and rec.get("page") != page_filter:
            continue
        if starred_only and not rec.get("starred"):
            continue
        consider(rec.get("url", ""), rec)
        for sub in rec.get("all_links") or []:
            consider(sub.get("url", ""), rec)

    index = {}
    for host, m in meta.items():
        # Representative URL: the one FMHY cites most often for this host;
        # shortest wins ties, which biases toward the homepage.
        best = sorted(url_counts[host].items(), key=lambda kv: (-kv[1], len(kv[0])))
        index[host] = {
            "refs": m["refs"],
            "starred": m["starred"],
            "pages": sorted(m["pages"]),
            "topics": sorted(m["topics"]),
            "title": m["title"][:80],
            "url": best[0][0] if best else f"https://{host}/",
        }
    return index


# ---------------------------------------------------------------------------
# Queue selection
# ---------------------------------------------------------------------------


def build_queue(index: dict, state: dict, limit: int) -> list[tuple[str, dict]]:
    fresh, due, retry = [], [], []
    now = now_utc()

    for host, info in index.items():
        st = state.get(host)
        if not st:
            fresh.append((host, info))
            continue

        last = parse_ts(st.get("checked_utc"))
        fails = st.get("fails", 0)
        if last is None:
            fresh.append((host, info))
        elif fails >= MAX_FAILS:
            if now - last > timedelta(days=FAIL_BACKOFF_DAYS):
                retry.append((host, info))
        elif st.get("ok") is False:
            if now - last > timedelta(days=7):
                retry.append((host, info))
        elif now - last > timedelta(days=RECHECK_DAYS):
            due.append((host, info))

    # Most-referenced hosts get rated first so the app has useful coverage early.
    fresh.sort(key=lambda kv: (-kv[1]["starred"], -kv[1]["refs"]))
    due.sort(key=lambda kv: parse_ts(state[kv[0]].get("checked_utc")) or now)
    retry.sort(key=lambda kv: -kv[1]["refs"])

    return (fresh + due + retry)[:limit]


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def _is_challenge(r: Result) -> bool:
    """A page that is a bot wall rather than the real site."""
    if r.status in CHALLENGE_STATUSES:
        return True
    title = (r.title or "").lower()
    if any(m in title for m in CHALLENGE_MARKERS):
        return True
    # A page with no title, no ads and almost no weight is usually an
    # interstitial rather than a genuinely clean site.
    if not title and r.page_kb < 8 and r.total_requests < 4:
        return True
    return False


async def scan(queue: list[tuple[str, dict]], concurrency: int, timeout: int,
               scroll: bool, budget_s: int = 0) -> dict[str, Result]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("pip install playwright && playwright install chromium")

    out: dict[str, Result] = {}
    sem = asyncio.Semaphore(concurrency)
    done = 0
    total = len(queue)
    deadline = time.time() + budget_s if budget_s else None
    stopped_early = False

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            # Never let a scanned page start a download on the runner.
            "--deny-permission-prompts",
        ])
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="en-GB",
            ignore_https_errors=True,
            accept_downloads=False,
        )
        context.set_default_timeout(timeout * 1000)

        async def worker(host: str, info: dict):
            nonlocal done, stopped_early
            async with sem:
                # Once the budget is spent, drain the rest of the queue without
                # scanning so the run commits what it has and exits cleanly,
                # rather than being killed mid-write by the CI timeout.
                if deadline and time.time() > deadline:
                    stopped_early = True
                    return
                try:
                    res = await analyse_browser(context, info["url"], timeout, scroll)
                except Exception as e:
                    res = Result(url=info["url"],
                                 error=f"{type(e).__name__}: {str(e)[:100]}")
                    res = score_result(res)

                if res.ok and _is_challenge(res):
                    res.ok = False
                    res.error = "blocked: bot challenge page"

                out[host] = res
                done += 1
                tag = res.grade if res.ok else "ERR"
                print(f"  [{done}/{total}] {tag:>3} {res.score:>5}  "
                      f"ads={res.ad_requests:<4} slots={res.ad_slots:<3} "
                      f"pop={res.popups}  {host[:45]}", flush=True)

        try:
            await asyncio.gather(*(worker(h, i) for h, i in queue))
        finally:
            # Close in a finally so a hung task cannot leave the browser alive.
            try:
                await context.close()
            except Exception:
                pass
            try:
                await browser.close()
            except Exception:
                pass

    if stopped_early:
        print(f"\nStopped on time budget after {len(out)} hosts "
              f"({total - len(out)} left in queue for next run).")

    return out


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def merge_state(state: dict, index: dict, results: dict[str, Result]) -> dict:
    stamp = now_utc().isoformat(timespec="seconds")
    for host, r in results.items():
        prev = state.get(host, {})
        fails = prev.get("fails", 0)
        fails = fails + 1 if not r.ok else 0

        state[host] = {
            "checked_utc": stamp,
            "ok": r.ok,
            "fails": fails,
            "error": r.error,
            "url": r.url,
            "title": r.title or index.get(host, {}).get("title", ""),
            "score": r.score,
            "grade": r.grade,
            "verdict": r.verdict,
            "ad_requests": r.ad_requests,
            "ad_domains": r.ad_domains,
            "ad_slots": r.ad_slots,
            "sticky_ads": r.sticky_ads,
            "popups": r.popups,
            "trackers": r.tracker_requests,
            "ad_weight_pct": r.ad_weight_pct,
            "page_kb": r.page_kb,
            "networks": r.ad_domain_list[:12],
            "refs": index.get(host, {}).get("refs", prev.get("refs", 0)),
            "starred": index.get(host, {}).get("starred", prev.get("starred", 0)),
            "pages": index.get(host, {}).get("pages", prev.get("pages", [])),
            "topics": index.get(host, {}).get("topics", prev.get("topics", [])),
        }
    return state


def write_outputs(state: dict, index: dict):
    DATA.mkdir(exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1, sort_keys=True),
                          encoding="utf-8")

    # Compact app-facing map. Short keys keep the download small for the app:
    #   s=score  g=grade  a=ad requests  n=ad slots  p=popups  k=sticky
    #   r=how many FMHY entries point here  c=FMHY categories  d=date checked
    compact = {}
    for host, st in state.items():
        if not st.get("ok"):
            continue
        compact[host] = {
            "s": st["score"], "g": st["grade"],
            "a": st["ad_requests"], "n": st["ad_slots"],
            "p": st["popups"], "k": st["sticky_ads"],
            "r": st.get("refs", 0),
            "c": (index.get(host, {}).get("topics")
                  or st.get("topics")
                  or st.get("pages", []))[:4],
            "d": (st.get("checked_utc") or "")[:10],
        }

    rated = [s for s in state.values() if s.get("ok")]
    RATINGS_FILE.write_text(json.dumps({
        "generated_utc": now_utc().isoformat(timespec="seconds"),
        "source": "https://github.com/HammerThunderr/FMHY",
        "hosts_in_index": len(index),
        "hosts_rated": len(compact),
        "coverage_pct": round(len(compact) / max(1, len(index)) * 100, 1),
        "average_score": round(sum(s["score"] for s in rated) / len(rated), 1)
                         if rated else 0,
        "legend": {"s": "score 0-100", "g": "grade A+..F", "a": "ad requests",
                   "n": "ad slots", "p": "popups", "k": "sticky ads",
                   "r": "FMHY references", "c": "FMHY categories",
                   "d": "date checked"},
        "hosts": compact,
    }, separators=(",", ":"), sort_keys=True), encoding="utf-8")

    cols = ["host", "grade", "score", "verdict", "ad_requests", "ad_domains",
            "ad_slots", "sticky_ads", "popups", "trackers", "ad_weight_pct",
            "refs", "starred", "pages", "url", "checked_utc", "error"]
    rows = sorted(state.items(), key=lambda kv: (kv[1].get("ok") is not True,
                                                 kv[1].get("score", 0)))
    with open(CSV_FILE, "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.writer(f)
        wr.writerow(cols)
        for host, st in rows:
            wr.writerow([host, st.get("grade", ""), st.get("score", ""),
                         st.get("verdict", ""), st.get("ad_requests", ""),
                         st.get("ad_domains", ""), st.get("ad_slots", ""),
                         st.get("sticky_ads", ""), st.get("popups", ""),
                         st.get("trackers", ""), st.get("ad_weight_pct", ""),
                         st.get("refs", ""), st.get("starred", ""),
                         " ".join(st.get("pages", [])), st.get("url", ""),
                         st.get("checked_utc", ""), st.get("error", "")])


# ---------------------------------------------------------------------------


def sanitise_state(state: dict) -> int:
    """Retro-flag hosts already scored before challenge detection existed.

    Without this, a Cloudflare wall recorded as a perfect A+ keeps its score
    until its 30-day recheck, sitting at the top of "Cleanest" the whole time.
    Flipping ok to False drops it from the ratings and requeues it for a retry.
    """
    fixed = 0
    for host, st in state.items():
        if not st.get("ok"):
            continue
        title = (st.get("title") or "").lower()
        blocked = any(m in title for m in CHALLENGE_MARKERS)
        if not blocked and not title and st.get("page_kb", 0) < 8:
            blocked = True
        if blocked:
            st["ok"] = False
            st["error"] = "blocked: bot challenge page"
            fixed += 1
    return fixed


def main():
    ap = argparse.ArgumentParser(description="Batch ad-rate the FMHY link index.")
    ap.add_argument("--limit", type=int, default=250,
                    help="hosts to scan this run (default 250)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--timeout", type=int, default=35)
    ap.add_argument("--page", help="only hosts from one FMHY page, e.g. Streaming")
    ap.add_argument("--starred-only", action="store_true")
    ap.add_argument("--no-scroll", action="store_true")
    ap.add_argument("--budget", type=int, default=0,
                    help="stop scanning after N seconds, commit what is done "
                         "(0 = no limit)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    index = build_host_index(args.page, args.starred_only)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) \
        if STATE_FILE.exists() else {}

    cleaned = sanitise_state(state)
    if cleaned:
        print(f"Dropped {cleaned} previously scored bot-challenge pages.\n")

    queue = build_queue(index, state, args.limit)
    rated = sum(1 for s in state.values() if s.get("ok"))
    print(f"Index: {len(index)} hosts | already rated: {rated} "
          f"({rated / max(1, len(index)) * 100:.1f}%) | queued now: {len(queue)}\n")

    if not queue:
        print("Nothing due. Everything is rated and within the re-check window.")
        write_outputs(state, index)
        return

    if args.dry_run:
        for h, i in queue[:40]:
            print(f"  {i['refs']:>4} refs  {'*' if i['starred'] else ' '}  "
                  f"{h[:40]:<42} {i['url'][:60]}")
        print(f"\n(dry run - {len(queue)} queued, nothing scanned)")
        return

    t0 = time.time()
    results = asyncio.run(scan(queue, args.concurrency, args.timeout,
                               not args.no_scroll, args.budget))
    state = merge_state(state, index, results)
    write_outputs(state, index)

    ok = [r for r in results.values() if r.ok]
    total_rated = sum(1 for s in state.values() if s.get("ok"))
    print(f"\nScanned {len(results)} in {time.time() - t0:.0f}s "
          f"({len(ok)} ok, {len(results) - len(ok)} failed)")
    print(f"Coverage now {total_rated}/{len(index)} "
          f"({total_rated / max(1, len(index)) * 100:.1f}%)")
    if ok:
        worst = sorted(ok, key=lambda r: r.score)[:5]
        print("\nWorst this batch:")
        for r in worst:
            print(f"  {r.grade:<2} {r.score:>5}  {urlparse(r.url).hostname}")


if __name__ == "__main__":
    main()
