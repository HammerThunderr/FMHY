#!/usr/bin/env python3
"""
ad_rater.py - Load a list of URLs, count the ads on each page, score and rank them.

Two modes:
  browser (default, accurate) : real Chromium via Playwright. Catches ads injected
                                by JavaScript, counts every ad-network request,
                                measures how much of the page weight is ads.
  static  (fast, rough)       : plain HTML fetch. Only sees ad code baked into the
                                source. Much faster, misses lazy-loaded ads.

Install:
    pip install playwright requests
    playwright install chromium

Usage:
    python ad_rater.py links.txt
    python ad_rater.py links.txt --mode static
    python ad_rater.py links.txt --concurrency 4 --timeout 45 --out results
    python ad_rater.py links.txt --headed          # watch it work

Input file: one URL per line. Blank lines and lines starting with # are ignored.
            A CSV export works too - anything that looks like a URL is picked up.

Output:   results.csv, results.json  (sorted best -> worst)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# 1. AD NETWORK DOMAINS
# Any request whose hostname contains one of these counts as an ad request.
# Add or remove entries freely - this list drives most of the score.
# ---------------------------------------------------------------------------

AD_DOMAINS = [
    # Google / programmatic core
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "googletagservices.com", "adservice.google.", "2mdn.net", "googletagmanager.com",
    # Big exchanges / SSPs
    "adnxs.com", "appnexus.com", "rubiconproject.com", "pubmatic.com", "openx.net",
    "casalemedia.com", "indexww.com", "adform.net", "smartadserver.com",
    "adsrvr.org", "bidswitch.net", "3lift.com", "triplelift.com", "sharethrough.com",
    "districtm.io", "sovrn.com", "lijit.com", "yieldmo.com", "gumgum.com",
    "media.net", "amazon-adsystem.com", "adtelligent.com", "onetag-sys.com",
    "rtbhouse.com", "criteo.com", "criteo.net", "teads.tv", "smaato.net",
    "improvedigital.com", "emxdgt.com", "pubnative.net", "adkernel.com",
    # Native / content recommendation
    "taboola.com", "outbrain.com", "mgid.com", "revcontent.com", "zergnet.com",
    "content.ad", "plista.com", "dianomi.com", "adskeeper.com",
    # Aggressive / pop / redirect networks
    "popads.net", "popcash.net", "propellerads.com", "propellerclick.com",
    "adsterra.com", "exoclick.com", "juicyads.com", "hilltopads.net",
    "trafficjunky.net", "adcash.com", "clickadu.com", "monetag.com",
    "onclickalgo.com", "bidgear.com", "adnium.com",
    # Video ads
    "spotxchange.com", "spotx.tv", "springserve.com", "innovid.com",
    "imrworldwide.com", "adsafeprotected.com", "moatads.com", "doubleverify.com",
    "flashtalking.com", "serving-sys.com", "tremorhub.com", "unrulymedia.com",
    # Affiliate / retail media
    "adroll.com", "outbrainimg.com", "awin1.com", "shareasale.com",
    "impact-radius", "linksynergy.com", "skimresources.com", "viglink.com",
    "narrativ.com", "connexity.net",
    # Consent / identity plumbing that only exists to serve ads
    "quantserve.com", "quantcount.com", "scorecardresearch.com", "crwdcntrl.net",
    "demdex.net", "everesttech.net", "bluekai.com", "agkn.com", "adsymptotic.com",
    "id5-sync.com", "pubcommon", "prebid.org", "sekindo.com",
]

# Domains that are tracking/analytics rather than display ads. Counted and
# reported separately so they do not inflate the ad score.
TRACKER_DOMAINS = [
    "google-analytics.com", "analytics.google.com", "facebook.net",
    "connect.facebook.net", "hotjar.com", "mixpanel.com", "segment.io",
    "segment.com", "amplitude.com", "clarity.ms", "matomo", "newrelic.com",
    "sentry.io", "fullstory.com", "mouseflow.com", "luckyorange.com",
    "yandex.ru/metrika", "mc.yandex.ru", "tiktok.com/i18n/pixel", "snap.licdn.com",
]

# ---------------------------------------------------------------------------
# 2. SCORING WEIGHTS
# Every site starts at 100 and loses points. Each category is capped so one
# extreme metric cannot single-handedly bottom out the score.
# Tune these to match how harshly you want to judge.
# ---------------------------------------------------------------------------

WEIGHTS = {
    "per_ad_request":      1.5,   # each network call to an ad domain
    "cap_ad_requests":     30,
    "per_ad_domain":       3.0,   # each distinct ad company involved
    "cap_ad_domains":      25,
    "per_ad_slot":         5.0,   # each ad container found in the DOM
    "cap_ad_slots":        30,
    "per_popup":          15.0,   # pop-up / pop-under windows
    "cap_popups":          15,
    "per_weight_pct":      0.5,   # each 1% of page bytes that is ad content
    "cap_weight":          15,
    "per_tracker":         0.5,   # trackers nudge the score only slightly
    "cap_trackers":        10,
}

GRADE_BANDS = [
    (95, "A+", "Effectively ad-free"),
    (85, "A",  "Very clean"),
    (72, "B",  "Light advertising"),
    (58, "C",  "Moderate advertising"),
    (44, "D",  "Heavy advertising"),
    (28, "E",  "Very heavy advertising"),
    (0,  "F",  "Ad-saturated / hostile"),
]

# ---------------------------------------------------------------------------
# 3. DOM DETECTION (runs inside the page)
# ---------------------------------------------------------------------------

DOM_SCRIPT = r"""
(adDomains) => {
  // Word-boundary-ish match so "download", "shadow", "gradient", "loading"
  // do not get flagged as ads.
  const AD_RE = new RegExp(
    '(^|[^a-z0-9])(ad|ads|adv|advert|adverts|advertising|advertisement|' +
    'adslot|adunit|adbox|adzone|adwrap|adcontainer|adspace|adblock|' +
    'sponsored|sponsor|promoted|dfp|gpt|taboola|outbrain|mgid|revcontent|' +
    'banner-ad|ad-banner|leaderboard|skyscraper|interstitial)' +
    '([^a-z0-9]|$)', 'i'
  );

  const EXPLICIT = [
    'ins.adsbygoogle',
    '[data-ad-slot]', '[data-ad-client]', '[data-ad-unit]', '[data-adunitpath]',
    'iframe[id^="google_ads_iframe"]', 'iframe[name^="google_ads_iframe"]',
    'div[id^="div-gpt-ad"]', 'div[id^="google_ads"]',
    '[id^="taboola"]', '[id^="outbrain"]', '.OUTBRAIN', '[class*="trc_related"]',
    'iframe[src*="doubleclick"]', 'iframe[src*="googlesyndication"]',
    'iframe[src*="amazon-adsystem"]', 'iframe[src*="adnxs"]',
    'amp-ad', 'amp-embed', '[id^="mgid"]', '[class*="mgbox"]',
  ].join(',');

  const matched = new Set();

  // Explicit, high-confidence ad containers
  document.querySelectorAll(EXPLICIT).forEach(el => matched.add(el));

  // Heuristic pass over id / class / data-attributes
  document.querySelectorAll('div,section,aside,iframe,ins,span,ul').forEach(el => {
    const id = el.id || '';
    const cls = (typeof el.className === 'string') ? el.className : '';
    if (AD_RE.test(id) || AD_RE.test(cls)) matched.add(el);
  });

  // Any iframe pointing at a known ad domain
  document.querySelectorAll('iframe').forEach(el => {
    const src = el.getAttribute('src') || el.getAttribute('data-src') || '';
    if (src && adDomains.some(d => src.includes(d))) matched.add(el);
  });

  // Drop nested matches - a wrapper plus its inner iframe is ONE ad slot
  const els = Array.from(matched);
  const topLevel = els.filter(el => !els.some(o => o !== el && o.contains(el)));

  // Only count slots with real on-screen area, or hidden ones that are clearly
  // ad tech (zero-size tracking iframes still count as ad requests, not slots).
  let visible = 0, sticky = 0, largest = 0;
  topLevel.forEach(el => {
    const r = el.getBoundingClientRect();
    const area = r.width * r.height;
    if (area > 1000) {
      visible++;
      if (area > largest) largest = area;
      const pos = getComputedStyle(el).position;
      if (pos === 'fixed' || pos === 'sticky') sticky++;
    }
  });

  const docArea = Math.max(1, document.documentElement.scrollWidth *
                              document.documentElement.scrollHeight);

  return {
    ad_slots: visible,
    ad_slots_raw: topLevel.length,
    sticky_ads: sticky,
    adsbygoogle: document.querySelectorAll('ins.adsbygoogle').length,
    gpt_slots: document.querySelectorAll('div[id^="div-gpt-ad"]').length,
    largest_ad_pct: Math.round((largest / docArea) * 1000) / 10,
    title: document.title || '',
  };
}
"""

# ---------------------------------------------------------------------------
# 4. RESULT MODEL
# ---------------------------------------------------------------------------


@dataclass
class Result:
    url: str
    ok: bool = False
    error: str = ""
    title: str = ""
    status: int = 0
    load_ms: int = 0

    ad_requests: int = 0
    ad_domains: int = 0
    ad_domain_list: list = field(default_factory=list)
    tracker_requests: int = 0
    ad_slots: int = 0
    sticky_ads: int = 0
    adsbygoogle: int = 0
    gpt_slots: int = 0
    popups: int = 0
    total_requests: int = 0
    page_kb: int = 0
    ad_kb: int = 0
    ad_weight_pct: float = 0.0
    largest_ad_pct: float = 0.0

    score: float = 0.0
    grade: str = ""
    verdict: str = ""


def domain_of(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return ""


def match_list(host: str, patterns: list[str]) -> str | None:
    for p in patterns:
        if p in host:
            return p
    return None


# ---------------------------------------------------------------------------
# 5. SCORING
# ---------------------------------------------------------------------------


def score_result(r: Result) -> Result:
    w = WEIGHTS
    penalties = 0.0
    penalties += min(w["cap_ad_requests"], r.ad_requests * w["per_ad_request"])
    penalties += min(w["cap_ad_domains"], r.ad_domains * w["per_ad_domain"])
    penalties += min(w["cap_ad_slots"], r.ad_slots * w["per_ad_slot"])
    penalties += min(w["cap_popups"], r.popups * w["per_popup"])
    penalties += min(w["cap_weight"], r.ad_weight_pct * w["per_weight_pct"])
    penalties += min(w["cap_trackers"], r.tracker_requests * w["per_tracker"])

    # Sticky / floating ads are the most intrusive format - flat extra hit.
    penalties += min(10, r.sticky_ads * 5)

    r.score = round(max(0.0, 100.0 - penalties), 1)
    for cutoff, grade, verdict in GRADE_BANDS:
        if r.score >= cutoff:
            r.grade, r.verdict = grade, verdict
            break
    return r


# ---------------------------------------------------------------------------
# 6. BROWSER MODE
# ---------------------------------------------------------------------------


async def analyse_browser(context, url: str, timeout: int, scroll: bool) -> Result:
    r = Result(url=url)
    page = await context.new_page()
    ad_domains_hit: set[str] = set()
    counters = {"ads": 0, "trackers": 0, "total": 0, "popups": 0,
                "bytes": 0, "ad_bytes": 0}

    def on_request(req):
        counters["total"] += 1
        host = domain_of(req.url)
        if not host:
            return
        if match_list(host, AD_DOMAINS):
            counters["ads"] += 1
            ad_domains_hit.add(host)
        elif match_list(host, TRACKER_DOMAINS):
            counters["trackers"] += 1

    def on_response(res):
        try:
            size = int(res.headers.get("content-length", 0) or 0)
        except (TypeError, ValueError):
            size = 0
        if size <= 0:
            return
        counters["bytes"] += size
        host = domain_of(res.url)
        if host and match_list(host, AD_DOMAINS):
            counters["ad_bytes"] += size

    def on_popup(_p):
        counters["popups"] += 1

    page.on("request", on_request)
    page.on("response", on_response)
    page.on("popup", on_popup)
    page.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

    started = time.time()
    try:
        resp = await page.goto(url, timeout=timeout * 1000,
                               wait_until="domcontentloaded")
        r.status = resp.status if resp else 0

        # Give lazy ad scripts a moment, then scroll to trigger below-the-fold
        # slots. Waits are kept tight: a 5s idle cap and 400ms scroll pauses are
        # enough for the ad networks that matter, and shave ~6s off every host,
        # which is what keeps a 250-batch inside the time budget.
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        if scroll:
            for frac in (0.33, 0.66, 1.0):
                await page.evaluate(
                    "f => window.scrollTo(0, document.body.scrollHeight * f)", frac)
                await page.wait_for_timeout(400)
            await page.evaluate("() => window.scrollTo(0, 0)")
            await page.wait_for_timeout(600)

        dom = await page.evaluate(DOM_SCRIPT, AD_DOMAINS)

        r.ok = True
        r.title = (dom.get("title") or "")[:90]
        r.ad_slots = dom.get("ad_slots", 0)
        r.sticky_ads = dom.get("sticky_ads", 0)
        r.adsbygoogle = dom.get("adsbygoogle", 0)
        r.gpt_slots = dom.get("gpt_slots", 0)
        r.largest_ad_pct = dom.get("largest_ad_pct", 0.0)

    except Exception as e:
        r.error = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        r.load_ms = int((time.time() - started) * 1000)
        r.ad_requests = counters["ads"]
        r.tracker_requests = counters["trackers"]
        r.total_requests = counters["total"]
        r.popups = counters["popups"]
        r.ad_domains = len(ad_domains_hit)
        r.ad_domain_list = sorted(ad_domains_hit)[:25]
        r.page_kb = counters["bytes"] // 1024
        r.ad_kb = counters["ad_bytes"] // 1024
        if counters["bytes"] > 0:
            r.ad_weight_pct = round(
                counters["ad_bytes"] / counters["bytes"] * 100, 1)
        try:
            await page.close()
        except Exception:
            pass

    return score_result(r)


async def run_browser(urls: list[str], concurrency: int, timeout: int,
                      headed: bool, scroll: bool, block_consent: bool) -> list[Result]:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        sys.exit("Playwright not installed.  pip install playwright"
                 "  &&  playwright install chromium\n"
                 "Or run with --mode static for a rough HTML-only check.")

    results: list[Result] = []
    sem = asyncio.Semaphore(concurrency)
    done = 0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed, args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ])
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            locale="en-GB",
            ignore_https_errors=True,
        )
        context.set_default_timeout(timeout * 1000)

        async def worker(u: str):
            nonlocal done
            async with sem:
                res = await analyse_browser(context, u, timeout, scroll)
                done += 1
                flag = res.grade if res.ok else "ERR"
                print(f"  [{done}/{len(urls)}] {flag:>3}  "
                      f"{res.score:>5}  ads={res.ad_requests:<4} "
                      f"slots={res.ad_slots:<3} {u[:60]}")
                results.append(res)

        await asyncio.gather(*(worker(u) for u in urls))
        await context.close()
        await browser.close()

    return results


# ---------------------------------------------------------------------------
# 7. STATIC MODE (no browser)
# ---------------------------------------------------------------------------

STATIC_SLOT_PATTERNS = [
    r"<ins[^>]+adsbygoogle",
    r"data-ad-slot",
    r"data-ad-client",
    r'id=["\']div-gpt-ad',
    r"<amp-ad\b",
    r'class=["\'][^"\']*\b(ad|ads|advert|advertisement|adslot|adunit|sponsored)\b',
    r'id=["\'][^"\']*\b(ad|ads|advert|advertisement|adslot|adunit|sponsored)\b',
]


def analyse_static(url: str, timeout: int) -> Result:
    import requests

    r = Result(url=url)
    started = time.time()
    try:
        resp = requests.get(
            url, timeout=timeout,
            headers={"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                                    "Chrome/124.0.0.0 Safari/537.36")},
        )
        r.status = resp.status_code
        html = resp.text
        r.page_kb = len(resp.content) // 1024
        r.ok = True

        m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
        r.title = (m.group(1).strip()[:90] if m else "")

        low = html.lower()
        hits = set()
        for d in AD_DOMAINS:
            c = low.count(d)
            if c:
                hits.add(d)
                r.ad_requests += c
        for d in TRACKER_DOMAINS:
            r.tracker_requests += low.count(d)
        r.ad_domains = len(hits)
        r.ad_domain_list = sorted(hits)[:25]

        slots = 0
        for pat in STATIC_SLOT_PATTERNS:
            slots += len(re.findall(pat, html, re.I))
        r.ad_slots = slots
        r.adsbygoogle = len(re.findall(r"adsbygoogle", html, re.I))

    except Exception as e:
        r.error = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        r.load_ms = int((time.time() - started) * 1000)

    return score_result(r)


def run_static(urls: list[str], timeout: int) -> list[Result]:
    results = []
    for i, u in enumerate(urls, 1):
        res = analyse_static(u, timeout)
        flag = res.grade if res.ok else "ERR"
        print(f"  [{i}/{len(urls)}] {flag:>3}  {res.score:>5}  "
              f"ads={res.ad_requests:<4} slots={res.ad_slots:<3} {u[:60]}")
        results.append(res)
    return results


# ---------------------------------------------------------------------------
# 8. IO
# ---------------------------------------------------------------------------

URL_RE = re.compile(r"https?://[^\s,;\"'<>\]\)]+", re.I)


def load_urls(path: Path) -> list[str]:
    if not path.exists():
        sys.exit(f"Input file not found: {path}")
    text = path.read_text(encoding="utf-8", errors="ignore")
    urls, seen = [], set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        found = URL_RE.findall(line)
        if not found and "." in line and " " not in line:
            found = ["https://" + line]          # bare domain like example.com
        for u in found:
            u = u.rstrip(".,;")
            if u not in seen:
                seen.add(u)
                urls.append(u)
    if not urls:
        sys.exit(f"No URLs found in {path}")
    return urls


CSV_COLS = ["rank", "grade", "score", "verdict", "url", "title", "status",
            "ad_requests", "ad_domains", "ad_slots", "sticky_ads",
            "adsbygoogle", "gpt_slots", "popups", "tracker_requests",
            "ad_weight_pct", "largest_ad_pct", "page_kb", "ad_kb",
            "total_requests", "load_ms", "error", "ad_domain_list"]


def write_output(results: list[Result], out: str):
    results.sort(key=lambda r: (not r.ok, -r.score))

    with open(f"{out}.csv", "w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=CSV_COLS, extrasaction="ignore")
        wr.writeheader()
        for i, r in enumerate(results, 1):
            row = asdict(r)
            row["rank"] = i
            row["ad_domain_list"] = " | ".join(r.ad_domain_list)
            wr.writerow(row)

    payload = []
    for i, r in enumerate(results, 1):
        d = asdict(r)
        d["rank"] = i
        payload.append(d)
    Path(f"{out}.json").write_text(
        json.dumps({"generated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "count": len(results), "results": payload},
                   indent=2), encoding="utf-8")


def print_table(results: list[Result]):
    ok = [r for r in results if r.ok]
    bad = [r for r in results if not r.ok]

    print("\n" + "=" * 96)
    print(f"{'#':>3}  {'GR':<3} {'SCORE':>6}  {'ADS':>4} {'DOMS':>4} "
          f"{'SLOTS':>5} {'STKY':>4} {'AD%':>5}  URL")
    print("-" * 96)
    for i, r in enumerate(ok, 1):
        print(f"{i:>3}  {r.grade:<3} {r.score:>6}  {r.ad_requests:>4} "
              f"{r.ad_domains:>4} {r.ad_slots:>5} {r.sticky_ads:>4} "
              f"{r.ad_weight_pct:>5}  {r.url[:48]}")
    print("=" * 96)

    if ok:
        avg = sum(r.score for r in ok) / len(ok)
        print(f"\nChecked {len(ok)} pages | average score {avg:.1f}")
        print(f"Cleanest: {ok[0].url[:60]}  ({ok[0].grade}, {ok[0].score})")
        print(f"Worst:    {ok[-1].url[:60]}  ({ok[-1].grade}, {ok[-1].score})")

    if bad:
        print(f"\n{len(bad)} failed:")
        for r in bad:
            print(f"  ! {r.url[:60]} -> {r.error}")


# ---------------------------------------------------------------------------
# 9. MAIN
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description="Count ads on a list of pages and rate them 0-100.")
    ap.add_argument("links", nargs="?", default="links.txt",
                    help="text file with one URL per line (default: links.txt)")
    ap.add_argument("--mode", choices=["browser", "static"], default="browser")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="pages loaded in parallel, browser mode (default 3)")
    ap.add_argument("--timeout", type=int, default=40, help="seconds per page")
    ap.add_argument("--out", default="results", help="output basename")
    ap.add_argument("--headed", action="store_true",
                    help="show the browser window")
    ap.add_argument("--no-scroll", action="store_true",
                    help="skip scrolling (faster, misses lazy-loaded ads)")
    args = ap.parse_args()

    urls = load_urls(Path(args.links))
    print(f"Loaded {len(urls)} URLs from {args.links}  |  mode={args.mode}\n")

    t0 = time.time()
    if args.mode == "browser":
        results = asyncio.run(run_browser(
            urls, args.concurrency, args.timeout,
            args.headed, not args.no_scroll, True))
    else:
        results = run_static(urls, args.timeout)

    write_output(results, args.out)
    print_table(results)
    print(f"\nSaved {args.out}.csv and {args.out}.json  "
          f"({time.time() - t0:.0f}s total)")


if __name__ == "__main__":
    main()
