# onsvisualjournalism-backend
Data processing for the ONS Visual Journalism catalogue of releases.

## Pipeline Notebooks

- [rebuild-catalogue.ipynb](rebuild-catalogue.ipynb) processes all input entries and
	replaces `data.json`.
- [update-catalogue.ipynb](update-catalogue.ipynb) processes the latest ten input
	entries and merges releases by `doc_uri` into the existing `data.json`. GitHub
	Actions runs this notebook.

Both notebooks identify releases by `doc_uri`, preserving different releases
that share a title. A repeated URL refreshes the same release instead of adding
a duplicate. Input entries with empty `vis_urls` are still excluded from
processing. Recovering historical releases collapsed by the previous title-based
logic requires running the full rebuild notebook.

## Theme Hierarchy

Both notebooks derive three hierarchy fields locally from `doc_uri`:

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

Both notebooks preserve `title` as supplied and derive `series` by removing a
recognised trailing release date and its preceding colon or comma. Supported
suffixes include years, month/day dates, date ranges, `year ending` periods,
compound ranges and `mid-YYYY`. Descriptive colons and embedded years are retained.
Years in narrative titles such as "nature since 2020" are also retained.

The rebuild notebook enriches every record in `pages` before writing. The update
notebook enriches every record in `merged`, including existing records outside
its latest-ten processing batch. `series` is a grouping field, not a merge key.
Date removal does not reconcile historical changes in series wording.

Both notebooks use `vis_urls` from the source input to fetch visualisations, then
remove it from every output record before writing `data.json`. The `vis` array
retains each visualisation URL and its chart type. The update merge also removes
`vis_urls` from older records already in the output file.

## Release Editions

Both notebooks fetch the publication page at `doc_uri` for each processed release
with non-empty `vis_urls` and add an `edition` field. This is local enrichment;
the upstream input does not provide a separate edition field.

The extractor reads the main heading, taking the text after the known title
when present, or a recognised trailing date expression otherwise. For example,
`GDP monthly estimate, UK: July 2025` yields `July 2025`. Descriptive colons are
not split indiscriminately. The existing `title` and `series` are unchanged.

If no edition is found, the value is `null`, not the publication date. Publication
requests have a 30-second timeout and HTTP errors stop the run before output is
written. The update notebook preserves editions on unprocessed records and adds
`null` for older records missing the field; run the rebuild notebook to backfill
all eligible historical releases. `doc_uri` remains the release identifier.

## Latest Releases

Every output record has a boolean `latest` field. Both notebooks recompute it
across the complete output immediately before writing, including older records
outside the update notebook's latest-ten processing batch.

Within each exact `series` value, records with the newest `release_date` are
`latest: true`; all others are `false`. Edition labels are not used, including
when they are missing. A single-entry series is always latest, and all records
tied on the newest publication day are latest.

ISO dates and timestamps are parsed using the calendar date recorded in
`release_date`; time of day and timezone offsets do not shift that date.
Missing or invalid publication dates stop the run before output is written.
Latest means newest in this dataset, not necessarily the latest release on ONS.

Run the offline regression checks with `python -m unittest test_editions.py -v`.
These check both notebook helpers and output cells without fetching charts or
overwriting the data file.
