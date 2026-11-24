# onsvisualjournalism-backend
Data processing for the ONS Visual Journalism catalogue of releases.

See [PIPELINE.md](PIPELINE.md) for the end-to-end guide for users and AI agents,
including the data contract, operating instructions and known limitations.

## Pipeline Configuration

[catalogue.ipynb](catalogue.ipynb) is the single pipeline for local and GitHub
Actions runs. It reads [config.json](config.json) from the repository root:

```json
{
    "discovery": {
        "mode": "new",
        "limit": null,
        "url": null,
        "start_date": null
    },
    "catalogue": {
        "mode": "new",
        "limit": null
    },
    "screenshots": {
        "mode": "off",
        "limit": null,
        "url": null,
        "start_date": null
    }
}
```

Paste the whole object above into `config.json`, replacing the previous format.
JSON uses `null`, not `undefined`, for unset values. All settings are optional;
omitted settings use the defaults above. `null` limit means no article-count
limit within the selected mode. A positive integer caps that stage's work.

Discovery maintains the source list in [articles-and-dvcs.json](articles-and-dvcs.json).
Catalogue builds the enriched output in [data.json](data.json). Screenshots saves
whole-visualisation JPGs and a linking index under `screenshots`. All three limits are
independent. Turning a stage off leaves its output untouched. If configuration
is excluded from an AI agent's access, apply these settings manually.

| `discovery.mode` | Work performed |
| --- | --- |
| `off` | Do not search or refresh source publications |
| `new` | Find publication URLs not already in the source cache, scanning since the last checkpoint with a seven-day overlap |
| `recent` | Refresh `limit` distinct publication URLs encountered through newest-first releases; a positive limit is required |
| `all` | Refresh all known publication URLs and discover new ones in the checkpoint window |
| `datestart` | Find publications dated strictly after `start_date`, e.g. `"2025-01-01"`; no end date |
| `url` | Scrape the single ONS publication URL pasted into `url`, without release search |

`url` and `start_date` are used only by their matching mode; leave them `null`
otherwise. In `url` mode the stage handles exactly one publication. A recent
release may link older documents, so discovery `recent` is a sample from recent
release links, not a guaranteed ranking of the newest publication dates.
`all` refreshes known history, not every publication ever released by ONS.

| `catalogue.mode` | Work performed |
| --- | --- |
| `off` | Do not read or write the catalogue |
| `new` | Add missing articles and process changed or freshly discovered/refreshed source records; retain other output |
| `recent` | Reprocess the newest `limit` cached publications by publication date; a positive limit is required |
| `all` | Rebuild the catalogue from all cached publications, reusing saved edition and chart metadata |

Catalogue `recent` and `all` regenerate output; they do not automatically
rescrape source pages. Missing metadata is fetched when needed. Valid saved
nulls are reused. For schema-only changes, use discovery `off` and catalogue
`all`: with complete saved metadata this needs no HTTP requests.
When discovery explicitly refreshes a publication and catalogue processes it
in the same run, its edition and chart metadata are refreshed too.
Limited runs preserve all unprocessed output. A complete catalogue `all` rebuild
replaces the output from the source cache. Local fields and `latest` flags are
always recalculated over the complete resulting catalogue.

### Screenshots

| `screenshots.mode` | Work performed |
| --- | --- |
| `off` | Do not read/write screenshot outputs or start a browser; the default |
| `new` | Capture missing images and retry failed captures, including failed refreshes with an older image |
| `recent` | Recapture all charts from the newest `limit` eligible cached publications; positive limit required |
| `all` | Recapture all eligible cached publications, subject to `limit` |
| `datestart` | Recapture publications strictly after `start_date`, subject to `limit` |
| `url` | Recapture one cached publication identified by its absolute ONS publication `url` |

Limits count **publications**, not charts. Every selected publication contributes
all its eligible visualisations; `new` captures only pending charts. Selection
uses the source cache after discovery/catalogue processing, newest publication
first, with URL as a tie-breaker. Shared visualisation URLs are captured once.
Screenshot selection does not search releases or advance the discovery checkpoint.
In `url` mode, use a publication URL, not a chart URL; if it is not cached, run
discovery `url` first or alongside screenshots. Existing successful images are
not automatically refreshed when content changes at the same URL: use `recent`,
`all`, `datestart` or `url`.

Capture the complete visualisation container, not just its SVG/canvas. Use the
chart's wrapping div, expanding to a common ancestor when legends, controls,
source, notes or other chart panels are siblings. Fall back to the visualisation
page body when no wrapping div contains them. Keep padding, headings, legends,
source, notes, map attribution and visible interactive controls as rendered;
do not open hidden panels or enumerate interactive states. Unsupported layouts
are reported rather than silently saving an empty chart.

The browser starts at **700px wide**. If the full container is already 700px tall (within
one output pixel), its width is unchanged. Otherwise a bounded coarse-to-fine
search samples responsive widths from **320px to 700px**, including breakpoints
in that search. The complete chart closest to 700px tall among measured layouts
is used, preferring wider layouts on a tie. The height target includes legends,
controls and source/notes, not only the plotted area. Some charts get shorter when narrowed
and cannot reach the target. Nothing is stretched, scaled or cropped to force it;
JPG width never exceeds 700px. Charts reload at each trial width because some
standalone ONS embeds do not redraw on viewport resize. Fonts, layout, pixels and
available map-ready state are checked before capture.
The first layout is reloaded with warm fonts so SVG legend dimensions do not
depend on fallback-font measurements from a cold navigation.

`screenshots/index.json` has `version: 1` and a `charts` object keyed by absolute
visualisation URL. Each entry contains:

- `filename`: normally `dvc3682-fig01.jpg`, using the DVC and figure numbers
  from the URL, normalised to lowercase. If another URL or an unknown existing
  file owns that name, append eight SHA-256 characters from the full URL, such as
  `dvc3682-fig01-a1b2c3d4.jpg`. A further collision adds `-2`, `-3`, etc.
  Query strings, fragments and case differences remain distinct URL identities.
  URLs without a DVC/figure pair use a readable path-based stem with the same
  collision protection. Existing indexed filename assignments remain stable.
- `publications`: all cached publication URLs referencing that visualisation.
- `last_success`: actual `width`, `height`, `viewport_width`, UTC `captured_at`
  and boolean `target_reached`, or `null` if it has never succeeded.
- `last_attempt`: UTC `at`, `status` (`success` or `failed`) and an `error` on
  failure. A failed refresh retains the previous image and success metadata.

Join the index keys to absolute catalogue `vis[].url` values (resolve relative
paths against `https://www.ons.gov.uk`). The catalogue schema is unchanged.
Partial runs preserve unselected images and entries; unused images are not pruned.
Enabled screenshot runs migrate the older long hashed filenames and update the
index without changing image bytes. Legacy files left without metadata by an
interrupted run are linked back to their cached URLs and explicitly marked for
recapture; no successful capture metadata is invented. Screenshot-off does not
perform migration.
Images captured with the earlier plot-only boundary are not automatically
invalidated; use a recapture mode to replace them with full-container captures.

Each failed chart is logged while other charts continue. Successful JPGs and the
index entries are saved after each completed capture attempt, so filename
ownership survives later interruptions. The notebook raises a consolidated failure. Actions
commits these partial results before marking the run failed. Invalid index data,
browser startup failures and filesystem failures remain fatal. JPGs and the index
are replaced atomically per file, not as one transaction: an interrupted run can
leave an image ahead of its index entry. Retry `new` for missing/failed entries;
explicitly recapture the affected publication after a fatal interrupted refresh.
Images are committed to Git, which increases repository size over time.

Install the browser once into the environment used by the notebook:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

GitHub Actions installs Chromium and its system dependencies when enabled.
Chromium is not needed for discovery/catalogue-only runs.

### Common Runs

Rebuild the catalogue after a data-structure change, without source discovery:

```json
{
    "discovery": {"mode": "off"},
    "catalogue": {"mode": "all", "limit": null}
}
```

Capture charts from the newest five cached publications without changing the
source cache, catalogue or checkpoint:

```json
{
    "discovery": {"mode": "off"},
    "catalogue": {"mode": "off"},
    "screenshots": {"mode": "recent", "limit": 5}
}
```

For ongoing missing-image capture, set `screenshots.mode` to `new`. For a single
cached publication, set it to `url` and paste the publication URL into
`screenshots.url`. Keep the limit `null` unless deliberately sampling.

Other runs only need a few changes to the default object:

- Refresh a recent sample: set discovery mode to `recent` and its limit to `50`.
    Leave catalogue mode as `new` to process the refreshed records.
- Find everything after a day: set discovery mode to `datestart` and
    `start_date` to a date such as `"2025-01-01"`. Leave its limit `null` for all
    matching articles available through the release API.
- Capture one article: set discovery mode to `url` and paste its full ONS
    article/bulletin/methodology URL into `url`. Catalogue `new` adds or updates it.
- Refresh known history and rebuild: set both modes to `all`, with both limits
    `null`. This is intentionally a longer live source refresh.

Keep the null fields in place when switching modes. Zero, negative, boolean or
string limits are invalid. Old top-level `mode`/`update_limit`, `scope`,
`metadata`, `urls`, and date-range settings are no longer accepted.

### Automation and Safety

[ons_discovery.py](ons_discovery.py) queries the ONS release search API directly,
follows publication links and maintains [articles-and-dvcs.json](articles-and-dvcs.json)
as the local history cache. The screenshot repository is no longer downloaded.
The optional [ons_screenshots.py](ons_screenshots.py) stage generates JPGs using
Playwright Chromium. Original PNG visualisation links remain excluded.
SVG and JPEG publication assets are retained.

`discovery-state.json` records the last successfully scanned published-release
day. Only completed `new` and `all` scans advance it. A scan stopped by its
article limit retains the old checkpoint; on a first limited run the starting
day is saved so later runs do not skip unfinished discovery. `recent`,
`datestart`, `url` and `off` leave the checkpoint untouched. Incremental scans
have an internal page safety cap and fail rather than silently skip work;
explicit `datestart` scans continue until they reach the requested date or exhaust
results. Historical discovery is limited by the ONS API's available coverage.

GitHub Actions uses the same configuration without an override. Restore your
normal settings after a one-off run; the notebook does not reset configuration.
After pasting the new format locally, commit `config.json` so the automated
pipeline uses the same settings. No separate workflow configuration is needed.

Discovery and catalogue use shared fetching, merge and enrichment code. Only enabled or
changed artifacts are written; catalogue-off leaves output untouched. Catalogue
enrichment can update the source cache even with discovery off. All selected
JSON writes are staged before publication. Catalogue and cache are replaced first,
with the discovery checkpoint last. Each file replacement is atomic, but the
three-file update is not a transaction: a replacement failure can leave newer
data with the older checkpoint, which is replayed on the next successful run.
Discovery, request, parse or serialization failures before publication leave the
existing files untouched.
The independent screenshot stage runs after those JSON writes; a screenshot
failure does not roll back a successful discovery/catalogue update.

The pipeline identifies releases by `doc_uri`, preserving different releases
that share a title. A repeated URL refreshes the same release instead of adding
a duplicate. Cached duplicate URLs prefer the newest publication date. Publication
HTML is fetched once per URL per run and reused for title, date, embed and edition
extraction. Publications with empty `vis_urls` after PNG filtering are excluded
from the output, including selected records already present in the catalogue.
Recovering historical releases collapsed by the previous title-based logic
requires retaining the history cache and using catalogue mode `all`.

## Theme Hierarchy

The pipeline derives three hierarchy fields locally from `doc_uri`:

- `theme`: the top-level theme, such as `employmentandlabourmarket`.
- `topic`: the second level, such as `peopleinwork`.
- `subtopic`: the third level, such as `employmentandemployeetypes`, or `null`
	when the URL has only two levels.

The fields retain the original URL slugs. Only path segments before `articles`,
`bulletins` or `methodologies` are taxonomy levels; publication types and article
slugs are excluded. For example, `economy/grossdomesticproductgdp/bulletins/...`
has `theme: economy`, `topic: grossdomesticproductgdp` and `subtopic: null`.

All output records are enriched before writing, including retained records in
incremental updates. This needs no additional page requests. Unsupported paths
without a recognised publication boundary or with more than three hierarchy
levels stop the run before output is written rather than silently mislabelling
the fields. Missing levels are `null`.

## Release Titles and Series

The pipeline extracts `title` from the publication heading and derives `series` by removing a
recognised trailing release date and its preceding colon or comma. Supported
suffixes include years, month/day dates, date ranges, `year ending` periods,
compound ranges and `mid-YYYY`. Descriptive colons and embedded years are retained.
Years in narrative titles such as "nature since 2020" are also retained.

All enabled catalogue modes enrich the final output before writing, including
existing records outside an update batch. `series` is a grouping field, not a merge key.
Date removal does not reconcile historical changes in series wording.

The pipeline uses `vis_urls` from the source input to fetch visualisations, then
removes it from every output record before writing `data.json`. The `vis` array
retains each visualisation URL and its chart type. Final processing also removes
`vis_urls` from older records already in the output file.

## Release Editions

When publication pages are fetched, the pipeline reuses that HTML to add an
`edition` field for eligible releases. Otherwise it uses matching saved metadata.
No second edition-only request is needed.

The extractor reads the main heading, taking the text after the known title
when present, or a recognised trailing date expression otherwise. For example,
`GDP monthly estimate, UK: July 2025` yields `July 2025`. Descriptive colons are
not split indiscriminately. The existing `title` and `series` are unchanged.

If no edition is found, the value is `null`, not the publication date. Requests
have a 30-second timeout, bounded retries for transient errors, and pacing between
uncached requests. HTTP errors stop the run before generated files are published.
Saved nulls are treated as completed extraction results, not automatically
retried. Use source discovery `recent`, `all` or `url` to refetch publications
when edition extraction needs to see their HTML again. `doc_uri` remains the
release identifier.

## Latest Releases

Every output record has a boolean `latest` field. Catalogue runs recompute it
across the complete output immediately before writing, including older records
outside an update batch.

Within each exact `series` value, records with the newest `release_date` are
`latest: true`; all others are `false`. Edition labels are not used, including
when they are missing. A single-entry series is always latest, and all records
tied on the newest publication day are latest.

ISO dates and timestamps are parsed using the calendar date recorded in
`release_date`; time of day and timezone offsets do not shift that date.
Missing or invalid publication dates stop the run before output is written.
Latest means newest in this dataset, not necessarily the latest release on ONS.

