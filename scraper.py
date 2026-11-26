#!/usr/bin/env python3
"""Fetch structured price metadata from configured Polish board-game pages."""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "games.json"
HISTORY_PATH = ROOT / "data" / "price_history.jsonl"
USER_AGENT = "BoardGamePriceHistory/1.0 (personal daily price tracker)"
REQUEST_DELAY_SECONDS = 2
TIMEOUT_SECONDS = 20

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("price-scraper")


def as_items(value: Any):
    if isinstance(value, list):
        yield from value
    elif isinstance(value, dict):
        yield value


def walk_jsonld(value: Any):
    """Yield Product and Offer-like objects, including nested graph structures."""
    if isinstance(value, list):
        for item in value:
            yield from walk_jsonld(item)
    elif isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(t in {"Product", "Offer", "AggregateOffer"} for t in types):
            yield value
        for key, child in value.items():
            if key in {"@graph", "offers", "mainEntity", "mainEntityOfPage"}:
                yield from walk_jsonld(child)


def parse_price(value: Any) -> Decimal | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^\d,.]", "", str(value)).strip()
    if not cleaned:
        return None
    # Schema.org prices use a dot decimal separator. For human-readable metadata,
    # treat a lone comma as the decimal separator and remove grouping spaces.
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    elif "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    try:
        amount = Decimal(cleaned)
        return amount if amount > 0 else None
    except InvalidOperation:
        return None


def find_offer(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        types = value.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(t in {"Offer", "AggregateOffer"} for t in types):
            return value
        for nested in as_items(value.get("offers")):
            found = find_offer(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = find_offer(nested)
            if found:
                return found
    return None


def extract_product_price(html: str) -> tuple[Decimal | None, bool | None]:
    soup = BeautifulSoup(html, "html.parser")

    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = list(walk_jsonld(data))
        for candidate in candidates:
            offer = find_offer(candidate) or candidate
            currency = str(offer.get("priceCurrency", "PLN")).upper()
            if currency != "PLN":
                continue
            price = parse_price(offer.get("price") or offer.get("lowPrice"))
            if price is not None:
                availability = str(offer.get("availability", "")).rsplit("/", 1)[-1].lower()
                in_stock = True if availability in {"instock", "limitedavailability", "onlineonly"} else (
                    False if availability in {"outofstock", "discontinued", "soldout"} else None
                )
                return price, in_stock

    # Common Schema.org microdata and Open Graph metadata fallback.
    price_node = soup.select_one('[itemprop="price"][content], meta[property="product:price:amount"]')
    if price_node:
        currency_node = soup.select_one('[itemprop="priceCurrency"][content], meta[property="product:price:currency"]')
        currency = (currency_node.get("content", "PLN").upper() if currency_node else "PLN")
        if currency != "PLN":
            return None, None
        price = parse_price(price_node.get("content"))
        if price is not None:
            stock_node = soup.select_one('[itemprop="availability"]')
            stock_text = " ".join((stock_node.get("href", ""), stock_node.get("content", ""))).lower() if stock_node else ""
            in_stock = False if "outofstock" in stock_text else True if "instock" in stock_text else None
            return price, in_stock
    return None, None


def fetch(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("Only absolute HTTPS source URLs are accepted")
    response = requests.get(
        url,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.7"},
        timeout=TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.text


def load_games() -> list[dict[str, Any]]:
    with CONFIG_PATH.open(encoding="utf-8") as file:
        config = json.load(file)
    games = config.get("games", [])
    if not isinstance(games, list):
        raise ValueError('games.json must contain a "games" array')
    for game in games:
        if not all(game.get(field) for field in ("id", "name", "kind")):
            raise ValueError("Every game needs id, name, and kind")
        if game["kind"] not in {"base_game", "expansion"}:
            raise ValueError(f"Unsupported game kind for {game['id']}: {game['kind']}")
        if not isinstance(game.get("sources", []), list):
            raise ValueError(f"sources for {game['id']} must be a list")
    return games


def collect() -> int:
    games = load_games()
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    observations: list[dict[str, Any]] = []
    failures = 0

    for game in games:
        for source in game.get("sources", []):
            label = source.get("shop", urlparse(source.get("url", "")).netloc)
            url = source.get("url", "")
            try:
                price, in_stock = extract_product_price(fetch(url))
                if price is None:
                    raise ValueError("No structured price found (page may need JavaScript or block automated access)")
                observation = {
                    "observed_at": timestamp,
                    "game_id": game["id"],
                    "game_name": game["name"],
                    "kind": game["kind"],
                    "shop": label,
                    "price": str(price.quantize(Decimal("0.01"))),
                    "currency": "PLN",
                    "in_stock": in_stock,
                    "url": url,
                }
                observations.append(observation)
                LOG.info("%s at %s: %s PLN", game["name"], label, observation["price"])
            except (requests.RequestException, ValueError) as exc:
                failures += 1
                LOG.warning("Could not read %s at %s (%s): %s", game["name"], label, url, exc)
            time.sleep(REQUEST_DELAY_SECONDS)

    if observations:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a", encoding="utf-8", newline="\n") as file:
            for observation in observations:
                file.write(json.dumps(observation, ensure_ascii=False) + "\n")
    LOG.info("Recorded %d observations; %d source failures", len(observations), failures)
    return 1 if failures and not observations else 0


if __name__ == "__main__":
    try:
        sys.exit(collect())
    except (OSError, json.JSONDecodeError, ValueError) as error:
        LOG.error("Scraper configuration or storage error: %s", error)
        sys.exit(1)
