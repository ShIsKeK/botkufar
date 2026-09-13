# -*- coding: utf-8 -*-
"""
Kufar -> Telegram, версия для GitHub Actions.
Запускается по расписанию: обрабатывает команды из чата, проверяет все поиски,
присылает новые объявления и завершается. Состояние хранится в kufar_data.json.
Токен берётся из переменной окружения BOT_TOKEN (хранится в GitHub Secrets).
"""

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlencode

import requests

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
DATA_FILE = Path("kufar_data.json")
MAX_NEW_PER_SEARCH = 20  # максимум новых объявлений за один запуск (защита от флуда)

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"
KUFAR_API = "https://api.kufar.by/search-api/v2/search/rendered-paginated"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "application/json",
}

REGION_IDS = {
    "минск": 7,
    "минская": 5,
    "брестская": 1,
    "витебская": 2,
    "гомельская": 3,
    "гродненская": 4,
    "могилевская": 6,
    "могилёвская": 6,
}

HELP_TEXT = (
    "👋 Я слежу за новыми объявлениями на Kufar.\n\n"
    "Команды:\n"
    "/add запрос | цена | регион — добавить поиск\n"
    "примеры:\n"
    "  /add iphone 15 | 500-1500 | минск\n"
    "  /add диван | 50-200\n"
    "  /add велосипед\n"
    "/addurl &lt;url&gt; — поиск по точной ссылке из DevTools\n"
    "/list — мои поиски\n"
    "/del N — удалить поиск №N\n"
    "/check — проверить сейчас\n\n"
    "Я запускаюсь автоматически раз в ~10-15 минут, поэтому ответ на команду "
    "может прийти не сразу — это нормально."
)


# ---------- хранение ----------
def load_data():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return {"chats": {}, "offset": None}


DATA = load_data()


def save_data():
    DATA_FILE.write_text(json.dumps(DATA, ensure_ascii=False, indent=1), encoding="utf-8")


def get_searches(chat_id):
    return DATA["chats"].setdefault(str(chat_id), {}).setdefault("searches", [])


# ---------- telegram ----------
def tg(method, **payload):
    r = requests.post(f"{TG_API}/{method}", json=payload, timeout=30)
    if not r.ok:
        print("Telegram error:", method, r.text[:300])
    return r


def send_text(chat_id, text, **kw):
    return tg("sendMessage", chat_id=chat_id, text=text,
              parse_mode="HTML", disable_web_page_preview=True, **kw)


def send_ad(chat_id, ad):
    link = ad.get("ad_link", "")
    text = (f"<b>{ad.get('subject', 'Без названия')}</b>\n"
            f"💰 {format_price(ad)}\n"
            f'🔗 <a href="{link}">Открыть объявление</a>')
    images = ad.get("images") or []
    if images and images[0].get("path"):
        photo = f"https://rms.kufar.by/v1/gallery/{images[0]['path']}"
        r = tg("sendPhoto", chat_id=chat_id, photo=photo, caption=text, parse_mode="HTML")
        if r.ok:
            return
    tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML")


# ---------- kufar ----------
def build_search_url(query, pmin, pmax, rgn):
    params = {"query": query, "sort": "lst.d", "size": "30", "lang": "ru", "cur": "BYR"}
    if pmin is not None or pmax is not None:
        lo = (pmin or 0) * 100
        hi = (pmax if pmax is not None else 99_999_999) * 100
        params["prc"] = f"r:{lo},{hi}"
    if rgn is not None:
        params["rgn"] = str(rgn)
    return f"{KUFAR_API}?{urlencode(params)}"


def fetch_ads(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return r.json().get("ads", [])


def format_price(ad):
    byn = str(ad.get("price_byn") or "0")
    if byn.isdigit() and int(byn) > 0:
        return f"{int(byn) // 100:,} руб.".replace(",", " ")
    return "договорная"


# ---------- логика поисков ----------
def add_search(chat_id, name, url):
    searches = get_searches(chat_id)
    search = {"name": name, "url": url, "seen": []}
    searches.append(search)
    send_text(chat_id, f"✅ Поиск «{name}» добавлен. Смотрю текущую выдачу…")
    try:
        ads = fetch_ads(url)
    except Exception as e:
        send_text(chat_id, f"⚠️ Kufar не ответил ({e}). Поиск сохранён, проверю позже.")
        return
    search["seen"] = [a["ad_id"] for a in ads if a.get("ad_id")]
    for ad in ads[:3]:
        try:
            send_ad(chat_id, ad)
        except Exception as e:
            print("ошибка отправки превью:", e)
        time.sleep(1)
    send_text(chat_id,
              f"☝️ Это 3 свежих из {len(ads)} найденных — проверьте, что выдача совпадает "
              f"с ожиданиями. Дальше буду присылать только новые. /list — список поисков")


def check_chat(chat_id):
    """Проверяет все поиски чата, присылает новые. Возвращает число отправленных."""
    sent = 0
    for s in get_searches(chat_id):
        try:
            ads = fetch_ads(s["url"])
        except Exception as e:
            print(f"[{chat_id}/{s['name']}] ошибка запроса: {e}")
            continue
        seen = set(s.get("seen", []))
        fresh = [a for a in ads if a.get("ad_id") and a["ad_id"] not in seen]
        for ad in reversed(fresh[:MAX_NEW_PER_SEARCH]):  # от старых к новым
            send_ad(chat_id, ad)
            print(f"[{chat_id}/{s['name']}] новое: {ad.get('subject')}")
            sent += 1
            time.sleep(1)
        seen |= {a["ad_id"] for a in ads if a.get("ad_id")}
        s["seen"] = list(seen)[-300:]
    return sent


def check_all():
    for chat_id in list(DATA["chats"]):
        check_chat(int(chat_id))


# ---------- команды ----------
def cmd_add(chat_id, arg):
    parts = [p.strip() for p in arg.split("|")]
    query = parts[0] if parts and parts[0] else ""
    if not query:
        send_text(chat_id, "Формат: /add запрос | цена | регион\n"
                           "Пример: /add iphone 15 | 500-1500 | минск")
        return
    pmin = pmax = None
    rgn = None
    if len(parts) > 1 and parts[1]:
        m = re.fullmatch(r"(\d*)-(\d*)", parts[1].replace(" ", ""))
        if not m or not (m.group(1) or m.group(2)):
            send_text(chat_id, "Цену не понял 🤔 Примеры: 100-500, 100-, -500")
            return
        pmin = int(m.group(1)) if m.group(1) else None
        pmax = int(m.group(2)) if m.group(2) else None
    if len(parts) > 2 and parts[2]:
        key = parts[2].lower().replace("обл.", "").replace("обл", "").strip()
        if key in ("вся", "беларусь", "все"):
            rgn = None
        elif key in REGION_IDS:
            rgn = REGION_IDS[key]
        else:
            send_text(chat_id, "Регион не понял. Варианты: минск, минская, брестская, "
                               "витебская, гомельская, гродненская, могилёвская, вся")
            return
    add_search(chat_id, query, build_search_url(query, pmin, pmax, rgn))


def cmd_list(chat_id):
    searches = get_searches(chat_id)
    if not searches:
        send_text(chat_id, "Пока нет ни одного поиска. /add — добавить")
        return
    lines = [f"{i + 1}. {s['name']}" for i, s in enumerate(searches)]
    send_text(chat_id, "Ваши поиски:\n" + "\n".join(lines) + "\n\n/del N — удалить")


def cmd_del(chat_id, arg):
    searches = get_searches(chat_id)
    try:
        removed = searches.pop(int(arg) - 1)
        send_text(chat_id, f"🗑 Удалён поиск «{removed['name']}»")
    except (ValueError, IndexError):
        send_text(chat_id, "Укажите номер из /list, например: /del 2")


def cmd_addurl(chat_id, arg):
    url = arg.strip()
    if "rendered-paginated" not in url:
        send_text(chat_id, "Нужна ссылка на запрос <code>rendered-paginated</code> из DevTools "
                           "(F12 → Network на странице поиска kufar.by).")
        return
    name = f"поиск {len(get_searches(chat_id)) + 1}"
    add_search(chat_id, name, url)


def cmd_check(chat_id):
    send_text(chat_id, "Проверяю вне очереди…")
    n = check_chat(chat_id)
    send_text(chat_id, f"Готово. Новых объявлений: {n}")


def handle_update(u):
    msg = u.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    text = (msg.get("text") or "").strip()
    if not chat_id or not text:
        return
    if text.startswith("/"):
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd in ("/start", "/help"):
            send_text(chat_id, HELP_TEXT)
        elif cmd == "/add":
            cmd_add(chat_id, arg)
        elif cmd == "/list":
            cmd_list(chat_id)
        elif cmd == "/del":
            cmd_del(chat_id, arg)
        elif cmd == "/addurl":
            cmd_addurl(chat_id, arg)
        elif cmd == "/check":
            cmd_check(chat_id)
        else:
            send_text(chat_id, "Не знаю такую команду. /help")
    else:
        send_text(chat_id, "Я понимаю только команды 🙂 /help")


# ---------- главная логика ----------
def process_updates():
    r = requests.get(f"{TG_API}/getUpdates",
                     params={"timeout": 0, "offset": DATA.get("offset")},
                     timeout=30)
    r.raise_for_status()
    updates = r.json().get("result", [])
    for u in updates:
        DATA["offset"] = u["update_id"] + 1
        try:
            handle_update(u)
        except Exception as e:
            print("ошибка обработки апдейта:", e)
    return len(updates)


def main():
    if not BOT_TOKEN:
        raise SystemExit("Не задан BOT_TOKEN! Добавьте его в GitHub Secrets.")
    n = process_updates()
    print(f"обработано команд: {n}")
    check_all()
    save_data()
    print("готово")


if __name__ == "__main__":
    main()
