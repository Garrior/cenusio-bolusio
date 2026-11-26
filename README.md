# Board game price history (Poland)

This project discovers board games and expansions automatically from Planszeo. It keeps an alphabetical catalog in `data/catalog.json` and appends price observations to `data/price_history.jsonl`. You do not need to maintain a game list.

## What the daily run does

- Crawls 100 Planszeo catalog pages per run and adds discovered titles. The catalog is sorted alphabetically in the repo.
- Checks a rotating batch of 2,500 catalog titles for prices. For each Planszeo page, it keeps the three cheapest distinct shops; Amazon.pl and Allegro each contribute their closest matching listing.
- Uses Allegro's official API if credentials are configured. Without them, the scraper tries one ordinary public search page; if Allegro returns an access block, it skips further Allegro page requests for that run.
- Appends observations as JSON Lines, then the local runner commits and pushes only the generated `data/` files.

Planszeo currently exposes about 1,441 catalog pages. At 100 pages per run, a complete first catalog pass takes about 15 daily runs. At 2,500 titles per run, a full price rotation is about two weeks for a catalog of roughly 36,000 titles. Each observation has its own date, game, shop, PLN amount, source URL, and availability when the source provides it. A full-market refresh for every one of tens of thousands of titles every day would need far more requests than this daily workflow can responsibly make.

## Local setup

Run these commands from the repository folder in PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
gh auth setup-git
```

The final command configures Git to use your existing GitHub CLI login for pushes. The scheduled script requires a clean working tree on the `main` branch and a configured Git commit name and email. It pulls the latest `main` with fast-forward only, runs the scraper, then commits and pushes changes under `data/`. It stops without pushing if the scrape fails or if there are unrelated local changes.

Run it manually to confirm setup:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\run-daily.ps1
```

The script writes a transcript to the ignored `logs/` folder. It never reads credentials from files in the repository or stages anything outside `data/`.

## Schedule it on Windows

Windows Task Scheduler is the built-in equivalent of cron:

1. Open **Task Scheduler** and choose **Create Basic Task**.
2. Name it `Board game price history` and choose a **Daily** trigger, such as 07:00 local time.
3. Choose **Start a program**. Set **Program/script** to `powershell.exe`.
4. Set **Add arguments** to `-NoProfile -ExecutionPolicy Bypass -File "D:\projects\cenusio-bolusio\scripts\run-daily.ps1"`.
5. Set **Start in** to `D:\projects\cenusio-bolusio`.
6. In task properties, choose **Run only when user is logged on** so the task uses your normal Git credentials. Enable **Wake the computer to run this task** if needed. The PC must be powered on (or wakeable) and connected to the internet; GitHub authentication must already work without prompts.

For Allegro's official API (optional), add `ALLEGRO_CLIENT_ID` and `ALLEGRO_CLIENT_SECRET` as user environment variables in Windows **Environment Variables** settings. They are read by the scraper process and are never written to the repository. Without them, the scraper tries Allegro's public search page, which may be blocked.

## Matching and limitations

Amazon and Allegro results are matched by title, with accessory-like titles filtered out. This is deliberately a rough match and can mix editions or miss translated titles. The catalog is limited to what Planszeo lists as board games and expansions. If a source requires JavaScript, blocks a request, or changes its page layout, that source may produce no observation for the affected title; the run logs the issue and continues where possible. Planszeo returned HTTP 403 to GitHub-hosted runners, so the scraper is intended to run from your local network.
