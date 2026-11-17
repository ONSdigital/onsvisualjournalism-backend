# onsvisualjournalism-backend
Data processing for the ONS Visual Journalism catalogue of releases.

See [PIPELINE.md](PIPELINE.md) for the end-to-end guide for users and AI agents,
including the data contract, operating instructions and known limitations.

## Pipeline Configuration

[catalogue.ipynb](catalogue.ipynb) is the single pipeline for local and GitHub
Actions runs. It reads [config.json](config.json) from the repository root:

```json
{
    "mode": "update",
    "discovery": {
        "page_size": 50,
        "max_pages": 15,
        "lookback_days": 7
    }
}
```

| Mode | Inputs processed | Existing output |
| --- | --- | --- |
| `update` | All publications discovered in the recent release window | Retained and merged by `doc_uri`; visited publications without non-PNG embeds are removed |
| `rebuild` | Recent discoveries plus every known publication in the local history cache | Replaced using refreshed eligible publications |

Only `mode` is required. Discovery settings default to the values above. Page
size and page cap must be positive integers; lookback days must be a non-negative
integer. The legacy `update_limit` setting is still accepted and validated but
does not limit work: every discovered eligible publication is processed.
Invalid configuration stops the run before fetching or writing.

[ons_discovery.py](ons_discovery.py) queries the ONS release search API directly,
follows publication links and maintains [articles-and-dvcs.json](articles-and-dvcs.json)
as the local history cache. The screenshot repository is no longer downloaded.
No screenshots, browser automation or PNG files are generated; original PNG
visualisation links are excluded too. SVG and JPEG publication assets are retained.

`discovery-state.json` records the last successfully scanned published-release
day. Each scan revisits that day minus `lookback_days` to cover same-day additions
and recently changed pages. On the first run, the newest cached publication day
(no later than today) supplies the baseline. With no cache, discovery begins with
the recent lookback window. Reaching `max_pages` before coverage is complete
fails the run rather than silently accepting a partial scan.

Update mode retains history outside the discovery window. Rebuild mode refreshes
all known cached publication URLs; it does not crawl the entire ONS archive.
Preserve the local cache when historical coverage matters.

To rebuild, set `mode` to `rebuild` and run the notebook. Afterwards, set it back
to `update` before committing configuration changes. GitHub Actions uses the same
configuration without an override, so a committed rebuild setting also affects
scheduled runs. The notebook does not reset configuration automatically.

Both modes use the same fetching, merge and enrichment code. All three generated
JSON files are staged before publication. Catalogue and cache are replaced first,
with the discovery checkpoint last. Each file replacement is atomic, but the
three-file update is not a transaction: a replacement failure can leave newer
data with the older checkpoint, which is replayed on the next successful run.
Discovery, request, parse or serialization failures before publication leave the
existing files untouched.

The pipeline identifies releases by `doc_uri`, preserving different releases
that share a title. A repeated URL refreshes the same release instead of adding
a duplicate. Cached duplicate URLs prefer the newest publication date. Publication
HTML is fetched once per URL per run and reused for title, date, embed and edition
extraction. Publications with empty `vis_urls` after PNG filtering are excluded
from the output, including visited records previously present in update mode.
Recovering historical releases collapsed by the previous title-based logic
requires retaining the history cache and running in rebuild mode.

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

Both modes enrich every record in the final output before writing, including
existing records outside an update batch. `series` is a grouping field, not a merge key.
Date removal does not reconcile historical changes in series wording.

The pipeline uses `vis_urls` from the source input to fetch visualisations, then
removes it from every output record before writing `data.json`. The `vis` array
retains each visualisation URL and its chart type. Update mode also removes
`vis_urls` from older records already in the output file.

## Release Editions

Discovery fetches publication pages and the pipeline reuses that HTML to add an
`edition` field for eligible releases. No second edition-only request is needed.

The extractor reads the main heading, taking the text after the known title
when present, or a recognised trailing date expression otherwise. For example,
`GDP monthly estimate, UK: July 2025` yields `July 2025`. Descriptive colons are
not split indiscriminately. The existing `title` and `series` are unchanged.

If no edition is found, the value is `null`, not the publication date. Requests
have a 30-second timeout, bounded retries for transient errors, and pacing between
uncached requests. HTTP errors stop the run before generated files are published.
Update mode preserves editions on unprocessed records and adds
`null` for older records missing the field; run in rebuild mode to backfill
all eligible historical releases. `doc_uri` remains the release identifier.

## Latest Releases

Every output record has a boolean `latest` field. Both modes recompute it
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

