# Regulated Casino Homepage Frequency Index

Django + Celery application that scans licensed casino operator homepages once a
night across multiple regulated markets, records which games are merchandised
where, and serves a public dashboard with date-range historical analytics.

The dashboard answers one question: **which games were being pushed on
regulated homepages, in which market, over any period you choose.**

---

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env                      # SCRAPER_MODE=mock by default

python manage.py migrate
python manage.py createsuperuser
python manage.py seed_catalog             # regions, brands, providers, games
python manage.py backfill_history --days 45   # simulated scan history

python manage.py runserver
```

Then in two more terminals:

```bash
celery -A config worker -l info
celery -A config beat -l info --scheduler django_celery_beat.schedulers:DatabaseScheduler
```

Or all of it at once: `docker compose -f deploy/docker-compose.yml up`.

| URL | What it is |
| --- | --- |
| `/` | Public dashboard with GEO + date-range filters |
| `/monitoring/` | Run history, per-brand reliability, scrape log |
| `/admin/index/scraperun/` | **Run All Scrapers Now** button |

---

## 1. Nightly automation

`index/tasks.py` holds the pipeline. Beat fires `run_nightly_pipeline` at
**02:00 UTC**, which runs brand discovery first, then fans out one
`scrape_brand` task per active brand:

```
run_nightly_pipeline                    (beat, 02:00 UTC)
  ├─ discover_all_brands()                sync: one pass per enabled
  │                                        BrandDiscoverySource, per active
  │                                        region — creates any Brand not
  │                                        already tracked by domain
  └─ run_nightly_brand_scraping()          └─ chord(scrape_brand × N active
                                                brands) ─→ finalise_run
```

**Why discovery runs first, and synchronously.** The game-scrape chord reads
`Brand.objects.filter(status=ACTIVE)` when it starts. If discovery ran as a
separate, independently-scheduled task, there'd be a race: sometimes it'd
finish before the scrape read the brand list, sometimes after. Doing it as a
plain synchronous call inside `run_nightly_pipeline` — not `.delay()`-ed —
guarantees ordering: the brand list is settled before anything reads it. It's
cheap enough for this: one page fetch per source, a handful of sources total.

**Activate / disable a brand.** Already modeled — `Brand.status` is
`active` / `paused` / `delisted`. Only `active` brands are scraped for games.
Flip it from the Brands list in admin (bulk actions: Activate, Pause,
Delist) or on the brand's own edit page.

**What discovery actually does.** Each `BrandDiscoverySource` points at a
regulator's public operator registry. By default it reads that page with the
**AI extractor** (`use_ai_extraction=True`) — the raw HTML goes to Claude with
a strict "return JSON matching this schema, only from what's on the page"
prompt, and it comes back with operator name + domain + licence number. Turn
`use_ai_extraction` off on a source to fall back to hand-written CSS selectors
instead (same `selectors` field as before). Either path creates a `Brand` for
anything not already tracked by domain — logged per-source to
`BrandDiscoveryLog`, rolled up per-run to `BrandDiscoveryRun`.

**Game extraction works the same way.** `Brand.use_ai_extraction=True` (the
default) fetches the lobby page (`homepage_url`) and, if the brand has one, a
separate live-casino page (`live_casino_url`), and asks Claude to identify
which games are shown and where (hero carousel / top-pick grid / live
section). Turn it off per brand to use the CSS `selectors` fallback instead.

**Why the AI never writes to the database directly.** Both extractors return
the same plain dataclasses (`Tile`, `Candidate`) the CSS path already
produced. AI output for games still goes through the existing fuzzy title
matcher and lands in `HomepagePlacement` (matched) or `UnmatchedTile`
(unmatched) exactly like a CSS-based scrape — a hallucinated title just fails
to match and sits in the review queue, it can't invent a `Game` row on its
own. AI output for brands still goes through the same domain-dedup check
before a `Brand` gets created.

**Setup.** Set `ANTHROPIC_API_KEY` in `.env`. With it unset, `AI["ENABLED"]`
is `False` and any brand/source with `use_ai_extraction=True` will fail its
scrape with a clear error in `ScrapeLog`/`BrandDiscoveryLog` rather than
silently doing nothing — either add the key or flip `use_ai_extraction` off
for brands you want to keep on CSS selectors.

A newly discovered brand is created **Paused** by default, so nobody scrapes
an unvetted domain for games until someone's looked at it. Set
`Region.auto_activate_discovered_brands` if you trust a region's source
enough to skip that review step.

Manual triggers: the **Discover Brands Now** button on the Brand Discovery
Runs admin list, the **Discover from selected sources now** action on the
Discovery Sources list, or **Run Full Pipeline Now** on the Scrape Runs list
(discovery + game scraping together, same as the nightly schedule).

Each brand task writes `HomepagePlacement` rows plus exactly one `ScrapeLog`
row, and updates `brand.last_checked_at`. Brand failures are recorded, never
raised out of the run, so one dead site cannot cost you the night's data.

Beat runs on UTC deliberately: with brands in Spain, Mexico and Italy, a
local-time schedule would drift twice a year in each market independently.

**Schedule is editable at runtime.** `django_celery_beat`'s DatabaseScheduler
writes the entries in `config/celery.py` into the DB on first boot; after that,
change the crontab in the admin under Periodic Tasks without a redeploy.

**Manual trigger.** Three ways: the green *Run All Scrapers Now* button on the
Scrape Runs changelist, the *Scrape selected brands now* action on the Brands
list, or `run_nightly_brand_scraping.delay(trigger="manual")` from a shell.

**Mock vs live.** `SCRAPER_MODE=mock` (default in DEBUG) generates deterministic
plausible lobbies — same brand and day always give the same page, head titles
appear nearly everywhere, tail titles drift, and about 4% of brand-days fail so
`ScrapeLog` has something to show. `SCRAPER_MODE=live` routes through
`index/scraping/`, which checks `robots.txt` before every fetch, honours any
declared `Crawl-delay`, identifies itself in the User-Agent, and requests one
page per brand per night. If `robots.txt` is unreachable the fetch is treated as
disallowed.

**Regions tracked**

| Code | Market | Regulator |
| --- | --- | --- |
| ES | Spain | DGOJ |
| MX | Mexico | SEGOB |
| IT | Italy | ADM |
| US | United States | State gaming boards |
| CA | Canada | Provincial (e.g. AGCO) |
| CL | Chile | SCJ |
| GR | Greece | HGC |
| DK | Denmark | Spillemyndigheden |
| SE | Sweden | Spelinspektionen |
| RO | Romania | ONJN |

US and Canada don't have one national regulator — gambling is licensed
state/province by state/province (NJDGE, PGCB, MGCB for the US; AGCO for
Ontario, etc.). They're seeded as single regions for now so the GEO filter has
something to select; if you need per-state accuracy, split `Region` into
`US-NJ`, `US-PA`, `CA-ON` and so on the same way `Brand.region` already works.
Chile's private online licensing regime under the SCJ is newer than DGOJ's —
worth confirming a brand's licence status directly rather than trusting the
region label alone.

---

## 2. Date-range analytics

All dashboard numbers are computed live from `HomepagePlacement` inside a window
on `created_at`. Nothing is precomputed per day, so a custom range behaves
exactly like a preset.

Presets: `today`, `last_7_days`, `last_30_days`, `custom` (+ `start_date` /
`end_date`). `resolve_window()` validates input rather than trusting it —
reversed ranges are swapped, future end dates clamped to today, ranges over 365
days truncated, anything unparseable falls back to 7 days.

Per-title metrics inside the window:

| Metric | Meaning |
| --- | --- |
| `days_featured` | Distinct days the title appeared on at least one homepage |
| `peak_brand_count` | Most operators featuring it on any single day |
| `brand_count` | Distinct operators over the whole window |
| `consistency` | `days_featured / period_days` |
| `geo_codes` | Markets it appeared in |
| `frequency_score` | Coverage 45% + consistency 30% + placement quality 25% |

Trend badges compare rank against the **immediately preceding window of equal
length** — a 7-day view compares to the 7 days before it, a 12-day custom range
to the 12 days before it. Titles absent from the previous window are marked
`new` rather than given a fake climb.

`created_at` is indexed on its own and paired with `brand_id` and `game_id`,
which is what the range scans actually use. `detected_at` is kept separate from
`created_at` so a backfill cannot silently rewrite a historical period — the
dashboard always filters on write time.

---

## 3. HTMX wiring

One form drives everything. Both partials read the same query params, so
`hx-include="#filters"` is all the coordination needed:

```html
<form id="filters" hx-get="/partial/game-table/" hx-target="#matrix-wrap"
      hx-trigger="change, keyup changed delay:350ms from:#q" hx-push-url="true">
```

- `GET /partial/game-table/` → the matrix
- `GET /partial/stats/` → hero cards + category bar
- `GET /partial/game/<slug>/` → expanded row, loaded on click

`hx-push-url` keeps filter state in the address bar, so a "Spain, last 30 days,
crash games" view is a shareable link. A `htmx:configRequest` listener strips
`start_date` / `end_date` unless the period is `custom`, keeping preset URLs
clean.

---

## Project layout

```
config/            settings, celery app + beat schedule, urls
index/
  models.py        Region, Brand, Game, HomepagePlacement, ScrapeLog, ScrapeRun
  tasks.py         nightly pipeline, mock + live scrapers
  views.py         dashboard + HTMX partials + monitoring
  admin.py         Run All Scrapers Now, review queues
  services/
    analytics.py   window resolution, aggregation, trend comparison
  scraping/        robots gate, fetcher, extractor, fuzzy title matcher
  management/commands/
    seed_catalog.py      regions, brands, games, aliases
    backfill_history.py  simulated scan history for the date filters
templates/index/   dashboard, partials, monitoring
```

---

## Before pointing this at live sites

- Read each operator's terms and `robots.txt`. `RESPECT_ROBOTS=1` is the
  default and should stay on.
- Keep the request rate at one homepage per brand per night. The schedule
  already assumes this; concurrency is capped and rate-limited per task.
- Spain's Royal Decree 958/2020 restricts gambling advertising. A public page
  naming licensed operators can read as promotion depending on presentation —
  worth a compliance review before launch, particularly if you add operator
  links or bonus information.
- `Region.legal_notice_*` and `Region.help_line` render in the footer per
  market. Fill them in for every region you add.
