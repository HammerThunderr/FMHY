#!/usr/bin/env python3
"""
FMHY scraper.

Reads the FMHY GitHub wiki markdown (cloned into ./_wiki by the workflow) and
emits structured JSON into ./data:

  data/fmhy.json    - full nested structure: page -> entries (with headings)
  data/links.json   - flat list of every link (one record per URL)
  data/meta.json    - counts + generation timestamp

No third-party dependencies. The FMHY wiki and repo host no files; this only
indexes the publicly published list of links.
"""
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

WIKI_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("_wiki")
OUT_DIR = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("data")

# Meta / non-content pages to skip. Remove any you actually want indexed.
SKIP_PAGES = {"Home", "Backups", "FMHY-Discord", "FMHY‐Notes.md", "Stream-Site-Grading"}

LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[\*\-]\s+(.*)$")


def clean_text(s: str) -> str:
    """Strip markdown emphasis and FMHY marker glyphs from a label/heading."""
    s = re.sub(r"\*\*|__|`", "", s)          # bold / code ticks
    s = re.sub(r"[►▷◄»]", "", s)             # section arrows
    s = s.replace("⭐", "").replace("🌐", "").replace("↪️", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -–—*")


def parse_page(path: Path):
    page = path.stem
    section = subsection = subsubsection = None
    entries = []

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if not line or set(line) <= {"*", " ", "-"}:  # divider rows like *** ---
            continue

        h = HEADING_RE.match(line)
        if h:
            level, title = len(h.group(1)), clean_text(h.group(2))
            if not title:
                continue
            if level == 1:
                section, subsection, subsubsection = title, None, None
            elif level == 2:
                subsection, subsubsection = title, None
            else:
                subsubsection = title
            continue

        b = BULLET_RE.match(line)
        text = b.group(1) if b else line
        links = LINK_RE.findall(text)
        if not links:
            continue

        primary_label, primary_url = links[0]
        if "back to wiki index" in primary_label.lower():
            continue
        # description = text after the first link's closing paren, tidied
        after = text.split(f"]({primary_url})", 1)[-1]
        desc = clean_text(re.sub(LINK_RE, "", after)).lstrip("-/ ").strip()

        entries.append({
            "page": page,
            "section": section,
            "subsection": subsection,
            "subsubsection": subsubsection,
            "title": clean_text(primary_label),
            "url": primary_url,
            "description": desc or None,
            "starred": "⭐" in text,
            "third_party_index": "🌐" in text,
            "section_link": "↪️" in text,
            "all_links": [{"text": clean_text(t), "url": u} for t, u in links],
        })
    return entries


def main():
    if not WIKI_DIR.exists():
        sys.exit(f"Wiki dir not found: {WIKI_DIR.resolve()}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    nested, flat = {}, []
    for md in sorted(WIKI_DIR.glob("*.md")):
        if md.stem in SKIP_PAGES:
            continue
        entries = parse_page(md)
        if not entries:
            continue
        nested[md.stem] = entries
        flat.extend(entries)

    ts = datetime.now(timezone.utc).isoformat()
    meta = {
        "generated_utc": ts,
        "source": "https://github.com/fmhy/FMHY/wiki",
        "pages": len(nested),
        "entries": len(flat),
        "total_urls": sum(len(e["all_links"]) for e in flat),
    }

    (OUT_DIR / "fmhy.json").write_text(json.dumps(nested, ensure_ascii=False, indent=2))
    (OUT_DIR / "links.json").write_text(json.dumps(flat, ensure_ascii=False, indent=2))
    (OUT_DIR / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    print(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
