# Board game price history (Poland)

This project discovers board games and expansions automatically from Planszeo. It keeps an alphabetical catalog in `data/catalog.json` and appends price observations to `data/price_history.jsonl`. You do not need to maintain a game list.

## What the daily run does

- Crawls 100 Planszeo catalog pages per run and adds discovered titles. The catalog is sorted alphabetically in the repo.
- Checks a rotating batch of 2,500 catalog titles for prices. For each Planszeo page, it keeps the three cheapest distinct shops; Amazon.pl and Allegro each contribute their closest matching listing.
- Uses Allegro's official API if credentials are configured. Without them, the scraper tries one ordinary public search page; if Allegro returns an access block, it skips further Allegro page requests for that run.
- Appends observations as JSON Lines and commits the catalog/history to the default branch.

Planszeo currently exposes about 1,441 catalog pages. At 100 pages per run, a complete first catalog pass takes about 15 daily runs. At 2,500 titles per run, a full price rotation is about two weeks for a catalog of roughly 36,000 titles. Each observation has its own date, game, shop, PLN amount, source URL, and availability when the source provides it. A full-market refresh for every one of tens of thousands of titles every day would need far more requests than this daily workflow can responsibly make.

## Allegro API setup (optional)

Create an Allegro application, then add repository Actions secrets named `ALLEGRO_CLIENT_ID` and `ALLEGRO_CLIENT_SECRET`. The workflow uses the client credentials flow and official offer search endpoint. Without these secrets, Allegro may return HTTP 403 to the public search page and no Allegro prices will be recorded.

## Workflow setup

The workflow runs daily at 06:00 UTC (07:00/08:00 in Poland depending on daylight saving time) and can also be started manually. In repository settings, GitHub Actions must have **Read and write permissions** so it can commit `data/catalog.json` and `data/price_history.jsonl`.

## Matching and limitations

Amazon and Allegro results are matched by title, with accessory-like titles filtered out. This is deliberately a rough match and can mix editions or miss translated titles. The catalog is limited to what Planszeo lists as board games and expansions. If a source requires JavaScript, blocks a request, or changes its page layout, that source may produce no observation for the affected title; the run logs the issue and continues where possible.
