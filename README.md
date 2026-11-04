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

Run the offline regression checks with `python -m unittest test_series.py`.
These check both notebook helpers and output cells without fetching charts or
overwriting the data file.
