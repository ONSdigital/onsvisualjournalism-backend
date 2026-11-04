# onsvisualjournalism-backend
Data processing for the ONS Visual Journalism catalogue of releases.

## Pipeline Configuration

[catalogue.ipynb](catalogue.ipynb) is the single pipeline for local and GitHub
Actions runs. It reads [config.json](config.json) from the repository root:

```json
{
    "mode": "update",
    "update_limit": 10
}
```

| Mode | Inputs processed | Existing output |
| --- | --- | --- |
| `update` | Newest `update_limit` input entries by publication date | Retained and merged by `doc_uri` |
| `rebuild` | All input entries; `update_limit` is ignored | Replaced using only the current inputs |

Both settings are required. `mode` must be `update` or `rebuild`, and
`update_limit` must be a positive integer. Invalid configuration stops the run
before any fetching or writing. Inputs are sorted by parsed publication date;
equal dates retain their input order. In update mode, the limit is applied before
excluding entries with empty `vis_urls`, so fewer than ten releases may be fetched.
If the output file is absent, update mode starts with an empty catalogue; use
rebuild mode to create a complete initial catalogue.

To rebuild, set `mode` to `rebuild` and run the notebook. Afterwards, set it back
to `update` before committing configuration changes. GitHub Actions uses the same
configuration without an override, so a committed rebuild setting also affects
scheduled runs. The notebook does not reset configuration automatically.

Both modes use the same fetching, merge and enrichment code. Output is written
to a temporary file and atomically replaces `data.json` only after successful
processing, so a failed run leaves the previous catalogue intact.

The pipeline identifies releases by `doc_uri`, preserving different releases
that share a title. A repeated URL refreshes the same release instead of adding
a duplicate. Input entries with empty `vis_urls` are still excluded from
processing. Recovering historical releases collapsed by the previous title-based
logic requires running in rebuild mode.

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

The pipeline preserves `title` as supplied and derives `series` by removing a
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

The pipeline fetches the publication page at `doc_uri` for each processed release
with non-empty `vis_urls` and add an `edition` field. This is local enrichment;
the upstream input does not provide a separate edition field.

The extractor reads the main heading, taking the text after the known title
when present, or a recognised trailing date expression otherwise. For example,
`GDP monthly estimate, UK: July 2025` yields `July 2025`. Descriptive colons are
not split indiscriminately. The existing `title` and `series` are unchanged.

If no edition is found, the value is `null`, not the publication date. Publication
requests have a 30-second timeout and HTTP errors stop the run before output is
written. Update mode preserves editions on unprocessed records and adds
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

