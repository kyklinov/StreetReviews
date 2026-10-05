#!/usr/bin/env python3
"""
CarX Street — сборщик оценок и отзывов с консолей (PlayStation Store и Xbox / Microsoft Store).
Версия: 1.1 (2026-10-05) — ссылка на PS Store ведёт на регион US

Что делает:
  PlayStation — берёт со страницы игры среднюю оценку, число оценок и распределение по звёздам.
               Текстовых отзывов в PS Store нет, поэтому каждый день сохраняется снимок,
               и из снимков строится динамика (docs/consoles/playstation.json, поле history).
  Xbox        — обходит все страны магазина: средняя оценка и число оценок по стране,
               плюс все текстовые отзывы (звёзды 1–5, дата, «полезно»). Темы и упоминания игр
               размечаются теми же словарями, что и у Steam (scripts/tags.json, scripts/games.json).
               Результат — docs/consoles/xbox.json.

Если один из магазинов не ответил, его файл не перезаписывается (на сайте остаются прошлые данные),
а скрипт завершается с кодом 0 — обновление Steam от этого не страдает.

Зависимости: только стандартная библиотека Python 3.9+.
Запуск: python scripts/fetch_consoles.py
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPT_VERSION = "1.1"
TZ = timezone(timedelta(hours=3))  # МСК
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "docs", "consoles")
TAGS_PATH = os.path.join(ROOT, "scripts", "tags.json")
GAMES_PATH = os.path.join(ROOT, "scripts", "games.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
PAUSE = float(os.environ.get("CONSOLE_PAUSE", "0.6"))

PS_CONCEPT = "10001917"
PS_URLS = [f"https://store.playstation.com/en-us/concept/{PS_CONCEPT}",
           f"https://store.playstation.com/en-gb/concept/{PS_CONCEPT}",
           f"https://store.playstation.com/nl-nl/concept/{PS_CONCEPT}"]

XBOX_ID = "9NN082LMVV2C"
XBOX_STORE_URL = "https://www.xbox.com/{locale}/games/store/carx-street/9nn082lmvv2c"
EDGE = "https://storeedgefd.dsx.mp.microsoft.com/v9.0"
# Страны магазина Xbox: код → (локаль, название по-русски). Страны без оценок и отзывов на сайт не попадают.
XBOX_MARKETS = {
    "US": ("en-US", "США"), "CA": ("en-CA", "Канада"), "MX": ("es-MX", "Мексика"), "BR": ("pt-BR", "Бразилия"),
    "AR": ("es-AR", "Аргентина"), "CL": ("es-CL", "Чили"), "CO": ("es-CO", "Колумбия"), "PE": ("es-PE", "Перу"),
    "GB": ("en-GB", "Великобритания"), "IE": ("en-IE", "Ирландия"), "DE": ("de-DE", "Германия"), "AT": ("de-AT", "Австрия"),
    "CH": ("de-CH", "Швейцария"), "FR": ("fr-FR", "Франция"), "BE": ("nl-BE", "Бельгия"), "NL": ("nl-NL", "Нидерланды"),
    "LU": ("fr-LU", "Люксембург"), "ES": ("es-ES", "Испания"), "PT": ("pt-PT", "Португалия"), "IT": ("it-IT", "Италия"),
    "PL": ("pl-PL", "Польша"), "CZ": ("cs-CZ", "Чехия"), "SK": ("sk-SK", "Словакия"), "HU": ("hu-HU", "Венгрия"),
    "GR": ("el-GR", "Греция"), "SE": ("sv-SE", "Швеция"), "NO": ("nb-NO", "Норвегия"), "DK": ("da-DK", "Дания"),
    "FI": ("fi-FI", "Финляндия"), "TR": ("tr-TR", "Турция"), "IL": ("he-IL", "Израиль"), "SA": ("ar-SA", "Саудовская Аравия"),
    "AE": ("ar-AE", "ОАЭ"), "ZA": ("en-ZA", "ЮАР"), "AU": ("en-AU", "Австралия"), "NZ": ("en-NZ", "Новая Зеландия"),
    "JP": ("ja-JP", "Япония"), "KR": ("ko-KR", "Южная Корея"), "HK": ("zh-HK", "Гонконг"), "TW": ("zh-TW", "Тайвань"),
    "SG": ("en-SG", "Сингапур"), "IN": ("en-IN", "Индия"), "RU": ("ru-RU", "Россия"), "UA": ("uk-UA", "Украина"),
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_get(url, tries=6, as_json=True):
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
            with urllib.request.urlopen(req, timeout=40) as r:
                raw = r.read().decode("utf-8", "replace")
            time.sleep(PAUSE)
            return json.loads(raw) if as_json else raw
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                raise
            wait = 60 if e.code == 429 else delay
            log(f"  HTTP {e.code}, попытка {attempt}/{tries}, жду {wait}s")
            time.sleep(wait)
        except Exception as e:
            log(f"  ошибка {e!r}, попытка {attempt}/{tries}, жду {delay}s")
            time.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError(f"Не удалось получить {url}")


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, path)


def upsert_history(history, entry, keep_days=800):
    history = [h for h in history if h.get("date") != entry["date"]]
    history.append(entry)
    history.sort(key=lambda h: h["date"])
    return history[-keep_days:]


# ---------------- PlayStation ----------------
def parse_ps_rating(html):
    """Ищет блок оценки в данных страницы (Apollo-кэш Next.js), иначе — в тексте страницы."""
    avg = cnt = None
    dist = {}
    m = re.search(r'"averageRating"\s*:\s*([0-9.]+)', html)
    if m:
        avg = float(m.group(1))
    m = re.search(r'"totalRatingsCount"\s*:\s*(\d+)', html)
    if m:
        cnt = int(m.group(1))
    for name, perc in re.findall(r'"name"\s*:\s*"([1-5])"\s*,\s*"percentage"\s*:\s*([0-9.]+)', html):
        dist[name] = round(float(perc) * (100 if float(perc) <= 1 else 1), 1)
    if avg is None:
        m = re.search(r'([0-9]\.[0-9]{1,2})\s*stars? out of five stars from\s*([0-9.,]+)\s*(K|M)?\s*ratings', html, re.I)
        if m:
            avg = float(m.group(1))
    if cnt is None:
        # «7834 ratings» (без сокращения K/M); берём самое большое число
        nums = [int(re.sub(r"\D", "", x)) for x in re.findall(r'(\d[\d,.\u00a0 ]*\d|\d)\s*ratings\b', html, re.I)]
        cnt = max(nums) if nums else None
    if not dist:
        # пять процентов подряд (5★→1★), которые в сумме дают ~100%
        nums = [int(x) for x in re.findall(r'(\d{1,3})\s*%', html)]
        for i in range(len(nums) - 4):
            win = nums[i:i + 5]
            if 97 <= sum(win) <= 103:
                dist = {star: float(p) for star, p in zip("54321", win)}
                break
    if avg is None or cnt is None:
        return None
    return {"avg": round(avg, 2), "count": cnt, "dist": dist}


def fetch_playstation():
    for url in PS_URLS:
        try:
            html = http_get(url, tries=3, as_json=False)
        except Exception as e:
            log(f"  PS: {url} — {e!r}")
            continue
        r = parse_ps_rating(html)
        if r:
            r["source"] = url
            return r
        log(f"  PS: на {url} не нашёл оценку (возможно, изменилась вёрстка или страница отдаёт заглушку)")
    return None


def update_playstation():
    path = os.path.join(OUT_DIR, "playstation.json")
    cur = fetch_playstation()
    if not cur:
        log("PlayStation: данные не получены — оставляю прошлый файл.")
        print("::warning::PlayStation Store: оценку получить не удалось, на сайте остались прошлые данные")
        return False
    data = load_json(path, {"history": []})
    entry = {"date": today(), "avg": cur["avg"], "count": cur["count"], "dist": cur["dist"]}
    data.update({
        "version": SCRIPT_VERSION,
        "generated_at": datetime.now(TZ).isoformat(timespec="minutes"),
        "store_url": f"https://store.playstation.com/en-us/concept/{PS_CONCEPT}",
        "source": cur["source"],
        "current": entry,
        "history": upsert_history(data.get("history", []), entry),
    })
    save_json(path, data)
    log(f"PlayStation: {cur['avg']} по {cur['count']} оценкам, распределение {cur['dist']}")
    return True


# ---------------- Xbox ----------------
def load_dict(path, key):
    spec = load_json(path, {})
    items = spec.get(key, [])
    out = []
    for t in items:
        try:
            out.append((t["id"], t["name"], re.compile("|".join(f"(?:{p})" for p in t["patterns"]), re.I), t["patterns"]))
        except re.error as e:
            log(f"  пропускаю {t.get('id')}: {e}")
    return out, spec


def xbox_market(code, locale):
    q = urllib.parse.urlencode({"market": code, "locale": locale, "deviceFamily": "Windows.Xbox"})
    try:
        prod = http_get(f"{EDGE}/products/{XBOX_ID}?{q}", tries=3)
    except Exception as e:
        log(f"  Xbox {code}: товар недоступен ({e!r})")
        return None, []
    p = prod.get("Payload") or {}
    avg = float(p.get("AverageRating") or 0)
    cnt = int(p.get("RatingCount") or 0)
    reviews, skip, total = [], 0, None
    while True:
        try:
            j = http_get(f"{EDGE}/ratings/product/{XBOX_ID}?{q}&skipItems={skip}", tries=4)
        except Exception as e:
            log(f"  Xbox {code}: отзывы, страница {skip // 25 + 1} — {e!r}")
            break
        pl = j.get("Payload") or {}
        batch = pl.get("Reviews") or []
        total = pl.get("TotalItems", total)
        reviews.extend(batch)
        skip += len(batch)
        if not batch or (total is not None and skip >= total) or skip > 20000:
            break
    return {"avg": round(avg, 2), "count": cnt, "reviews_total": total or len(reviews)}, reviews


def clean(t, limit=None):
    t = re.sub(r"[ \t]+", " ", (t or "").replace("\r", "")).strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    if limit and len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def update_xbox():
    path = os.path.join(OUT_DIR, "xbox.json")
    tags, _ = load_dict(TAGS_PATH, "tags")
    games, gspec = load_dict(GAMES_PATH, "games")
    nfs_generic = re.compile("|".join(f"(?:{p})" for p in gspec.get("nfs_generic", [])), re.I) if gspec.get("nfs_generic") else None
    nfs_ids = {g[0] for g in games if g[0].startswith("nfs_")}

    markets, all_reviews = [], {}
    for code, (locale, name) in XBOX_MARKETS.items():
        info, reviews = xbox_market(code, locale)
        if info is None:
            continue
        for r in reviews:
            rid = r.get("ReviewId")
            if rid and rid not in all_reviews and not r.get("IsTakenDown"):
                r["_market"] = code
                all_reviews[rid] = r
        if info["count"] or reviews:
            markets.append({"code": code, "locale": locale, "name": name, **info, "reviews_loaded": len(reviews)})
        log(f"  Xbox {code}: {info['avg']} / {info['count']} оценок, {len(reviews)} отзывов")
    if not markets:
        log("Xbox: ни одна страна не ответила — оставляю прошлый файл.")
        print("::warning::Xbox: магазин не ответил, на сайте остались прошлые данные")
        return False

    mi = {m["code"]: i for i, m in enumerate(markets)}
    tag_ids = [t[0] for t in tags]
    game_ids = [g[0] for g in games]
    rows, texts = [], {}
    for r in sorted(all_reviews.values(), key=lambda x: x.get("SubmittedDateTimeUtc") or "", reverse=True):
        if r["_market"] not in mi:
            continue
        title, body = clean(r.get("Title")), clean(r.get("ReviewText"))
        full = (title + "\n" + body).strip() if title and title.lower() not in body.lower()[:len(title) + 5] else body or title
        try:
            ts = int(datetime.fromisoformat((r.get("SubmittedDateTimeUtc") or "")[:19]).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
        tmask = 0
        for i, (_, _, rx, _) in enumerate(tags):
            if rx.search(full):
                tmask |= 1 << i
        gmask, has_nfs_part = 0, False
        for i, (gid, _, rx, _) in enumerate(games):
            if rx.search(full):
                gmask |= 1 << i
                has_nfs_part = has_nfs_part or gid in nfs_ids
        if nfs_generic and not has_nfs_part and nfs_generic.search(full):
            gmask |= 1 << len(games)
        rid = r["ReviewId"]
        rows.append([rid, ts, int(r.get("Rating") or 0), mi[r["_market"]], int(r.get("HelpfulPositive") or 0),
                     int(r.get("HelpfulNegative") or 0), tmask, gmask, 1 if full else 0])
        if full:
            texts[rid] = [clean(full, 600), (r.get("ReviewerName") or "")[:40]]

    stars_total = sum(m["count"] for m in markets)
    avg_total = round(sum(m["avg"] * m["count"] for m in markets) / stars_total, 2) if stars_total else None
    data = load_json(path, {"history": []})
    entry = {"date": today(), "avg": avg_total, "count": stars_total, "reviews": len(rows)}
    data.update({
        "version": SCRIPT_VERSION,
        "generated_at": datetime.now(TZ).isoformat(timespec="minutes"),
        "product_id": XBOX_ID,
        "store_url": XBOX_STORE_URL.format(locale="ru-RU"),
        "store_url_tpl": XBOX_STORE_URL,
        "summary": entry,
        "markets": markets,
        "tags": [{"id": t[0], "name": t[1]} for t in tags],
        "games": [{"id": g[0], "name": g[1], "patterns": g[3]} for g in games],
        "fields": ["id", "ts", "stars", "market", "helpful_pos", "helpful_neg", "tags", "games", "has_text"],
        "rows": rows,
        "texts": texts,
        "history": upsert_history(data.get("history", []), entry),
    })
    save_json(path, data)
    log(f"Xbox: {len(markets)} стран, {stars_total} оценок (средняя {avg_total}), {len(rows)} текстовых отзывов")
    return True


def main():
    log(f"CarX consoles collector v{SCRIPT_VERSION}")
    ok_ps = ok_xb = False
    try:
        ok_ps = update_playstation()
    except Exception as e:
        log(f"PlayStation: ошибка {e!r}")
    try:
        ok_xb = update_xbox()
    except Exception as e:
        log(f"Xbox: ошибка {e!r}")
    log(f"Готово: PlayStation — {'обновлено' if ok_ps else 'без изменений'}, Xbox — {'обновлено' if ok_xb else 'без изменений'}")


if __name__ == "__main__":
    main()
