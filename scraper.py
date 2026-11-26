#!/usr/bin/env python3
"""Discover Polish board games on Planszeo and record rotating daily prices."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("PRICE_DATA_DIR", str(ROOT / "data"))).resolve()
CATALOG_PATH = DATA_DIR / "catalog.json"
HISTORY_PATH = DATA_DIR / "price_history.jsonl"
PLANSEZO = "https://www.planszeo.pl"
SEARCH_URL = f"{PLANSEZO}/szukaj"
AMAZON_SEARCH = "https://www.amazon.pl/s"
ALLEGRO_SEARCH = "https://allegro.pl/listing"
ALLEGRO_API = "https://api.allegro.pl"
REQUEST_DELAY_SECONDS = float(os.getenv("REQUEST_DELAY_SECONDS", "0.5"))
DISCOVERY_PAGES_PER_RUN = int(os.getenv("DISCOVERY_PAGES_PER_RUN", "100"))
PRICE_BATCH_SIZE = int(os.getenv("PRICE_BATCH_SIZE", "2500"))
TIMEOUT_SECONDS = 25
USER_AGENT = "Mozilla/5.0 (compatible; BoardGamePriceHistory/1.0; personal project)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("price-scraper")
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7",
})

ACCESSORY_TERMS = {
    "accessory", "akcesorium", "akcesoria", "organizer", "organiser", "insert", "wkład", "mata", "playmat",
    "koszulki", "sleeves", "sleeve", "booster", "booster box", "deck box", "deckbox",
    "promotional card", "promo card", "karta promocyjna", "karty promocyjne", "zestaw koszulek",
    "kompatybilny", "compatible", "storage", "storage box", "tacka", "tacek", "tray",
    "dice tower", "dice tray", "deluxe upgrade", "upgrade pack", "metal coins", "coin set",
    "żeton", "zeton", "token set", "accessory pack", "zestaw akcesoriów", "candlestick",
}


def parse_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^\d,.]", "", str(value)).strip()
    if not cleaned:
        return None
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        # Polish format is usually 1 234,56; Schema.org uses 1234.56.
        cleaned = cleaned.replace(".", "").replace(",", ".") if cleaned.rfind(",") > cleaned.rfind(".") else cleaned.replace(",", "")
    try:
        amount = Decimal(cleaned)
        return amount if amount > 0 else None
    except InvalidOperation:
        return None


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return " ".join(re.findall(r"[a-z0-9]+", text))


def name_score(query: str, candidate: str) -> float:
    q, c = normalise(query), normalise(candidate)
    if not q or not c:
        return 0.0
    q_words, c_words = set(q.split()), set(c.split())
    shared = len(q_words & c_words)
    extra_words = len(c_words - q_words)
    if extra_words > max(3, len(q_words) + 1):
        return 0.0
    jaccard = shared / max(1, len(q_words | c_words))
    return max(SequenceMatcher(None, q, c).ratio(), jaccard)


def is_accessory(title: str) -> bool:
    normal = normalise(title)
    return any(normalise(term) in normal for term in ACCESSORY_TERMS)


def walk_json(value: Any):
    if isinstance(value, list):
        for child in value:
            yield from walk_json(child)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from walk_json(child)


def product_jsonld(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in walk_json(data):
            types = item.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if "Product" in types:
                return item
    return None


def get_page(url: str, *, params: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> requests.Response:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Only absolute HTTPS source URLs are accepted")
    response = SESSION.get(url, params=params, headers=headers, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    return response


def page_count(soup: BeautifulSoup) -> int:
    pages = [1]
    for link in soup.select('a[href*="page="]'):
        href = link.get("href", "")
        if "/szukaj" not in href:
            continue
        match = re.search(r"[?&]page=(\d+)", href)
        if match:
            pages.append(int(match.group(1)))
    return max(pages)


def game_kind(text: str) -> str:
    folded = text.casefold()
    if "dodatek" in folded or "rozszerzenie" in folded:
        return "expansion"
    if "gra podstawowa" in folded:
        return "base_game"
    return "board_game"


def discover_listing_page(page_number: int) -> tuple[list[dict[str, str]], int]:
    response = get_page(SEARCH_URL, params={"page": page_number})
    soup = BeautifulSoup(response.text, "html.parser")
    total_pages = page_count(soup)
    results: list[dict[str, str]] = []
    seen: set[str] = set()

    for link in soup.select('a[href^="/gry-planszowe/"]'):
        href = link.get("href", "").split("?", 1)[0].rstrip("/")
        parts = href.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "gry-planszowe" or href in seen:
            continue
        title = link.get_text(" ", strip=True)
        if not title or title.casefold().startswith(("najlepsza ", "gry dla ", "gry kooperacyjne", "gry rodzinne")):
            continue

        card = link
        kind_text = title
        for _ in range(7):
            if card.parent is None:
                break
            card = card.parent
            kind_text += " " + card.get_text(" ", strip=True)
            if "gra podstawowa" in kind_text.casefold() or "dodatek" in kind_text.casefold():
                break

        slug = parts[1]
        results.append({
            "id": slug,
            "name": title,
            "kind": game_kind(kind_text),
            "planszeo_url": urljoin(PLANSEZO, href),
        })
        seen.add(href)
    return results, total_pages


def load_catalog() -> dict[str, Any]:
    if not CATALOG_PATH.exists():
        return {"discovery_cursor": 1, "price_cursor": 0, "total_pages": 0, "games": []}
    with CATALOG_PATH.open(encoding="utf-8") as file:
        catalog = json.load(file)
    catalog.setdefault("discovery_cursor", 1)
    catalog.setdefault("price_cursor", 0)
    catalog.setdefault("total_pages", 0)
    catalog.setdefault("games", [])
    return catalog


def discover_games(catalog: dict[str, Any]) -> int:
    cursor = int(catalog.get("discovery_cursor", 1))
    total = int(catalog.get("total_pages", 0))
    found: dict[str, dict[str, str]] = {game["id"]: game for game in catalog["games"]}
    pages_to_scan = range(cursor, cursor + max(1, DISCOVERY_PAGES_PER_RUN))
    pages_scanned = 0

    for page_number in pages_to_scan:
        if total and page_number > total:
            page_number = ((page_number - 1) % total) + 1
        try:
            games, discovered_total = discover_listing_page(page_number)
            total = discovered_total
            pages_scanned += 1
            for game in games:
                found[game["id"]] = game
        except (requests.RequestException, ValueError) as error:
            LOG.warning("Catalog page %d failed: %s", page_number, error)
        time.sleep(REQUEST_DELAY_SECONDS)

    if total:
        catalog["total_pages"] = total
        catalog["discovery_cursor"] = ((cursor + pages_scanned - 1) % total) + 1
        if pages_scanned == DISCOVERY_PAGES_PER_RUN:
            catalog["discovery_cursor"] = (cursor + pages_scanned - 1) % total + 1
    catalog["games"] = sorted(found.values(), key=lambda game: (normalise(game["name"]), game["id"]))
    LOG.info("Scanned %d catalog pages; catalog contains %d titles (page %d of %d next)",
             pages_scanned, len(catalog["games"]), catalog.get("discovery_cursor", 1), total)
    return pages_scanned


def extract_planszeo(html: str, game: dict[str, str], observed_at: str) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    product = product_jsonld(soup) or {}
    canonical_name = str(product.get("name") or game["name"]).strip()
    if canonical_name:
        game["name"] = canonical_name
    game["kind"] = game_kind(soup.get_text(" ", strip=True)) if game["kind"] == "board_game" else game["kind"]
    records: list[dict[str, Any]] = []
    seen_offer_ids: set[str] = set()

    for row in soup.select("div.grid.grid-cols-4.items-center"):
        offer_link = row.select_one('a[href^="/oferta/"]')
        if not offer_link:
            continue
        offer_id = offer_link.get("href", "").rstrip("/").rsplit("/", 1)[-1]
        if offer_id in seen_offer_ids:
            continue
        seen_offer_ids.add(offer_id)
        cells = row.find_all("div", recursive=False)
        if len(cells) < 4:
            continue
        price_node = cells[2].select_one(".text-lg.font-bold")
        price = parse_price(price_node.get_text(" ", strip=True) if price_node else None)
        if price is None:
            continue
        offered_title = cells[2].get_text(" ", strip=True)
        if price_node:
            offered_title = offered_title.replace(price_node.get_text(" ", strip=True), " ")
        offered_title = re.sub(r"\b(?:niedostępna|dostępna)\b", " ", offered_title, flags=re.I).strip()
        if is_accessory(offered_title) or name_score(canonical_name, offered_title) < 0.45:
            continue
        merchant_text = cells[3].get_text(" ", strip=True)
        merchant = re.split(r"\s+(?:Wysyłka|Dostawa|darmowa dostawa|miasto:)", merchant_text, maxsplit=1, flags=re.I)[0].strip()
        if not merchant:
            merchant = "Planszeo offer"
        product_text = cells[2].get_text(" ", strip=True).casefold()
        in_stock = False if "niedostępna" in product_text else True if "dostępna" in product_text else None
        records.append(make_observation(
            game, merchant, price, "PLN", in_stock, urljoin(PLANSEZO, offer_link.get("href", "")), observed_at, "planszeo"
        ))

    if not records:
        offers = product.get("offers", {})
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        currency = str(offers.get("priceCurrency", "PLN")).upper() if isinstance(offers, dict) else "PLN"
        amount = parse_price(offers.get("lowPrice") or offers.get("price")) if isinstance(offers, dict) else None
        if amount is not None and currency == "PLN":
            records.append(make_observation(game, "Planszeo", amount, currency, None, game["planszeo_url"], observed_at, "planszeo"))
    # Keep the most useful price signal without writing every duplicate listing
    # into the Git history: retain the cheapest offer from each store and the
    # three cheapest distinct stores for this Planszeo product.
    cheapest_by_store: dict[str, dict[str, Any]] = {}
    for record in records:
        key = normalise(record["shop"])
        previous = cheapest_by_store.get(key)
        if previous is None or Decimal(record["price"]) < Decimal(previous["price"]):
            cheapest_by_store[key] = record
    return sorted(cheapest_by_store.values(), key=lambda record: Decimal(record["price"]))[:3]


def make_observation(game: dict[str, str], shop: str, price: Decimal, currency: str,
                     in_stock: bool | None, url: str, observed_at: str, source: str) -> dict[str, Any]:
    return {
        "observed_at": observed_at,
        "game_id": game["id"],
        "game_name": game["name"],
        "kind": game["kind"],
        "shop": shop,
        "price": str(price.quantize(Decimal("0.01"))),
        "currency": currency,
        "in_stock": in_stock,
        "url": url,
        "source": source,
    }


def amazon_results(game: dict[str, str], observed_at: str) -> list[dict[str, Any]]:
    response = get_page(AMAZON_SEARCH, params={"k": game["name"], "i": "toys"})
    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[tuple[float, str, Decimal, str]] = []
    for item in soup.select('[data-component-type="s-search-result"]'):
        title_node = item.select_one("h2 span")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title or is_accessory(title):
            continue
        score = name_score(game["name"], title)
        if score < 0.55:
            continue
        price_node = item.select_one(".a-price .a-offscreen")
        price = parse_price(price_node.get_text(" ", strip=True) if price_node else None)
        link = item.select_one("a[href*='/dp/']")
        if price is None or not link:
            continue
        candidates.append((score, title, price, urljoin("https://www.amazon.pl", link["href"])))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[2]))
    return [make_observation(game, "Amazon.pl", price, "PLN", None, url, observed_at, "amazon_search")
            for _, _, price, url in candidates[:1]]


def allegro_api_results(game: dict[str, str], observed_at: str, token: str) -> list[dict[str, Any]]:
    response = get_page(
        f"{ALLEGRO_API}/offers/listing",
        params={"phrase": game["name"], "limit": 20},
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.allegro.public.v1+json", "Accept-Language": "pl-PL"},
    )
    data = response.json()
    candidates: list[tuple[float, Decimal, str, str]] = []
    for item in data.get("items", {}).get("regular", []):
        title = str(item.get("name", ""))
        if not title or is_accessory(title):
            continue
        score = name_score(game["name"], title)
        if score < 0.55:
            continue
        price_data = item.get("sellingMode", {}).get("price", {})
        currency = str(price_data.get("currency", "PLN")).upper()
        price = parse_price(price_data.get("amount"))
        offer_id = str(item.get("id", ""))
        if price is None or currency != "PLN" or not offer_id:
            continue
        candidates.append((score, price, title, f"https://allegro.pl/oferta/{offer_id}"))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    return [make_observation(game, "Allegro", price, "PLN", None, url, observed_at, "allegro_api")
            for _, price, _, url in candidates[:1]]


def allegro_web_results(game: dict[str, str], observed_at: str) -> list[dict[str, Any]]:
    response = get_page(ALLEGRO_SEARCH, params={"string": game["name"]})
    soup = BeautifulSoup(response.text, "html.parser")
    candidates: list[tuple[float, Decimal, str, str]] = []
    for item in soup.select('article, [data-item-id]'):
        title_node = item.select_one("h2, [data-testid*=title], a[href*='/oferta/']")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        if not title or is_accessory(title):
            continue
        score = name_score(game["name"], title)
        if score < 0.55:
            continue
        price_node = item.select_one('[data-testid*=price], [aria-label*=zł], span')
        price = parse_price(price_node.get_text(" ", strip=True) if price_node else None)
        link = item.select_one("a[href*='/oferta/']")
        if price is None or not link:
            continue
        candidates.append((score, price, title, urljoin("https://allegro.pl", link["href"])))
    candidates.sort(key=lambda candidate: (-candidate[0], candidate[1]))
    return [make_observation(game, "Allegro", price, "PLN", None, url, observed_at, "allegro_search")
            for _, price, _, url in candidates[:1]]


def create_allegro_token() -> str | None:
    client_id = os.getenv("ALLEGRO_CLIENT_ID")
    client_secret = os.getenv("ALLEGRO_CLIENT_SECRET")
    if not client_id or not client_secret:
        return None
    response = SESSION.post(
        "https://allegro.pl/auth/oauth/token",
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return str(response.json()["access_token"])


def collect_prices(catalog: dict[str, Any], observed_at: str) -> tuple[list[dict[str, Any]], int]:
    games = catalog["games"]
    if not games:
        return [], 0
    start = int(catalog.get("price_cursor", 0)) % len(games)
    batch_size = min(max(1, PRICE_BATCH_SIZE), len(games))
    batch = [games[(start + offset) % len(games)] for offset in range(batch_size)]
    catalog["price_cursor"] = (start + batch_size) % len(games)
    records: list[dict[str, Any]] = []
    failures = 0
    allegro_token: str | None = None
    allegro_checked = False
    allegro_web_blocked = False

    for index, game in enumerate(batch, 1):
        try:
            response = get_page(game["planszeo_url"])
            records.extend(extract_planszeo(response.text, game, observed_at))
        except (requests.RequestException, ValueError) as error:
            failures += 1
            LOG.warning("Planszeo failed for %s: %s", game["name"], error)
        time.sleep(REQUEST_DELAY_SECONDS)

        try:
            records.extend(amazon_results(game, observed_at))
        except (requests.RequestException, ValueError) as error:
            failures += 1
            LOG.warning("Amazon.pl search failed for %s: %s", game["name"], error)
        time.sleep(REQUEST_DELAY_SECONDS)

        if not allegro_checked:
            try:
                allegro_token = create_allegro_token()
            except (requests.RequestException, ValueError, KeyError) as error:
                LOG.warning("Allegro API token unavailable: %s", error)
            allegro_checked = True
        if allegro_token:
            try:
                records.extend(allegro_api_results(game, observed_at, allegro_token))
            except (requests.RequestException, ValueError) as error:
                failures += 1
                LOG.warning("Allegro API search failed for %s: %s", game["name"], error)
        elif not allegro_web_blocked:
            try:
                records.extend(allegro_web_results(game, observed_at))
            except requests.HTTPError as error:
                if error.response is not None and error.response.status_code in {403, 429}:
                    allegro_web_blocked = True
                    LOG.warning("Allegro search is returning HTTP %s; skipping further web searches. Configure ALLEGRO_CLIENT_ID and ALLEGRO_CLIENT_SECRET to use the official API.", error.response.status_code)
                else:
                    failures += 1
                    LOG.warning("Allegro search failed for %s: %s", game["name"], error)
            except (requests.RequestException, ValueError) as error:
                failures += 1
                LOG.warning("Allegro search failed for %s: %s", game["name"], error)
        time.sleep(REQUEST_DELAY_SECONDS)
        if index % 100 == 0:
            LOG.info("Checked prices for %d/%d games", index, len(batch))
    return records, failures


def save(catalog: dict[str, Any], observations: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with CATALOG_PATH.open("w", encoding="utf-8", newline="\n") as file:
        json.dump(catalog, file, ensure_ascii=False, indent=2)
        file.write("\n")
    if observations:
        with HISTORY_PATH.open("a", encoding="utf-8", newline="\n") as file:
            for observation in observations:
                file.write(json.dumps(observation, ensure_ascii=False) + "\n")


def collect() -> int:
    catalog = load_catalog()
    discover_games(catalog)
    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    observations, failures = collect_prices(catalog, observed_at)
    save(catalog, observations)
    LOG.info("Wrote %d price observations; %d source failures", len(observations), failures)
    return 1 if failures and not observations else 0


if __name__ == "__main__":
    try:
        sys.exit(collect())
    except (OSError, json.JSONDecodeError, ValueError) as error:
        LOG.error("Catalog, configuration, or storage error: %s", error)
        sys.exit(1)
