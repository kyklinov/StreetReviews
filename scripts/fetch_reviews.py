#!/usr/bin/env python3
"""
CarX Street — сборщик отзывов Steam для дэшборда.
Версия: 1.6 (2026-10-05) — переводится и самый полезный отзыв

Что делает:
  1. Берёт сводку по отзывам (все типы покупок, все языки) и отдельно по каждому языку,
     на который локализована страница игры в Steam.
  2. Выкачивает все отзывы (окнами по месяцу — так Steam отдаёт полный список без обрывов).
  3. Считает метрики и пишет docs/data.json, который читает docs/index.html.

Зависимости: только стандартная библиотека Python 3.9+.
Запуск: python scripts/fetch_reviews.py
"""
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

SCRIPT_VERSION = "1.6"
APP_ID = int(os.environ.get("APP_ID", "1114150"))
TZ = timezone(timedelta(hours=3))  # Москва — границы дней считаем по МСК
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "docs", "data.json")
TAGS_PATH = os.path.join(ROOT, "scripts", "tags.json")
GAMES_PATH = os.path.join(ROOT, "scripts", "games.json")
REQUEST_PAUSE = float(os.environ.get("REQUEST_PAUSE", "1.5"))  # пауза между запросами, сек
UA = "Mozilla/5.0 (compatible; carx-reviews-dashboard/1.0)"

# Названия языков Steam (как в supported_languages) -> код API отзывов
STEAM_LANG_CODES = {
    "english": "english", "russian": "russian", "french": "french", "german": "german",
    "japanese": "japanese", "spanish - spain": "spanish", "spanish - latin america": "latam",
    "portuguese - portugal": "portuguese", "portuguese - brazil": "brazilian", "turkish": "turkish",
    "traditional chinese": "tchinese", "simplified chinese": "schinese", "italian": "italian",
    "korean": "koreana", "polish": "polish", "ukrainian": "ukrainian", "czech": "czech",
    "dutch": "dutch", "danish": "danish", "finnish": "finnish", "norwegian": "norwegian",
    "swedish": "swedish", "hungarian": "hungarian", "romanian": "romanian", "thai": "thai",
    "vietnamese": "vietnamese", "greek": "greek", "bulgarian": "bulgarian", "indonesian": "indonesian",
    "arabic": "arabic",
}
LANG_RU = {
    "english": "Английский", "russian": "Русский", "french": "Французский", "german": "Немецкий",
    "japanese": "Японский", "spanish": "Испанский (Испания)", "latam": "Испанский (Лат. Америка)",
    "portuguese": "Португальский", "brazilian": "Португальский (Бразилия)", "turkish": "Турецкий",
    "tchinese": "Китайский (трад.)", "schinese": "Китайский (упр.)", "italian": "Итальянский",
    "koreana": "Корейский", "polish": "Польский", "ukrainian": "Украинский", "czech": "Чешский",
    "dutch": "Нидерландский", "danish": "Датский", "finnish": "Финский", "norwegian": "Норвежский",
    "swedish": "Шведский", "hungarian": "Венгерский", "romanian": "Румынский", "thai": "Тайский",
    "vietnamese": "Вьетнамский", "greek": "Греческий", "bulgarian": "Болгарский",
    "indonesian": "Индонезийский", "arabic": "Арабский",
}
# Если не удалось прочитать список языков со страницы — используем этот
FALLBACK_LANGS = ["english", "russian", "french", "german", "japanese", "spanish",
                  "portuguese", "turkish", "tchinese", "schinese", "italian"]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def get_json(url, tries=15):
    """GET с ретраями и паузой при 429 (Steam ограничивает частоту запросов)."""
    delay = 5
    for attempt in range(1, tries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                data = json.loads(r.read().decode("utf-8"))
            time.sleep(REQUEST_PAUSE)
            return data
        except urllib.error.HTTPError as e:
            wait = 90 if e.code == 429 else delay  # Steam блокирует на 2–3 минуты
            log(f"  HTTP {e.code}, попытка {attempt}/{tries}, жду {wait}s")
            time.sleep(wait)
        except Exception as e:  # сеть, таймаут, битый JSON
            log(f"  ошибка {e!r}, попытка {attempt}/{tries}, жду {delay}s")
            time.sleep(delay)
        delay = min(delay * 2, 120)
    raise RuntimeError(f"Не удалось получить {url}")


def reviews_url(**params):
    base = {"json": 1, "purchase_type": "all", "language": "all", "num_per_page": 0}
    base.update(params)
    return f"https://store.steampowered.com/appreviews/{APP_ID}?" + urllib.parse.urlencode(base)


def summary(language="all"):
    q = get_json(reviews_url(language=language, filter="all", day_range=9999))["query_summary"]
    return {
        "total": q.get("total_reviews", 0),
        "positive": q.get("total_positive", 0),
        "negative": q.get("total_negative", 0),
        "score_desc": q.get("review_score_desc", ""),
    }


def app_details():
    d = get_json(f"https://store.steampowered.com/api/appdetails?appids={APP_ID}&l=english")
    data = d.get(str(APP_ID), {}).get("data", {})
    langs_raw = data.get("supported_languages", "")
    langs_raw = re.sub(r"<[^>]+>", "", langs_raw)
    langs_raw = langs_raw.split("languages with full audio support")[0]
    codes = []
    for part in langs_raw.split(","):
        name = part.replace("*", "").strip().lower()
        if name in STEAM_LANG_CODES:
            codes.append(STEAM_LANG_CODES[name])
    return {
        "name": data.get("name", "CarX Street"),
        "header_image": data.get("header_image", ""),
        "release_date": (data.get("release_date") or {}).get("date", ""),
        "languages": codes or FALLBACK_LANGS,
    }


def month_windows(start_ts, end_ts):
    """Календарные месяцы (UTC) от start до end включительно."""
    d = datetime.fromtimestamp(start_ts, timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    end = datetime.fromtimestamp(end_ts, timezone.utc)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        yield int(d.timestamp()), int(nxt.timestamp()) - 1
        d = nxt


def fetch_all_reviews(release_ts):
    """Выкачивает все отзывы. Steam обрывает длинную пагинацию, поэтому идём окнами по месяцу."""
    seen = {}
    now_ts = int(time.time())
    for start, end in month_windows(release_ts, now_ts):
        cursor, got, pages = "*", 0, 0
        while True:
            url = reviews_url(filter="recent", num_per_page=100, cursor=cursor,
                              start_date=start, end_date=end, date_range_type="include")
            j = get_json(url)
            batch = j.get("reviews") or []
            pages += 1
            for r in batch:
                seen[r["recommendationid"]] = r
            got += len(batch)
            new_cursor = j.get("cursor")
            if not batch or not new_cursor or new_cursor == cursor or pages > 300:
                break
            cursor = new_cursor
        log(f"  {datetime.fromtimestamp(start, timezone.utc):%Y-%m}: {got} отзывов")
    return list(seen.values())


BBCODE = re.compile(r"\[/?[a-z0-9*]+(?:=[^\]]*)?\]", re.I)


def clean_text(t, limit=None):
    t = BBCODE.sub(" ", t or "")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    if limit and len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0] + "…"
    return t


def review_card(r, limit=700):
    a = r.get("author", {})
    return {
        "id": r["recommendationid"],
        "positive": bool(r.get("voted_up")),
        "language": r.get("language"),
        "language_ru": LANG_RU.get(r.get("language"), r.get("language")),
        "text": clean_text(r.get("review"), limit),
        "created": r.get("timestamp_created"),
        "votes_up": r.get("votes_up", 0),
        "votes_funny": r.get("votes_funny", 0),
        "playtime_h": round((a.get("playtime_at_review") or 0) / 60, 1),
        "free": bool(r.get("received_for_free")),
        "author": a.get("personaname") or "",
        "url": f"https://steamcommunity.com/profiles/{a.get('steamid')}/recommended/{APP_ID}/",
    }


# ---------- Перевод на русский ----------
# Используется бесплатный публичный эндпоинт Google Translate (без ключа).
# Если он недоступен — отзыв просто показывается без перевода, сборка не падает.
TRANSLATE_SECTIONS = ("recent_positive", "recent_negative", "most_helpful")
GOOGLE_LANG = {
    "english": "en", "german": "de", "french": "fr", "spanish": "es", "latam": "es",
    "portuguese": "pt", "brazilian": "pt", "polish": "pl", "turkish": "tr", "italian": "it",
    "schinese": "zh-CN", "tchinese": "zh-TW", "japanese": "ja", "koreana": "ko",
    "ukrainian": "uk", "czech": "cs", "dutch": "nl", "danish": "da", "finnish": "fi",
    "norwegian": "no", "swedish": "sv", "hungarian": "hu", "romanian": "ro", "thai": "th",
    "vietnamese": "vi", "greek": "el", "bulgarian": "bg", "indonesian": "id", "arabic": "ar",
}


def translate_ru(text, steam_lang):
    if not text or steam_lang == "russian" or not re.search(r"[^\W\d_]", text):
        return None
    sl = GOOGLE_LANG.get(steam_lang, "auto")
    url = ("https://translate.googleapis.com/translate_a/single?client=gtx&dt=t&tl=ru&sl="
           + sl + "&q=" + urllib.parse.quote(text[:4500]))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
        out = "".join(part[0] for part in (data[0] or []) if part and part[0])
        time.sleep(0.3)
        out = out.strip()
        return out if out and out.lower() != text.strip().lower() else None
    except Exception as e:
        log(f"  перевод не удался: {e!r}")
        return None


def add_translations(cards):
    for c in cards:
        tr = translate_ru(c.get("text"), c.get("language"))
        if tr:
            c["text_ru"] = tr
    return cards


def load_tags():
    with open(TAGS_PATH, encoding="utf-8") as f:
        spec = json.load(f)
    out = []
    for t in spec["tags"]:
        rx = re.compile("|".join(f"(?:{p})" for p in t["patterns"]), re.I)
        out.append((t["id"], t["name"], rx))
    return out


# ---------- Упоминания других игр ----------
GAMES = []          # [(id, name, regex, patterns)]
NFS_GENERIC = None  # NFS без указания части


def load_games():
    global GAMES, NFS_GENERIC
    if not os.path.exists(GAMES_PATH):
        GAMES, NFS_GENERIC = [], None
        return
    with open(GAMES_PATH, encoding="utf-8") as f:
        spec = json.load(f)
    GAMES = [(g["id"], g["name"], re.compile("|".join(f"(?:{p})" for p in g["patterns"]), re.I), g["patterns"])
             for g in spec["games"]]
    NFS_GENERIC = re.compile("|".join(f"(?:{p})" for p in spec.get("nfs_generic", [])), re.I) if spec.get("nfs_generic") else None


def mark_games(reviews):
    """r["_games"] — id игр, упомянутых в отзыве; r["_nfsg"] — NFS упомянута, но без конкретной части."""
    nfs_ids = {gid for gid, *_ in GAMES if gid.startswith("nfs_")}
    for r in reviews:
        text = r.get("review") or ""
        r["_games"] = [gid for gid, _, rx, _ in GAMES if rx.search(text)]
        r["_nfsg"] = bool(NFS_GENERIC and NFS_GENERIC.search(text) and not nfs_ids.intersection(r["_games"]))


def game_snippet(text, rx, width=420):
    """Фрагмент текста вокруг первого упоминания игры."""
    text = clean_text(text)
    m = rx.search(text)
    if not m or len(text) <= width:
        return clean_text(text, width)
    start = max(0, m.start() - width // 3)
    if start:
        sp = text.find(" ", start)
        start = sp + 1 if 0 <= sp < m.start() else start
    out = text[start:start + width]
    if start + width < len(text):
        out = out.rsplit(" ", 1)[0] + "…"
    return ("…" if start else "") + out


def game_stats(reviews, examples=5, top=5):
    cnt = {gid: [0, 0] for gid, *_ in GAMES}
    gen = [0, 0]
    for r in reviews:
        k = 0 if r.get("voted_up") else 1
        for gid in r.get("_games", ()):
            cnt[gid][k] += 1
        if r.get("_nfsg"):
            gen[k] += 1
    rows = [{"id": gid, "name": name, "pos": cnt[gid][0], "neg": cnt[gid][1]} for gid, name, *_ in GAMES]
    rows.sort(key=lambda g: -(g["pos"] + g["neg"]))
    ex = {}
    by_id = {gid: rx for gid, _, rx, _ in GAMES}
    for g in rows[:top]:
        if not g["pos"] + g["neg"]:
            continue
        items = [r for r in reviews if g["id"] in r.get("_games", ()) and clean_text(r.get("review"))][:examples]
        cards = []
        for r in items:
            c = review_card(r)
            c["text"] = game_snippet(r.get("review"), by_id[g["id"]])
            cards.append(c)
        ex[g["id"]] = cards
    return {"list": rows, "examples": ex, "nfs_generic": {"pos": gen[0], "neg": gen[1]}}


def mark_tags(reviews, tags):
    """Один раз находит темы в каждом отзыве (список id в r["_tags"])."""
    for r in reviews:
        text = r.get("review") or ""
        r["_tags"] = [tid for tid, _, rx in tags if rx.search(text)]


def tag_stats(reviews, tags):
    cnt = {tid: [0, 0] for tid, _, _ in tags}
    for r in reviews:
        for tid in r.get("_tags", ()):
            cnt[tid][0 if r.get("voted_up") else 1] += 1
    return [{"id": tid, "name": name, "pos": cnt[tid][0], "neg": cnt[tid][1]} for tid, name, _ in tags]


def day_key(ts):
    return datetime.fromtimestamp(ts, TZ).strftime("%Y-%m-%d")


def daily_series(reviews, days):
    today = datetime.now(TZ).date()
    keys = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    cnt = defaultdict(lambda: [0, 0])
    for r in reviews:
        k = day_key(r["timestamp_created"])
        cnt[k][0 if r.get("voted_up") else 1] += 1
    return [{"date": k, "pos": cnt[k][0], "neg": cnt[k][1]} for k in keys]


def pct(p, n):
    return round(100 * p / (p + n), 1) if (p + n) else None


def write_period_files(reviews, tags, out_dir):
    """Данные для фильтра по периоду на странице.
    reviews/index.json — по строке на отзыв: id, время, оценка, язык, бесплатно, «полезно», «забавно», темы (битовая маска), есть ли текст.
    reviews/text-ГГГГ-ММ.json — тексты (до 600 знаков), автор, steamid (сжатый), часы в игре; страница грузит только нужные месяцы."""
    os.makedirs(out_dir, exist_ok=True)
    langs = sorted({r.get("language") or "" for r in reviews})
    li = {l: i for i, l in enumerate(langs)}
    tag_ids = [tid for tid, _, _ in tags]
    game_ids = [gid for gid, *_ in GAMES]
    rows, texts = [], defaultdict(dict)
    for r in sorted(reviews, key=lambda x: x["timestamp_created"], reverse=True):
        mask = 0
        for tid in r.get("_tags", ()):
            mask |= 1 << tag_ids.index(tid)
        gmask = 0
        for gid in r.get("_games", ()):
            gmask |= 1 << game_ids.index(gid)
        if r.get("_nfsg"):
            gmask |= 1 << len(game_ids)          # последний бит — NFS без указания части
        text = clean_text(r.get("review"), 600)
        rows.append([r["recommendationid"], r["timestamp_created"], 1 if r.get("voted_up") else 0, li[r.get("language") or ""],
                     1 if r.get("received_for_free") else 0, r.get("votes_up", 0), r.get("votes_funny", 0), mask, 1 if text else 0,
                     gmask])
        if text:
            a = r.get("author", {})
            month = datetime.fromtimestamp(r["timestamp_created"], TZ).strftime("%Y-%m")
            sid = int(a.get("steamid") or 0) - 76561197960265728
            texts[month][r["recommendationid"]] = [text, (a.get("personaname") or "")[:40], base36(max(sid, 0)),
                                                   round((a.get("playtime_at_review") or 0) / 60, 1)]
    first = min((r["timestamp_created"] for r in reviews), default=None)
    index = {"generated_at": datetime.now(TZ).isoformat(timespec="minutes"),
             "first_date": datetime.fromtimestamp(first, TZ).strftime("%Y-%m-%d") if first else None,
             "langs": langs, "tags": [{"id": tid, "name": name} for tid, name, _ in tags],
             "games": [{"id": gid, "name": name, "patterns": pats} for gid, name, _, pats in GAMES],
             "fields": ["id", "ts", "up", "lang", "free", "votes_up", "votes_funny", "tags", "has_text", "games"], "rows": rows}
    with open(os.path.join(out_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))
    keep = set()
    for month, items in texts.items():
        name = f"text-{month}.json"
        keep.add(name)
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, separators=(",", ":"))
    for name in os.listdir(out_dir):          # месяцы, в которых не осталось отзывов
        if name.startswith("text-") and name not in keep:
            os.remove(os.path.join(out_dir, name))
    return index["first_date"]


def base36(n):
    chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = chars[r] + out
        if not n:
            return out


def build_slice(reviews, summ, tags):
    """Все метрики дэшборда для набора отзывов (все языки или один язык).
    summ — сводка Steam для этого набора: total / positive / negative / score_desc."""
    pos_reviews = [r for r in reviews if r.get("voted_up")]
    neg_reviews = [r for r in reviews if not r.get("voted_up")]
    free = [r for r in reviews if r.get("received_for_free")]
    free_pos = sum(1 for r in free if r.get("voted_up"))
    most_helpful = max(reviews, key=lambda r: (r.get("votes_up", 0), r.get("votes_funny", 0))) if reviews else None
    cutoff_30 = time.time() - 30 * 86400
    last30 = [r for r in reviews if r["timestamp_created"] >= cutoff_30]
    return {
        "summary": {"total": summ["total"], "positive": summ["positive"], "negative": summ["negative"],
                    "score_desc": summ.get("score_desc", ""), "pct": pct(summ["positive"], summ["negative"])},
        "free": {"total": len(free), "positive": free_pos, "negative": len(free) - free_pos,
                 "pct": pct(free_pos, len(free) - free_pos)},
        "daily30": daily_series(reviews, 30),
        "most_helpful": review_card(most_helpful, limit=3000) if most_helpful else None,
        "recent_positive": [review_card(r) for r in pos_reviews if clean_text(r.get("review"))][:10],
        "recent_negative": [review_card(r) for r in neg_reviews if clean_text(r.get("review"))][:10],
        "tags": {"all": tag_stats(reviews, tags), "d30": tag_stats(last30, tags)},
        "games": game_stats(reviews),
        "last30_total": {"positive": sum(1 for r in last30 if r.get("voted_up")),
                         "negative": sum(1 for r in last30 if not r.get("voted_up"))},
    }


def main():
    t0 = time.time()
    log(f"CarX reviews collector v{SCRIPT_VERSION}, app {APP_ID}")
    info = app_details()
    log("Языки страницы:", ", ".join(info["languages"]))

    total = summary("all")
    log("Сводка:", total)

    languages = []
    for code in info["languages"]:
        s = summary(code)
        s.update({"code": code, "name": LANG_RU.get(code, code), "pct": pct(s["positive"], s["negative"])})
        languages.append(s)
    languages.sort(key=lambda x: -x["total"])

    # Дата первого отзыва ≈ релиз. Берём из ответа API (app_release_date), иначе — август 2024.
    probe = get_json(reviews_url(filter="recent", num_per_page=1))
    release_ts = int((probe.get("reviews") or [{}])[0].get("app_release_date") or 1722470400)
    release_ts -= 31 * 86400  # запас на случай предзаказов / раннего доступа

    log("Скачиваю отзывы…")
    reviews = fetch_all_reviews(release_ts)
    log(f"Скачано {len(reviews)} из {total['total']}")
    if total["total"] and len(reviews) < 0.8 * total["total"]:
        log("ВНИМАНИЕ: скачано меньше 80% отзывов — Steam мог ограничить запросы.")

    reviews.sort(key=lambda r: r["timestamp_created"], reverse=True)
    tags = load_tags()
    mark_tags(reviews, tags)
    load_games()
    mark_games(reviews)

    # Языки, на которые страница не локализована, — одной строкой (разница со сводкой)
    lang_counts = Counter(r.get("language") for r in reviews)
    covered = {l["code"] for l in languages}
    other_pos = max(0, total["positive"] - sum(l["positive"] for l in languages))
    other_neg = max(0, total["negative"] - sum(l["negative"] for l in languages))
    ea = [r for r in reviews if r.get("written_during_early_access")]

    data = {
        "version": SCRIPT_VERSION,
        "generated_at": datetime.now(TZ).isoformat(timespec="minutes"),
        "app": {"id": APP_ID, "name": info["name"], "header_image": info["header_image"],
                "url": f"https://store.steampowered.com/app/{APP_ID}/",
                "release_date": info["release_date"]},
        "fetched": {"count": len(reviews),
                    "coverage": round(100 * len(reviews) / total["total"], 1) if total["total"] else None,
                    "seconds": round(time.time() - t0)},
        "languages": languages,
        "languages_other": {"positive": other_pos, "negative": other_neg, "total": other_pos + other_neg,
                            "pct": pct(other_pos, other_neg),
                            "count_langs": len([l for l in lang_counts if l not in covered])},
        "early_access": {"total": len(ea), "positive": sum(1 for r in ea if r.get("voted_up"))},
    }
    # Срез «все языки» лежит на верхнем уровне (summary, free, daily30, tags, отзывы…)
    data.update(build_slice(reviews, total, tags))
    data["games_meta"] = [{"id": gid, "name": name, "patterns": pats} for gid, name, _, pats in GAMES]

    log("Перевожу отзывы на русский…")
    for key in TRANSLATE_SECTIONS:
        if isinstance(data.get(key), list):
            add_translations(data[key])
        elif isinstance(data.get(key), dict):
            add_translations([data[key]])

    # Срезы по каждому языку локализации — для фильтра на странице.
    # Переводы переиспользуются из общего среза; остальные страница переводит сама при показе.
    known_tr = {}
    for key in ("recent_positive", "recent_negative", "most_helpful"):
        items = data.get(key) if isinstance(data.get(key), list) else [data.get(key)]
        for c in items:
            if c and c.get("text_ru"):
                known_tr[c["id"]] = c["text_ru"]
    by_lang = defaultdict(list)
    for r in reviews:
        by_lang[r.get("language")].append(r)
    data["by_language"] = {}
    for l in languages:
        sl = build_slice(by_lang.get(l["code"], []), l, tags)
        for key in ("recent_positive", "recent_negative", "most_helpful"):
            items = sl.get(key) if isinstance(sl.get(key), list) else [sl.get(key)]
            for c in items:
                if c and c["id"] in known_tr:
                    c["text_ru"] = known_tr[c["id"]]
        data["by_language"][l["code"]] = sl

    log("Готовлю данные для фильтра по периоду…")
    data["first_review_date"] = write_period_files(reviews, tags, os.path.join(os.path.dirname(OUT_PATH), "reviews"))

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    log(f"Готово: {OUT_PATH} ({round(time.time() - t0)} c)")


if __name__ == "__main__":
    main()
