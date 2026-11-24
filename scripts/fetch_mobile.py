"""Collect CarX Street Mobile ratings from the public US store pages."""

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fetch_consoles import save_json, upsert_history

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs/projects/street-mobile"
TZ = timezone(timedelta(hours=3))
APPLE_ID = "1458863319"
GOOGLE_ID = "com.carxtech.sr"
APPLE_URL = f"https://apps.apple.com/us/app/carx-street/id{APPLE_ID}?see-all=reviews&platform=iphone"
GOOGLE_URL = f"https://play.google.com/store/apps/details?id={GOOGLE_ID}&hl=en&gl=US"
JSON_SCRIPT = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def parse_apple(html):
    match = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("App Store: в HTML нет данных витрины")
    page = json.loads(match[1])["data"][0]
    if page["intent"]["id"] != APPLE_ID or page["intent"]["storefront"] != "us":
        raise ValueError("App Store: получена другая игра или витрина")
    shelves = page["data"]["shelfMapping"]
    rating = shelves["productRatings"]["items"][0]
    counts = [round(value) for value in reversed(rating.get("ratingCounts") or [])]
    return rating["ratingAverage"], rating["totalNumberOfRatings"], counts


def google_page_data(html, key):
    # The callbacks are embedded in the public HTML. Decode the JSON array without executing JavaScript.
    match = re.search(r"AF_initDataCallback\(\{key: '" + key + r"', hash: '[^']+', data:", html)
    if not match:
        return []
    try:
        return json.JSONDecoder().raw_decode(html[match.end():])[0]
    except ValueError:
        return []


def parse_google(html):
    for match in JSON_SCRIPT.finditer(html):
        app = json.loads(match[1])
        if isinstance(app, dict) and parse_qs(urlparse(app.get("url", "")).query).get("id") == [GOOGLE_ID]:
            rating = app["aggregateRating"]
            break
    else:
        raise ValueError("Google Play: в HTML нет рейтинга CarX Street")
    average, total = float(rating["ratingValue"]), int(rating["ratingCount"])

    counts = []
    try:
        block = google_page_data(html, "ds:5")[1][2][51]
        if block[2][1] == total and abs(block[0][1] - average) < 0.01:
            counts = [entry[1] for entry in block[1][1:]]
    except (IndexError, TypeError, KeyError):
        pass

    return average, total, counts


def collect(platform, url, parse):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(request, timeout=25) as response:
        html = response.read().decode("utf-8")
    average, total, counts = parse(html)
    if not 1 <= average <= 5 or total <= 0:
        raise ValueError("Витрина вернула некорректный рейтинг или счётчик")
    if len(counts) != 5 or not all(isinstance(n, int) and n >= 0 for n in counts) or not sum(counts):
        counts = []

    now = datetime.now(TZ)
    current = {"date": now.date().isoformat(), "avg": round(average, 2), "count": total, "stars": counts}
    path = OUT_DIR / f"{platform}.json"
    history = json.loads(path.read_text(encoding="utf-8"))["history"] if path.exists() else []
    save_json(str(path), {
        "source": url, "generated_at": now.isoformat(timespec="minutes"), "current": current,
        "history": upsert_history(history, current),
    })
    print(f"{platform}: {average:.2f}/5, {total} оценок", flush=True)


def main():
    failed = False
    for platform, url, parse in (("ios", APPLE_URL, parse_apple), ("android", GOOGLE_URL, parse_google)):
        try:
            collect(platform, url, parse)
        except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
            print(f"::warning::{platform}: {error}. Предыдущий снимок сохранён.", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
