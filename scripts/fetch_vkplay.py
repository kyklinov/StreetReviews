#!/usr/bin/env python3
"""
CarX Street — сборщик отзывов VK Play.
Версия: 1.0 (2026-10-05)

Что делает:
  Берёт из открытого API VK Play (то же, что использует страница игры, вход не нужен):
    - сводку оценок: средняя (из 10), число положительных / нейтральных / отрицательных
      (play/microreviews/stat);
    - все текстовые отзывы: оценка 1–10, дата, 👍 / 👎, время в игре, автор (play/microreviews_v2).
  Темы и упоминания игр размечаются теми же словарями, что у Steam (scripts/tags.json, scripts/games.json).
  Каждый день сохраняется снимок сводки (поле history) — из него строится динамика средней оценки.
  Результат — docs/vkplay/vkplay.json.

Деление по баллам — как у самого VK Play: 6–10 положительные, 5 нейтральные, 1–4 отрицательные
(проверено сверкой со сводкой VK Play 05.10.2026: 135 / 9 / 31).
Отзывы без оценки (VK Play скрывает оценку у тех, кто наиграл меньше 2 часов) в группы не входят.

Если VK Play не ответил (например, не пускает зарубежные адреса GitHub Actions), файл не
перезаписывается — на сайте остаются последние собранные данные, скрипт завершается с кодом 0.

Зависимости: только стандартная библиотека Python 3.9+.
Запуск: python scripts/fetch_vkplay.py
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

SCRIPT_VERSION = "1.0"
TZ = timezone(timedelta(hours=3))  # МСК — даты в API VK Play указаны по Москве
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "vkplay", "vkplay.json")
TAGS_PATH = os.path.join(ROOT, "scripts", "tags.json")
GAMES_PATH = os.path.join(ROOT, "scripts", "games.json")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0 Safari/537.36"
PAUSE = float(os.environ.get("VK_PAUSE", "0.7"))

GAME_ID = 34754
GAME_SLUG = "carx-street-34754"
STORE_URL = f"https://vkplay.ru/play/game/{GAME_SLUG}/"
REVIEWS_URL = f"https://vkplay.ru/play/game/{GAME_SLUG}/reviews/"
API = "https://api.vkplay.ru/play"
LANGS = ("ru_RU", "en_US")  # отзывы приходят по языку; берём оба и склеиваем


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def http_json(url, tries=5):
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": UA, "Accept": "application/json", "Accept-Language": "ru-RU,ru;q=0.9",
                "Origin": "https://vkplay.ru", "Referer": REVIEWS_URL})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            time.sleep(PAUSE)
            return data
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404):
                raise
            log(f"  HTTP {e.code}, попытка {attempt}/{tries}, жду {delay}s")
        except Exception as e:
            log(f"  ошибка {e!r}, попытка {attempt}/{tries}, жду {delay}s")
        time.sleep(delay)
        delay = min(delay * 2, 60)
    raise RuntimeError(f"Не удалось получить {url}")


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


def load_dict(path, key):
    spec = load_json(path, {})
    out = []
    for t in spec.get(key, []):
        try:
            out.append((t["id"], t["name"], re.compile("|".join(f"(?:{p})" for p in t["patterns"]), re.I), t["patterns"]))
        except re.error as e:
            log(f"  пропускаю {t.get('id')}: {e}")
    return out, spec


def clean(text, limit=None):
    t = re.sub(r"https?://\S+", "[ссылка]", (text or "").replace("\r", "")).strip()
    t = re.sub(r"\n{3,}", "\n\n", t)
    if limit and len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def fetch_stat():
    q = urllib.parse.urlencode({"game_id": GAME_ID, "duration": "all", "lang": "ru_RU"})
    s = http_json(f"{API}/microreviews/stat/?{q}")
    return {"avg": s.get("avg_rating"), "high": int(s.get("count_high") or 0), "medium": int(s.get("count_medium") or 0),
            "low": int(s.get("count_low") or 0), "langs": s.get("lang") or {}}


def fetch_reviews():
    out = {}
    for lang in LANGS:
        q = urllib.parse.urlencode({"isPopup": "false", "game_id": GAME_ID, "header_lang": "ru_RU", "filter": "new",
                                    "lang": lang, "duration": "all", "limit": 50})
        url, pages = f"{API}/microreviews_v2/?{q}", 0
        while url and pages < 400:
            j = http_json(url)
            for r in j.get("results") or []:
                if r.get("is_published", True):
                    out[r["id"]] = r
            url, pages = j.get("next"), pages + 1
        log(f"  {lang}: {pages} стр., всего отзывов в наборе {len(out)}")
    return list(out.values())


def build(reviews, stat, prev):
    tags, _ = load_dict(TAGS_PATH, "tags")
    games, gspec = load_dict(GAMES_PATH, "games")
    nfs_generic = re.compile("|".join(f"(?:{p})" for p in gspec.get("nfs_generic", [])), re.I) if gspec.get("nfs_generic") else None
    nfs_ids = {g[0] for g in games if g[0].startswith("nfs_")}
    rows, texts = [], {}
    for r in sorted(reviews, key=lambda x: x.get("date_added") or "", reverse=True):
        try:
            ts = int(datetime.strptime(r["date_added"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ).timestamp())
        except (KeyError, ValueError):
            continue
        full = clean(r.get("text"))
        tmask = 0
        for i, (_, _, rx, _) in enumerate(tags):
            if rx.search(full):
                tmask |= 1 << i
        gmask, has_part = 0, False
        for i, (gid, _, rx, _) in enumerate(games):
            if rx.search(full):
                gmask |= 1 << i
                has_part = has_part or gid in nfs_ids
        if nfs_generic and not has_part and nfs_generic.search(full):
            gmask |= 1 << len(games)
        a = r.get("author") or {}
        score = 0 if r.get("rating") is None or r.get("rating_is_hidden") else int(r["rating"])
        hours = round((a.get("time_spend") or 0) / 3600, 1)
        rows.append([r["id"], ts, score, int(r.get("likes") or 0), int(r.get("dislikes") or 0), tmask, gmask,
                     1 if full else 0, hours, 0 if (r.get("lang") or "ru_RU") == "ru_RU" else 1])
        if full:
            texts[str(r["id"])] = [clean(full, 600), (a.get("nick") or "Игрок VK Play")[:40]]
    total = stat["high"] + stat["medium"] + stat["low"]
    entry = {"date": datetime.now(TZ).strftime("%Y-%m-%d"), "avg": stat["avg"], "count": total,
             "high": stat["high"], "medium": stat["medium"], "low": stat["low"], "reviews": len(rows)}
    data = dict(prev)
    data.update({
        "version": SCRIPT_VERSION,
        "generated_at": datetime.now(TZ).isoformat(timespec="minutes"),
        "game_id": GAME_ID,
        "store_url": STORE_URL,
        "reviews_url": REVIEWS_URL,
        "groups": {"pos": [6, 10], "neu": [5, 5], "neg": [1, 4]},
        "summary": entry,
        "tags": [{"id": t[0], "name": t[1]} for t in tags],
        "games": [{"id": g[0], "name": g[1], "patterns": g[3]} for g in games],
        "fields": ["id", "ts", "score", "likes", "dislikes", "tags", "games", "has_text", "hours", "foreign"],
        "rows": rows,
        "texts": texts,
        "history": upsert_history(prev.get("history", []), entry),
    })
    return data


def main():
    log(f"CarX VK Play collector v{SCRIPT_VERSION}")
    try:
        stat = fetch_stat()
        reviews = fetch_reviews()
    except Exception as e:
        log(f"VK Play: ошибка {e!r}")
        print("::warning::VK Play: данные получить не удалось, на сайте остались прошлые данные")
        return
    if not reviews:
        print("::warning::VK Play: отзывы не пришли, на сайте остались прошлые данные")
        return
    data = build(reviews, stat, load_json(OUT_PATH, {"history": []}))
    save_json(OUT_PATH, data)
    s = data["summary"]
    log(f"VK Play: средняя {s['avg']}, {s['count']} оценок ({s['high']}/{s['medium']}/{s['low']}), {len(data['rows'])} отзывов")


if __name__ == "__main__":
    main()
