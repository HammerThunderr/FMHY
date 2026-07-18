# fmhy-data

Auto-updating structured export of the [FMHY](https://fmhy.net/) wiki.

A GitHub Action clones the FMHY GitHub wiki (the source markdown — the site and
repo host no files themselves), parses every link into JSON, and commits the
result back to `data/` daily. No API keys, no third-party Python packages, no
billing — runs entirely on the free GitHub Actions tier for public repos.

## Output (`data/`)

| File          | Contents                                                        |
|---------------|-----------------------------------------------------------------|
| `fmhy.json`   | Nested: `page -> [entries]`, each with section/subsection headings |
| `links.json`  | Flat list — one record per entry, easiest to load into an app/DB |
| `meta.json`   | `generated_utc`, page/entry/URL counts                          |

### Entry schema (`links.json`)

```json
{
  "page": "Adblock",
  "section": "Adblocking",
  "subsection": "Adblock Filters",
  "subsubsection": null,
  "title": "uBlock Origin",
  "url": "https://github.com/gorhill/uBlock",
  "description": "Adblockers",
  "starred": true,
  "third_party_index": false,
  "section_link": false,
  "all_links": [{ "text": "uBlock Origin", "url": "https://github.com/gorhill/uBlock" }]
}
```

`starred` = ⭐ community recommendation, `third_party_index` = 🌐, `section_link` = ↪️.

## Consuming the data

Once pushed, each file is fetchable raw — same pattern you'd point a Flutter
fetcher at:

```
https://raw.githubusercontent.com/<you>/fmhy-data/main/data/links.json
```

Optionally enable **Settings → Pages** (deploy from `main`) to also serve it at
`https://<you>.github.io/fmhy-data/data/links.json`.

## Setup

1. Create a repo, drop these files in.
2. **Settings → Actions → General → Workflow permissions →** "Read and write".
3. **Actions** tab → *Update FMHY data* → *Run workflow* to seed `data/`.

After that it refreshes daily on its own.

## Run locally

```bash
git clone --depth 1 https://github.com/fmhy/FMHY.wiki.git _wiki
python scrape.py _wiki data
```
