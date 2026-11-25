"""Collect ratings from public CarX mobile store pages."""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from fetch_consoles import save_json, upsert_history

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs/projects"
TZ = timezone(timedelta(hours=3))
APPS = {
    "street-mobile": ("carx-street", "1458863319", "com.carxtech.sr"),
    "drift-racing-3": ("carx-drift-racing-3", "1569386864", "com.carxtech.carxdr3"),
    "drift-racing-2": ("carx-drift-racing-2", "1198510863", "com.carxtech.carxdr2"),
    "rally-mobile": ("carx-rally", "1332286428", "com.carxtech.rally"),
}
RUSTORE_ID = "com.carxtech.carxdr2.rustore"
RUSTORE_URL = f"https://www.rustore.ru/catalog/app/{RUSTORE_ID}"
HUAWEI_URL = "https://appgallery.huawei.com/#/app/C102679135"
JSON_SCRIPT = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def parse_apple(html, app_id):
    match = re.search(r'<script[^>]*id="serialized-server-data"[^>]*>(.*?)</script>', html, re.S)
    if not match:
        raise ValueError("App Store: в HTML нет данных витрины")
    page = json.loads(match[1])["data"][0]
    if page["intent"]["id"] != app_id or page["intent"]["storefront"] != "us":
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


def parse_google(html, app_id):
    for match in JSON_SCRIPT.finditer(html):
        app = json.loads(match[1])
        if isinstance(app, dict) and parse_qs(urlparse(app.get("url", "")).query).get("id") == [app_id]:
            rating = app["aggregateRating"]
            break
    else:
        raise ValueError("Google Play: в HTML нет рейтинга приложения")
    average, total = float(rating["ratingValue"]), int(rating["ratingCount"])

    counts = []
    try:
        block = google_page_data(html, "ds:5")[1][2][51]
        if block[2][1] == total and abs(block[0][1] - average) < 0.01:
            counts = [entry[1] for entry in block[1][1:]]
    except (IndexError, TypeError, KeyError):
        pass

    return average, total, counts


def parse_rustore(html, app_id):
    for match in JSON_SCRIPT.finditer(html):
        data = json.loads(match[1])
        for app in data.get("@graph", []):
            if app.get("@type") != "SoftwareApplication":
                continue
            identity = parse_qs(urlparse(app.get("installUrl", "")).query).get("utm_campaign")
            if identity == [app_id]:
                rating = app["aggregateRating"]
                return float(rating["ratingValue"]), int(rating["ratingCount"]), []
    raise ValueError("RuStore: в HTML нет рейтинга приложения")


def fetch_html(url, parse, app_id):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"})
    with urlopen(request, timeout=25) as response:
        return parse(response.read().decode("utf-8"), app_id)


def save_snapshot(project, platform, url, rating):
    average, total, counts = rating
    if not 1 <= average <= 5 or total <= 0:
        raise ValueError("Витрина вернула некорректный рейтинг или счётчик")
    if len(counts) != 5 or not all(isinstance(n, int) and n >= 0 for n in counts) or not sum(counts):
        counts = []

    now = datetime.now(TZ)
    current = {"date": now.date().isoformat(), "avg": round(average, 2), "count": total, "stars": counts}
    path = OUT_DIR / project / f"{platform}.json"
    history = json.loads(path.read_text(encoding="utf-8"))["history"] if path.exists() else []
    save_json(str(path), {
        "source": url, "generated_at": now.isoformat(timespec="minutes"), "current": current,
        "history": upsert_history(history, current),
    })
    print(f"{project}/{platform}: {average:.2f}/5, {total} оценок", flush=True)


def fetch_huawei(page):
    page.goto(HUAWEI_URL, wait_until="domcontentloaded", timeout=30_000)
    consent = page.get_by_text("Accept only essential cookies", exact=False)
    count = page.locator(".score_left .commentators")
    consent.or_(count).first.wait_for(state="visible", timeout=25_000)
    if consent.is_visible():
        consent.click()
    count.wait_for(state="visible", timeout=25_000)
    if not page.get_by_text("CarX Drift Racing 2", exact=True).first.is_visible():
        raise ValueError("AppGallery: открыта другая карточка игры")
    label = count.inner_text()
    match = re.fullmatch(r"Ratings:\s*([\d,]+)", label.strip())
    if not match:
        raise ValueError("AppGallery: счётчик оценок не найден")
    average = page.locator(".score_left .count").inner_text()
    total = int(match[1].replace(",", ""))
    rows = page.locator(".score_right .star").evaluate_all(
        """rows => rows.map(row => ({
            stars: row.querySelectorAll('.starBox img').length,
            filled: row.querySelector('.fill')?.style.flexGrow,
            empty: row.querySelector('.empty')?.style.flexGrow,
        }))"""
    )
    counts = {}
    for row in rows:
        filled, empty = row.get("filled"), row.get("empty")
        if filled and empty and filled.isdecimal() and empty.isdecimal() and int(filled) + int(empty) == total:
            counts[row["stars"]] = int(filled)
    breakdown = [counts[stars] for stars in range(1, 6)] if set(counts) == set(range(1, 6)) and sum(counts.values()) == total else []
    return float(average.strip()), total, breakdown


def collect_huawei():
    from playwright.sync_api import Error, sync_playwright

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            page = browser.new_page(locale="en-US")
            rating = fetch_huawei(page)
        save_snapshot("drift-racing-2", "huawei", HUAWEI_URL, rating)
    except (Error, OSError, ValueError, KeyError) as error:
        print(f"::warning::drift-racing-2/huawei: {error}. Предыдущий снимок сохранён.", file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--huawei", action="store_true", help="Собрать только AppGallery в Chromium")
    if parser.parse_args().huawei:
        return collect_huawei()

    sources = []
    for project, (slug, apple_id, android_id) in APPS.items():
        apple_url = f"https://apps.apple.com/us/app/{slug}/id{apple_id}?see-all=reviews&platform=iphone"
        google_url = f"https://play.google.com/store/apps/details?id={android_id}&hl=en&gl=US"
        sources.extend((
            (project, "ios", apple_url, parse_apple, apple_id),
            (project, "android", google_url, parse_google, android_id),
        ))
    sources.append(("drift-racing-2", "rustore", RUSTORE_URL, parse_rustore, RUSTORE_ID))

    failed = False
    for project, platform, url, parse, app_id in sources:
        try:
            save_snapshot(project, platform, url, fetch_html(url, parse, app_id))
        except (OSError, ValueError, TypeError, KeyError, IndexError) as error:
            print(f"::warning::{project}/{platform}: {error}. Предыдущий снимок сохранён.", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
