# Board game price history (Poland)

A small daily price tracker for board games and expansions sold in Poland. It stores one JSON Lines observation per game and source in `data/price_history.jsonl`, so the history stays append-only and can be analyzed later.

## Setup

1. Add games and their known product page URLs to [`games.json`](games.json). Use a stable `id` for each game. The first version accepts Planszeo product URLs and other product pages with Schema.org `Product` / `Offer` JSON-LD or standard `itemprop` price metadata.
2. Add a `sources` list for each game. For example:

   ```json
   {
     "id": "wingspan",
     "name": "Wingspan",
     "kind": "base_game",
     "sources": [
       {"shop": "Planszeo", "url": "https://planszeo.pl/..."},
       {"shop": "Local shop", "url": "https://shop.example/..."}
     ]
   }
   ```

   `kind` should be `base_game` or `expansion`. Keeping product links explicit avoids matching similarly named accessories, RPGs, or TCG products.
3. Enable Actions write access in the repository settings: **Settings → Actions → General → Workflow permissions → Read and write permissions**. The workflow commits updated history back to the default branch.
4. Run **Daily board game prices → Run workflow** once to seed the history, then the scheduled run executes daily at 06:00 UTC (07:00/08:00 Poland time depending on daylight saving time).

## Data format

Each line in `data/price_history.jsonl` is an independent observation with `observed_at` in UTC, game identity, source/shop, price in PLN, availability (when published by the page), and URL. Missing or unparseable prices are logged as source errors in the Actions run; they are not written as zero prices.

## Source notes

- **Planszeo / product pages:** pages are fetched politely (one request per URL, a delay between pages) and only structured product/offer metadata is read. Sites may block automation or change their markup. Pages that require JavaScript or a location check may not yield a price.
- **Allegro:** use a specific offer URL only if its page exposes standard structured data. Prefer an official API integration if you need reliable live offers; this repository does not attempt to guess which search results represent the exact game. An API adapter can be added with Allegro app credentials and product/offer IDs.
- **Amazon.pl:** intentionally not scraped. Amazon's published terms prohibit automated collection and reuse of product prices/listings without permission. Add it only through an Amazon-authorized data route and after reviewing that route's terms.

The list in `games.json` starts empty. Price history becomes useful after you add the games you actually want to track and the first daily observations accumulate.
