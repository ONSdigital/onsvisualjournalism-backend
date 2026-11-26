# Pipeline Guide for Users and AI Agents

## Purpose

This repository builds a catalogue of Office for National Statistics (ONS)
publications containing visualisations. It discovers releases directly from ONS
and combines them with locally cached history and fetched page metadata, producing
[data.json](data.json) for downstream consumers such as a catalogue UI.

The catalogue is not an exhaustive list of ONS publications. Eligibility depends
on finding a non-empty `vis_urls` array after PNG and download-link filtering.

## Project Map

| File | Role |
| --- | --- |
| [catalogue.py](catalogue.py) | The single orchestration entry point for independent stages |
| [config.json](config.json) | Selects discovery/catalogue/screenshots modes, limits and optional URL/start date |
| [ons_discovery.py](ons_discovery.py) | Configuration, selection, discovery, metadata reuse, HTTP and safe JSON-writing helpers |
| [ons_screenshots.py](ons_screenshots.py) | Independent 700px-wide whole-visualisation JPG capture and image index |
| `screenshots/` | Generated JPGs and `index.json` mapping visualisations to images and publications |
| [articles-and-dvcs.json](articles-and-dvcs.json) | Locally maintained history cache, initially seeded from upstream |
| [data.json](data.json) | Generated, enriched catalogue |
| `discovery-state.json` | Last successfully scanned published-release day; created by a successful run |
| [requirements.txt](requirements.txt) | Python dependencies |
| [.github/workflows/test.yml](.github/workflows/test.yml) | Executes the pipeline script and commits generated JSON/JPG outputs |
| [test_ons_screenshots.py](test_ons_screenshots.py) | Offline selection, capture, sizing, persistence and integration tests |
| [README.md](README.md) | Configuration and field reference |

There should be only one pipeline script. Do not introduce separate update and
rebuild implementations: their scraping and enrichment logic must stay shared.

## Discovery and Cached History

The original scraper was adapted from
[ONSdigital/get-ons-vis-screenshots](https://github.com/ONSdigital/get-ons-vis-screenshots).
The backend no longer downloads that repository's data or runs its screenshot
code. When enabled, release-search discovery queries
`https://api.beta.ons.gov.uk/v1/search/releases` in newest-first order, selecting
published, non-cancelled release entries. Their landing pages link to publication
URLs under `articles`, `bulletins` or `methodologies`.

The local history cache retains these fields:

- `doc_uri`: publication URL and release identity.
- `release_date`: freshly parsed dates are normalized to `YYYY-MM-DD`; retained
  legacy records can still contain ISO timestamps.
- `title`: publication heading, with the HTML title as a fallback.
- `vis_urls`: normalized, unique visualisation paths, excluding PNG and download links.
- Optional `enrichment`: `source_signature`, nullable `edition` and a `vis`
  array of URL/chart-type pairs. This is reusable processing input, not a public
  output field. No raw HTML is persisted.

Publication parsing prefers JSON-LD publication dates, with analytics and visible
release-date fallbacks. Pym embed attributes are preferred for visualisations;
DOM links and a controlled legacy-path fallback are used when necessary.
Publication paths retain query strings and discard fragments for identity.
Visualisation paths retain required queries and fragments. External hosts and
PNG assets are rejected.

The cache preserves known historical URLs, including publications with no
eligible embeds. It is not discarded when recent release discovery finds fewer
records. Discovery `all` refreshes all known URLs; a catalogue rebuild
does not imply a source refresh. No mode claims a complete historical archive crawl.

## Configuration and Modes

The single configuration reference, default JSON and common-run instructions
are in [README.md](README.md#pipeline-configuration). The script reads
[config.json](config.json) from the repository root; GitHub Actions executes that
same script with the committed configuration and no overrides.

Discovery modes are `off`, `new`, `recent`, `all`, `datestart` and `url`.
Catalogue modes are `off`, `new`, `recent` and `all`. Both default to `new`, with
`limit: null`. Discovery also defaults `url` and `start_date` to `null`.
Screenshots accepts the same six modes and settings as discovery but defaults
to `off`. It selects cached publications rather than searching releases. Its
`new` mode captures missing/failed images; other enabled modes recapture selected
charts. Recent selects newest eligible cached publications. URL mode requires a
cached publication URL; use discovery URL mode alongside it to populate the cache.
All selected charts are considered, but limits count publications, not charts.
Limits are positive article counts or null; recent mode requires a count.
Start-date discovery uses publication dates strictly after the selected day.
Unused URL/date inputs may remain in place when switching modes.

Earlier settings such as top-level mode, update_limit, scope, metadata policy,
URL arrays and date ranges are no longer supported. Replace the whole config
object with the documented default before changing modes. The excluded user
configuration must be edited manually, never retrieved via alternate tools.

Catalogue rebuilding automatically reuses enrichment whose source signature
matches normalized URL, day, title and eligible visualisation URLs. Matching
existing catalogue metadata can seed the enrichment cache. Present null edition
and chart-type values are complete results; missing or changed inputs are fetched.
Signature-valid enrichment is retained across unchanged source refreshes. There
is no raw-HTML snapshot: new information that was never saved needs HTTP access.
When an enabled catalogue stage processes a publication refreshed by discovery
in the same run, it re-extracts edition and chart metadata, sharing the fetched
publication HTML. Discovery-off catalogue rebuilding instead reuses saved data.

Catalogue `new` selects missing, changed or freshly discovered/refreshed records,
including pending work left by a source-only run. Catalogue `recent` and `all`
select newest-first cached publications; recent requires a limit. Limits cap
selected work, not the complete output. Only a complete catalogue `all` rebuild
replaces the full output; every partial run preserves unselected history.

Discovery `new` counts uncached publications toward its limit. Recent counts
unique publication URLs reached through newest-first release links, including
known or ineligible publications. These links can include older documents, so
the sample is not an exact newest-publication ranking. Discovery `all` refreshes
known cache history plus incremental discoveries, not the entire ONS archive.
With a limit, source discovery takes matching release links first and uses any
remaining all-mode budget on cached history in newest-publication order.

All three stages off is a logged no-op. Preserve the source cache and return config
to normal settings after one-off runs. Commit the new config for scheduled runs;
the script does not change it automatically.

## Processing Stages

1. Import dependencies, load configuration and validate its settings.
2. Load cached history and the optional discovery checkpoint; normalize and
  deduplicate cached URLs, preferring the newest source snapshot.
3. Execute the selected discovery policy, or skip discovery. Do not read the
  checkpoint in discovery-off mode.
4. If catalogue processing is enabled, load existing output for merge or metadata
  reuse. Catalogue-off does not open or write the catalogue.
5. Select cached publications by mode and limit. Use an empty output lookup only
  for a complete catalogue `all` rebuild; preserve unrelated records otherwise.
6. Reuse saved edition/chart metadata and fetch missing inputs. Remove selected
  records without eligible embeds; exclude PNG and download links. Save reusable
  enrichment separately from the public output fields.
7. Merge processed records, sort by publication day, group taxonomy fields,
  derive series, preserve/default editions and remove `vis_urls` and PNG links.
8. Recalculate `latest` across the entire output.
9. Stage only enabled/changed JSON artifacts before replacing any. Replace the
  catalogue, then cache, then checkpoint when applicable.
10. If screenshots is enabled, select publications from the final cache and
  capture unique eligible chart URLs. Persist successful JPGs and the merged
  screenshot index, log a consolidated failure summary and close browser resources.
11. Report the current screenshot counts and per-chart warnings to Actions.
  Close the HTTP session even on failure. Actions commits completed runs,
  including partial screenshot results, without failing solely for individual
  capture failures. Fatal pipeline errors still fail the run and skip committing.

### Screenshot Capture and Linking

The full configuration and index reference is in
[README.md](README.md#screenshots). Screenshot-only runs do not read/write
`data.json` or advance discovery state. Off mode does not read the image index or
start a browser; Playwright/Pillow imports are deferred until capture is needed.

Find a supported rendered plot, then capture its full wrapping div. Expand to
the common ancestor of chart panels, legends, controls, source and notes; fall
back to the visualisation page body when no suitable div contains them. Preserve
padding and all visible contents, including interactive controls and attribution.
Hidden panels remain hidden; this does not enumerate interactive states.
Unknown layouts, unresolved overflowing content, blank plots, script errors and
unstable layouts/pixels are explicit per-chart failures reported as warnings.
They retain the previous image and success metadata, if present; they are not
recorded as successful captures.

Use a fixed 700px viewport width and save a 700px-wide JPEG at the full natural
container height. There is no height target or responsive-width search. Preserve
horizontal space around narrower containers rather than stretching them. Reject
content outside the viewport instead of clipping it; tall containers are captured
in full, including content below the browser viewport. Reload only at the fixed
width to warm fonts and compare stable layouts. Measure and validate the final
JPEG, recording actual dimensions without a target-height flag. Old index entries
with narrower widths and `target_reached` remain readable until recaptured.
Before final measurement, allow capture-only wrapping of overflowing row-based
flex chart panels when each individual panel fits. Do not scale or hide contents.
Visible overflow can use a containing ancestor that still fits the viewport.
Reject source SVG labels outside the viewport rather than silently clipping them.
Inspect visible same-origin ONS iframes recursively, expanding their heights for
full content and translating child plot coordinates into the top-level capture.
Capture the complete dashboard, with child-frame readiness and blankness checks,
not independent frame images. Ignore hidden/helper frames and report visible
external/inaccessible frames as unsupported. Round fractional capture bounds
outward and reserve document-edge space before capturing, rather than accepting
an undersized/clipped image.
Measure the whole captured container, not only the SVG/canvas. Check blankness
using the plot surfaces so non-empty legends/source cannot mask a blank chart.
Track MapLibre/Mapbox constructors before navigation to observe renderer/style/tile
readiness even when instances are held in module-local state. Before saving, wait
for visible images and loading indicators, and require unchanged pixels with no
pending/new network requests for two seconds (ten seconds for maps). New activity
resets the quiet period. Readiness waits are bounded at 90 seconds and the overall
chart timeout remains 300 seconds; unready charts fail without replacing old images.
Previously successful captures are not automatically invalidated by the width
change; use an explicit refresh mode to recapture them.

The index is keyed by normalised absolute visualisation URL, retaining query
strings and fragments. Filenames normally use `dvcXXXX-figXX.jpg` in lowercase.
Collisions append eight SHA-256 characters from the full URL, with a numeric
suffix if that also collides. Non-standard URLs use a readable path-based stem.
Reserve existing filenames case-insensitively and preserve indexed assignments;
never overwrite another URL's image or an unknown pre-existing file.
Shared URLs have one image and a list of all referencing cached
publications, even when only some were selected for capture. `data.json` has no
additional screenshot fields.

Enabled stages migrate legacy long hashed filenames and update their index
links. Existing image bytes and saved capture metadata are preserved. Legacy
images left unindexed by an interrupted run can be identified from their full
URL hash and cached URL list; register these with missing success metadata and
an explicit failed-attempt explanation requiring recapture, rather than inventing
successful metadata. Migration is skipped when screenshots is off. Save index
entries after each capture attempt to retain filename ownership if later work
is interrupted.

Failures update `last_attempt` without replacing the prior JPG or `last_success`.
`new` retries failed attempts and missing images. It does not detect changed
content behind an unchanged successfully captured URL; request a recapture mode.
Unselected entries/images are preserved and unused images are not deleted.
Use a recapture mode to refresh images made with the earlier plot-only boundary.

Image and index replacements are independently atomic. An interrupted or fatal
write can leave an image newer than its success metadata; explicitly recapture
that publication to repair a failed refresh, or use `new` for missing/failed
entries. Screenshot errors do not undo already-published catalogue/cache/state.
Browser-startup, index-validation and filesystem errors remain fatal, not
silently skipped charts. The workflow propagates current-run failure status,
not historical errors retained for unselected charts.

### Checkpoint and Failure Recovery

`discovery-state.json` contains one `last_release_date` field. A run scans back to
that published-release day minus `lookback_days`, inclusively revisiting the
cutoff day. On the first run, the newest valid cached publication day, capped at
today, supplies the baseline. With no cache, the baseline is today.

Pagination stops after exhausting results or a complete page establishes coverage
before the cutoff. Old linked publications do not stop discovery. Repeated links
are deduplicated and publication HTML is cached for the run. Reaching the safety
cap without establishing coverage raises an error and does not advance state.
Incremental modes have an internal page safety cap. Explicit `datestart` scans
can traverse beyond it until they reach the date or exhaust results. Repeated
identical API pages are rejected rather than causing an unbounded loop.

Only completed `new` and `all` scans can advance the global checkpoint.
`recent`, `datestart`, `url` and `off` neither advance it nor create it when absent.
A bounded sample is not evidence that the incremental window has been covered.
Limited incremental runs retain the existing checkpoint. A first limited run
saves its initial baseline day so newly cached records cannot move that baseline
ahead of pending releases on the next run.
`new` skips known publication pages; periodic recent or explicit refresh runs are
needed to pick up corrections to those pages. Discovery-off preserves the state
file byte-for-byte, although catalogue enrichment can still update the cache.

Request, discovery, parsing and staging failures leave existing JSON untouched.
Each file replacement is atomic, but the three-file update is not a transaction:
a disk failure during replacement can leave newer data with an older cache or
checkpoint. The next run replays the older discovery window; URL-based merges
make replay idempotent. Temporary files are cleaned up. Persistent backups and
locks for manual local runs are not created automatically.

## Output Contract

The output is a JSON array with one record per stored `doc_uri`.

| Field | Meaning |
| --- | --- |
| `doc_uri` | Release identifier and link to the publication |
| `release_date` | Parsed publication date; refreshed records are normalized ISO dates |
| `title` | Publication heading, not an identifier or a shortened UI label |
| `edition` | Scraped edition label, or `null` |
| `series` | Title with recognized trailing date expressions removed |
| `latest` | Boolean indicating a newest publication day within the exact series |
| `theme` | First taxonomy path segment |
| `topic` | Second taxonomy path segment, or `null` if absent |
| `subtopic` | Third taxonomy path segment, or `null` if absent |
| `vis` | Visualisation objects containing `url`, nullable raw `chart_type` and the three chart-category arrays below |

`theme`, `topic` and `subtopic` are written consecutively as flat fields. Their
values are raw URL slugs, not human-readable labels. Key order helps readability
but is not a JSON identity or grouping mechanism.

The output omits `vis_urls` and cache-only `enrichment`; visualisation URLs are
available under `vis`. PNG and download links are filtered before eligibility,
so publications containing only those paths are excluded.

### Chart Categories

Every final `vis` object has `chart_families`, `chart_variants` and
`chart_features` arrays. The approved values and mapping examples are in
[README.md](README.md#chart-categories). The raw `chart_type` is retained;
existing visualisation metadata is not replaced by these derived fields.

`derive_chart_taxonomy` maps supported tags to deterministic, duplicate-free
arrays. `classify_visualisations` applies it without mutating its input and
preserves other fields. Unknown or null tags yield empty arrays, not guessed
categories. Population pyramid is a family; orientation variants are excluded;
confidence bands and intervals share `confidence-interval`. `range-slider`
requires explicit tag evidence. With-area line templates remain line-only
because a tag alone does not establish whether the area is a separate encoding.

The final catalogue output pass applies the mapping to ALL merged visualisations,
including retained records outside the current processing limit. Catalogue-off
does not run this pass. Cache reuse still stores only source chart metadata:
taxonomy is re-derived rather than cached, so mapping changes need no source
refresh. Catalogue-new freshness compares the original URL/chart-type pairs,
not the derived arrays, preventing repeated processing or limit starvation.

The initial existing-data backfill uses the same helper directly against the
saved catalogue, without executing discovery, metadata fetching or screenshots.
It adds only the three arrays, retaining list order, URLs, raw tags and all
other metadata. Reapplying it is idempotent. Future enabled catalogue writes
keep the fields up to date; no additional workflow stage or config is required.

## Release Identity Versus Series

`doc_uri`, not `title` or `series`, is the merge key. Different monthly releases
may share a title and must remain separate records. Reintroducing title-based
deduplication would collapse historical releases.

A recurring dashboard can reuse one URL while changing its content. Such a URL
still identifies one stored record; this pipeline does not archive every edition
ever displayed at the same address.

Cached duplicate URLs are merged in publication-date order, so the newest source
snapshot survives. Refreshed metadata comes from the current page rather than an
older cached occurrence. Each discovered or rebuilt URL is processed once per run.

## Series Derivation

`derive_series` strips recognized date suffixes and their preceding colon or
comma. Examples include years, month/day dates, ranges, `year ending` periods,
compound ranges and `mid-YYYY`.

It deliberately does not remove all embedded years or split every colon.
Descriptive titles such as `Milestones: journeying through modern life` remain
intact. Year recognition currently covers 1900 through 2099.

This is not editorial normalization. Punctuation differences, renamed series and
unsupported qualifiers such as `revised results` or `2024-based` can leave
related releases in separate series. Since `latest` uses exact series values,
review grouping effects before changing the regex. Do not merge geographically
different publications merely because their names look similar.

## Edition Extraction and Null Values

`DiscoveryClient` fetches publication HTML once for both metadata and editions.
`extract_edition` normalizes whitespace in the first `h1`, then:

1. Uses the text following the known source title and a colon when that prefix
   matches the heading.
2. Otherwise looks for a recognized date suffix using the series date pattern.
3. Returns `None`, serialized as `null`, when neither rule succeeds or no `h1`
   exists.

The edition is a display label, not a unique identifier or publication date. For
example, a July GDP edition may be published in September.

An edition of `null` can mean either:

- The page was processed but had no recognizable edition, including one-off
  articles, generic dashboards or an unsupported heading format.
- The record was retained from older output or merged from another branch and
  never processed by the edition scraper. Final enrichment defaults missing
  editions to `null` without fetching old publication pages.

The public schema does not distinguish these cases. Matching saved nulls are
reused, including during cached rebuilds. Choose source discovery `recent`,
`all` or `url` with catalogue enabled to revisit pages after extractor changes.
Even after a successful rebuild, null editions can be legitimate or reflect an
extraction limitation. Do not manufacture editions from URL slugs or publication
dates without an explicit change to the agreed extraction policy.

## Latest Flags and Publication Dates

`publication_date` accepts ISO date strings and timestamps, including `Z` values.
Comparisons use the calendar date recorded in the source string. Time of day is
ignored, and timezone offsets do not shift that date to another timezone.

`mark_latest` computes the maximum publication day per exact `series` and
overwrites every record's flag:

- All records on that maximum day are `latest: true`.
- Older records are `latest: false`.
- Single-entry series are naturally `true`.
- Edition text or a missing edition has no effect on the result.

More than one true record per series is valid when publication dates tie. Latest
means newest in this dataset, not necessarily newest currently available on the
ONS website. Missing or invalid publication dates raise an error before writing.

## Theme Extraction

`extract_themes` parses the URL path using `urllib.parse.urlsplit` and takes the
segments before the first `articles`, `bulletins` or `methodologies` marker.
Those markers and the following publication slug are not taxonomy levels.

For example, `economy/grossdomesticproductgdp/bulletins/...` has two levels:
`theme` is `economy`, `topic` is `grossdomesticproductgdp`, and `subtopic` is `null`.
One through three preceding levels are supported. Missing levels are `null`;
missing publication boundaries or deeper paths raise an error.

## Running Locally

Use the repository root as the working directory. GitHub Actions uses Python
3.11. Install dependencies into the Python interpreter you intend to use:

```powershell
python -m pip install -r requirements.txt
```

Inspect the current configuration before running, then execute from PowerShell:

```powershell
python catalogue.py
```

The script reads configuration and writes generated files relative to the
working directory. Which files and pages are touched depends on all stage
settings. Catalogue-only rebuilding reuses saved inputs, fetching missing
metadata when necessary. Explicit discovery refreshes also refresh catalogue
metadata for processed publications. Live requests have a 30-second timeout and one
second of pacing between uncached requests. Up to three retries with bounded
exponential backoff handle selected transient failures. A full rebuild can take
many minutes; publication HTML is reused. An enabled screenshots stage separately
starts Chromium and visits the selected chart URLs. Install it first:

```powershell
python -m playwright install chromium
```

Before a production rebuild, retain copies of cache, data and checkpoint. Afterward, inspect
the output diff and restore normal stage settings if required. Neither the script nor a
local run commits or pushes changes automatically.

If configuration or another file is excluded from an AI agent's access, respect
that restriction. Ask the user to inspect or expose it; do not retrieve excluded
contents through alternate tools or Git history.

## GitHub Actions

The workflow runs on pushes to `main`, manual dispatch and a daily cron schedule
of `35 4 * * *` (04:35 UTC). It:

1. Checks out the repository and configures Python 3.11.
2. Installs requirements; there is no upstream-repository download step.
3. Checks the screenshot settings and installs Chromium/system
   dependencies only if screenshots is enabled.
4. Executes `python -u catalogue.py` using the tracked config. Unbuffered output
  shows progress directly in Actions logs; no notebook execution is required.
5. Stages only existing cache, catalogue and checkpoint files plus screenshot
  JPGs/index when present, then uses the automated Git
  identity, stashes generated changes, rebases
   against `origin/main`, reapplies the stash, and commits and pushes changes.
6. Marks the run failed if the script failed. Expected individual screenshot
   failures allow step 5 to commit successful work first; unrelated fatal
  pipeline failures skip that step.

Review the workflow before changing branch behavior. A concurrency group prevents
overlapping runs on the same branch without cancelling active runs. The workflow
does not have a dedicated offline test step, and manual local runs still require
coordination to avoid concurrent writers.

## Current Operational Limitations

- Search, landing, publication and chart metadata requests use the bounded HTTP
  client and reject unsuccessful HTTP responses. A broken historical publication
  or chart can stop a rebuild rather than silently produce partial data.
- Requests use `verify=False`, and insecure-request warnings are suppressed.
  This disables TLS certificate verification; it is existing behavior, not a
  recommendation for new integrations. Screenshot browser requests keep normal
  browser TLS verification and have separate navigation/rendering timeouts.
- Chart metadata extraction tries template-name and template-property fallbacks
  before saving the entry. A null chart type still does not establish that the
  chart is unclassifiable; use discovery refresh with catalogue enabled after
  extractor changes.
- PNG paths are excluded and `datadownload.xlsx` paths are skipped. Other
  non-PNG file types can still enter the visualisation list.
- Discovery is bounded by the checkpoint/overlap and safety cap, not a complete
  archive crawl. New-only mode does not refresh known pages, even in the overlap.
  Use discovery `recent`, `all` or `url` with catalogue enabled to capture changes.
- Cached enrichment is parsed metadata, not an HTML snapshot. New fields that
  require previously unrecorded page content cannot be rebuilt strictly offline.
- Separate atomic file replacements are not a cross-file transaction. Failed
  replacement leaves the checkpoint behind for replay; do not advance it manually
  to hide a failed discovery/catalogue run. Screenshot work happens after those
  writes and can fail with an already-advanced successful discovery checkpoint.
- Screenshot container support is deliberately conservative, not universal
  coverage of all legacy templates. Maps wait for network/layout/pixel stability
  and tracked or globally exposed renderer ready state when available; this is not proof
  that every possible interactive state or data layer has been captured.
- JPG history grows the Git repository; unused images are not pruned or archived
  separately. Coordinate manual runs to avoid competing writers.

These are observed behaviors to consider when maintaining the pipeline. This
guide does not change or fix them.

## Validation and Maintenance Checklist

Run the focused screenshot suite after installing requirements and Chromium:

```powershell
python -m unittest -v test_ons_screenshots
```

The suite uses in-memory browser routes rather than live ONS requests and never
reads the user configuration. It checks modes, publication limits, shared URLs,
fixed-width capture at natural height, reloads, whole-container JPEG dimensions, complete panels, map
readiness/visible controls, partial failures, atomic image writes and script
stage isolation, including cached catalogue derivation. Broader discovery/catalogue checks below remain maintenance
guidance, not claims of complete automated coverage.

- Exercise every discovery/catalogue mode with duplicate cached URLs
  and more than ten discoveries.
- Assert zero HTTP requests for catalogue-only `all` rebuilds with complete metadata,
  saved-null reuse, unchanged-source enrichment retention, and processing
  of pending cache records after a discovery-only run.
- Verify limited catalogue runs preserve other output, off stages preserve
  their artifacts, and recent/start-date/URL scans do not advance the global checkpoint.
- Verify distinct URLs with matching titles survive, repeated URLs follow the
  intended snapshot policy, and empty `vis_urls` inputs are skipped.
- Verify all retained update records receive derived metadata and recalculated
  latest flags, even outside the selected batch.
- Test editionless articles, dated headings, descriptive colons and unsupported
  qualifiers without conflating publication date with edition.
- Test two- and three-level taxonomy and consecutive taxonomy field ordering.
- Check that `latest` is boolean on every record and matches the maximum parsed
  publication day per exact series, allowing ties.
- Confirm output URLs are unique, `vis_urls` is absent, values are preserved as
  intended and output is sorted by publication day.
- Exercise same-day overlap, cancelled releases, old linked documents, page-cap
  failures, malformed responses, PNG paths with queries and fragments, and HTML
  reuse with exactly one publication fetch per URL.
- Simulate invalid config, failed requests and failed staging; prior files must
  remain intact. Simulate replacement failures separately: the checkpoint must
  not advance, temporary files must be cleaned up, and replay must be safe.
- Review Git status before modifying files, preserve unrelated user changes and
  do not commit, push or trigger a production run without authorization.

Avoid hard-coding catalogue counts into documentation or validation assumptions;
the upstream source and generated output change over time. The script and
workflow are the source of truth when this guide needs updating.