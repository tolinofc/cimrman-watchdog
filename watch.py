#!/usr/bin/env python3
"""Hlídá zpětně uvolněná místa na představení Žižkovského divadla Járy Cimrmana.

Data bere z veřejného API GoOut (žádný klíč, žádné přihlášení):
  1. /services/entities/v1/schedules  – seznam budoucích představení a jejich prodejů
  2. /services/feeder/v3/sales        – stav každého jednotlivého sedadla v sále

Když se sedadlo, které bylo při minulé kontrole obsazené, objeví jako volné,
pošle zprávu do Telegramu.
"""

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PERFORMER_ID = os.environ.get("PERFORMER_ID", "2294080")  # Divadlo Járy Cimrmana
SERVICES = "https://goout.net/services"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
try:
    TZ = ZoneInfo("Europe/Prague")
except Exception:  # Windows bez balíčku tzdata – použije se místní čas počítače
    TZ = None

# Prodeje v těchhle stavech nejdou na GoOut koupit, jejich sedadla neřešíme.
INACTIVE_SALE_STATES = {"HIDDEN", "CANCELLED", "CANCELED", "ENDED", "EXPIRED", "DRAFT", "DELETED"}

TITLE_FILTER = [t.strip().lower() for t in os.environ.get("TITLE_FILTER", "").split(",") if t.strip()]
MIN_SEATS = int(os.environ.get("MIN_SEATS", "1"))
SOURCE_LABEL = os.environ.get("SOURCE_LABEL", "")
# Hlásit i plánované spuštění prodeje (17. v 17:00 apod.)? Výchozí: ne, jen zpětně uvolněná místa.
NOTIFY_SALE_START = os.environ.get("NOTIFY_SALE_START", "0") == "1"
# Na PC: při uvolnění rovnou otevřít stránku nákupu ve výchozím prohlížeči.
OPEN_BROWSER = os.environ.get("OPEN_BROWSER", "0") == "1"
# Kolik míst předvybrat v odkazu (userscript goout-predvyber.user.js je pak nakliká). 0 = vypnuto.
PICK_COUNT = max(0, min(4, int(os.environ.get("PICK_COUNT", "2"))))


def log(msg):
    print(f"[{datetime.now(TZ):%H:%M:%S}] {msg}", flush=True)


def get_json(url, attempts=4):
    last = None
    for attempt in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ConnectionError) as exc:
            last = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GoOut API selhalo po {attempts} pokusech: {url}\n{last}")


def parse_dt(value):
    """ISO čas z API -> naivní datetime v pražské zóně."""
    if not value:
        return None
    return datetime.fromisoformat(value).astimezone(TZ).replace(tzinfo=None)


def now():
    return datetime.now(TZ).replace(tzinfo=None)


# ---------------------------------------------------------------- data z API

def fetch_shows():
    """Budoucí představení: {sale_id: {...}}."""
    shows = {}
    scroll = None
    for _ in range(20):
        params = [
            ("languages[]", "cs"),
            ("grouped", "false"),
            ("performerIds[]", PERFORMER_ID),
            ("limit", "100"),
            ("include", "events,venues,sales"),
        ]
        if scroll:
            params.append(("scrollId", scroll))
        data = get_json(f"{SERVICES}/entities/v1/schedules?{urllib.parse.urlencode(params)}")
        inc = data.get("included", {})
        events = {e["id"]: e for e in inc.get("events", [])}
        venues = {v["id"]: v for v in inc.get("venues", [])}
        sales = {s["id"]: s for s in inc.get("sales", [])}
        for sch in data.get("schedules", []):
            rel = sch["relationships"]
            sale_ref = rel.get("sale")
            if not sale_ref or sale_ref["id"] not in sales:
                continue
            start = parse_dt(sch["attributes"]["startAt"])
            if start and start <= now():
                continue
            ev = events.get((rel.get("event") or {}).get("id"), {})
            ve = venues.get((rel.get("venue") or {}).get("id"), {})
            title = ev.get("locales", {}).get("cs", {}).get("name") or "?"
            if TITLE_FILTER and not any(t in title.lower() for t in TITLE_FILTER):
                continue
            sale = sales[sale_ref["id"]]
            shows[str(sale["id"])] = {
                "title": title,
                "start": start,
                "venue": ve.get("locales", {}).get("cs", {}).get("name") or "",
                "url": sale["attributes"].get("saleUrl") or sch["locales"].get("cs", {}).get("siteUrl"),
            }
        scroll = (data.get("meta") or {}).get("nextScrollId")
        if not scroll:
            break
    return shows


def fetch_seats(sale_ids):
    """Pro každý prodej: volná sedadla a kategorie, které jsou právě v prodeji.

    Vrací {sale_id: {"seats": {seat_id: deal_id}, "deals": {deal_id: {...}}, "state": str}}.
    Počítají se jen sedadla v kategoriích, které jsou veřejné a jejichž prodej už začal
    (vozíčkářská místa, abonmá apod. jsou na GoOut skrytá).
    """
    result = {}
    ids = list(sale_ids)
    for i in range(0, len(ids), 20):
        chunk = ",".join(ids[i:i + 20])
        data = get_json(f"{SERVICES}/feeder/v3/sales?saleIds={chunk}&details=false&language=CS")
        for sid, sale in (data.get("sales") or {}).items():
            state = (sale.get("state") or "").upper()
            on_sale = {}
            if state not in INACTIVE_SALE_STATES:
                for did, deal in (sale.get("deals") or {}).items():
                    start = parse_dt(deal.get("start"))
                    if deal.get("hidden") or not deal.get("active"):
                        continue
                    if start and start > now():
                        continue
                    on_sale[str(did)] = {
                        "name": deal.get("name") or "?",
                        "price": (deal.get("priceCents") or 0) // 100,
                    }
            seats = {}
            for hall in sale.get("halls") or []:
                for seat in hall.get("seats") or []:
                    did = str(seat.get("dealId"))
                    if seat.get("available") and seat.get("valid", 1) and did in on_sale:
                        seats[str(seat["id"])] = did
            result[str(sid)] = {"seats": seats, "deals": on_sale, "state": state,
                                "hall_id": sale.get("hallId")}
    return result


_HALLS = {}


def hall_layout(hall_id):
    """Plánek sálu: {seat_id: (blok, řada, číslo)}. Načte se jednou a drží v paměti."""
    if not hall_id:
        return {}
    if hall_id not in _HALLS:
        try:
            data = get_json(f"{SERVICES}/feeder/halls/v1/hall?hallId={hall_id}&language=cs&details=true", attempts=2)
            blocks = {str(k): v for k, v in (data.get("blocks") or {}).items()}
            layout = {}
            for seat in data.get("seats") or []:
                block = blocks.get(str(seat.get("blockId")), {})
                layout[str(seat["id"])] = (
                    block.get("localText") or block.get("name") or "",
                    seat.get("row") if block.get("hasRows", True) else None,
                    seat.get("index"),
                )
            _HALLS[hall_id] = layout
        except Exception as exc:
            log(f"Plánek sálu {hall_id} se nepodařilo načíst: {exc}")
            return {}
    return _HALLS[hall_id]


def describe_seats(seat_ids, layout):
    """['Přízemí vlevo, řada 5: 7, 8 ⬅ vedle sebe', ...]"""
    groups = defaultdict(list)
    unknown = 0
    for sid in seat_ids:
        info = layout.get(str(sid))
        if not info:
            unknown += 1
            continue
        block, row, num = info
        groups[(block, row)].append(num)
    out = []
    for (block, row), nums in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        nums = sorted(n for n in nums if n is not None)
        pair = any(b - a == 1 for a, b in zip(nums, nums[1:]))
        where = f"{block}, řada {row}" if row is not None else block
        out.append(f"{where}: místo {', '.join(map(str, nums))}" + (" (vedle sebe)" if pair else ""))
    if unknown:
        out.append(f"+{unknown} míst bez plánku")
    return out


def pick_seats(seat_ids, layout, count):
    """Vybere až `count` míst k předvýběru – přednostně souvislou řadu vedle sebe."""
    if count <= 0 or not seat_ids:
        return []
    def key(sid):
        block, row, num = layout.get(str(sid), ("~", None, None))
        return (block, row or 0, num or 0)
    ordered = sorted(seat_ids, key=key)
    best = ordered[:1]
    run = ordered[:1]
    for prev, cur in zip(ordered, ordered[1:]):
        a, b = key(prev), key(cur)
        if a[:2] == b[:2] and b[2] - a[2] == 1 and str(cur) in layout:
            run = run + [cur]
        else:
            run = [cur]
        if len(run) > len(best):
            best = run
        if len(best) >= count:
            break
    if len(best) < count:  # nejsou vedle sebe – doplň dalšími
        best = best + [x for x in ordered if x not in best][: count - len(best)]
    return best[:count]


def buy_url(alert):
    url = alert["show"]["url"]
    if not url or not alert["freed"]:
        return url
    seat_ids = [sid for ids in alert["freed"].values() for sid in ids]
    picked = pick_seats(seat_ids, hall_layout(alert.get("hall_id")), PICK_COUNT)
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}vybrat={','.join(picked)}" if picked else url


# ---------------------------------------------------------------- porovnání

def diff(prev, current, shows):
    """Najde sedadla, která byla obsazená a teď jsou volná."""
    alerts = []
    for sid, cur in current.items():
        if sid not in shows:
            continue
        old = prev.get(sid)
        if old is None:
            # Představení vidíme poprvé (nově vypsané) – nehlásíme, jen si ho zapamatujeme.
            continue
        old_seats = set(old.get("seats", []))
        old_deals = set(old.get("deals", []))
        freed = defaultdict(list)
        opened = defaultdict(list)
        for seat_id, did in cur["seats"].items():
            if seat_id in old_seats:
                continue
            if did in old_deals:
                freed[did].append(seat_id)
            else:
                opened[did].append(seat_id)  # kategorie právě spustila prodej
        if not NOTIFY_SALE_START:
            opened.clear()
        if sum(map(len, freed.values())) >= MIN_SEATS or opened:
            alerts.append({
                "sale_id": sid,
                "show": shows[sid],
                "freed": dict(freed),
                "opened": dict(opened),
                "deals": cur["deals"],
                "available_total": len(cur["seats"]),
                "hall_id": cur.get("hall_id"),
            })
    return alerts


def format_alert(a):
    s = a["show"]
    when = s["start"].strftime("%a %d. %m. %Y %H:%M") if s["start"] else "?"
    for en, cz in zip(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], ["po", "út", "st", "čt", "pá", "so", "ne"]):
        when = when.replace(en, cz)
    lines = []
    if a["freed"]:
        lines.append(f"🎭 <b>Uvolněná místa: {sum(map(len, a['freed'].values()))}</b>")
    else:
        lines.append("🟢 <b>Spuštěn prodej</b>")
    lines.append(f"<b>{html.escape(s['title'])}</b> – {when}")
    layout = hall_layout(a.get("hall_id"))
    for label, group in (("uvolněno", a["freed"]), ("nově v prodeji", a["opened"])):
        for did, seat_ids in sorted(group.items(), key=lambda kv: a["deals"][kv[0]]["name"]):
            d = a["deals"][did]
            lines.append(f"• <b>{html.escape(d['name'])}</b> ({d['price']} Kč): {len(seat_ids)}× {label}")
            for where in describe_seats(seat_ids, layout)[:8]:
                lines.append(f"   {html.escape(where)}")
    lines.append(f"Volno celkem: {a['available_total']}")
    if SOURCE_LABEL:
        lines.append(f"<i>{html.escape(SOURCE_LABEL)}</i>")
    return "\n".join(lines)


def format_status(shows, current):
    lines = []
    for sid, s in sorted(shows.items(), key=lambda kv: kv[1]["start"] or datetime.max):
        cur = current.get(sid, {"seats": {}, "deals": {}, "state": "?"})
        per = defaultdict(int)
        for did in cur["seats"].values():
            per[cur["deals"][did]["name"]] += 1
        detail = ", ".join(f"{k}: {v}" for k, v in sorted(per.items())) or "nic volného"
        when = s["start"].strftime("%d.%m. %H:%M") if s["start"] else "?"
        lines.append(f"{when}  {s['title']}  [{cur['state']}]  {detail}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram

def telegram(text, button_url=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID nejsou nastavené, zpráva jen do konzole:")
        print(text, flush=True)
        if button_url:
            print(f"[tlačítko Koupit -> {button_url}]", flush=True)
        return
    fields = {
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }
    if button_url:
        fields["reply_markup"] = json.dumps(
            {"inline_keyboard": [[{"text": "🎟 Koupit na GoOut", "url": button_url}]]})
    body = urllib.parse.urlencode(fields).encode()
    for attempt in range(3):
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as exc:
            log(f"Telegram vrátil {exc.code}: {exc.read().decode('utf-8', 'replace')}")
            if exc.code < 500:
                return
        except (urllib.error.URLError, TimeoutError) as exc:
            log(f"Telegram nedostupný: {exc}")
        time.sleep(2 ** attempt)


def telegram_chat_id():
    """Vypíše chat ID z posledních zpráv, které bot dostal."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("Nastav nejdřív proměnnou TELEGRAM_BOT_TOKEN.")
    data = get_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        sys.exit("Bot zatím nedostal žádnou zprávu. Napiš mu v Telegramu cokoli a spusť to znovu.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


# ---------------------------------------------------------------- stav

def load_state(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def save_state(path, current, shows):
    data = {
        sid: {"seats": sorted(cur["seats"]), "deals": sorted(cur["deals"])}
        for sid, cur in current.items()
        if sid in shows
    }
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------- hlavní smyčka

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default="state/seats.json", help="soubor se stavem sedadel")
    ap.add_argument("--interval", type=float, default=15, help="pauza mezi kontrolami v sekundách")
    ap.add_argument("--duration", type=float, default=0,
                    help="jak dlouho smyčka poběží (s); 0 = jen jedna kontrola, -1 = donekonečna")
    ap.add_argument("--shows-refresh", type=float, default=600,
                    help="jak často znovu načíst seznam představení (s)")
    ap.add_argument("--status", action="store_true", help="vypsat aktuální volná místa a skončit")
    ap.add_argument("--test-telegram", action="store_true", help="poslat zkušební zprávu a skončit")
    ap.add_argument("--chat-id", action="store_true", help="zjistit TELEGRAM_CHAT_ID a skončit")
    args = ap.parse_args()

    if args.chat_id:
        telegram_chat_id()
        return
    if args.test_telegram:
        telegram("✅ Cimrman watchdog: Telegram funguje." + (f"\n<i>{html.escape(SOURCE_LABEL)}</i>" if SOURCE_LABEL else ""))
        return

    shows = fetch_shows()
    shows_at = time.monotonic()
    if args.status:
        print(format_status(shows, fetch_seats(shows)))
        return

    prev = load_state(args.state)
    if prev is None:
        log("Žádný předchozí stav – první kontrola jen zapíše, co je teď volné.")
        prev = {}
        first_run = True
    else:
        first_run = False

    deadline = None if args.duration < 0 else time.monotonic() + args.duration
    checks = errors = 0
    while True:
        try:
            if time.monotonic() - shows_at > args.shows_refresh:
                shows = fetch_shows()
                shows_at = time.monotonic()
            current = fetch_seats(shows)
            if not first_run:
                for alert in diff(prev, current, shows):
                    log(f"HLÁŠENÍ: {alert['show']['title']} {alert['show']['start']} "
                        f"uvolněno={alert['freed']} prodej={alert['opened']}")
                    url = buy_url(alert)
                    if OPEN_BROWSER and url and alert["freed"]:
                        webbrowser.open(url)
                    telegram(format_alert(alert), button_url=url)
            first_run = False
            prev = {sid: {"seats": list(c["seats"]), "deals": list(c["deals"])} for sid, c in current.items()}
            save_state(args.state, current, shows)
            checks += 1
            errors = 0
            free = sum(len(c["seats"]) for sid, c in current.items() if sid in shows)
            log(f"Kontrola {checks}: {len(shows)} představení, volných míst {free}")
        except Exception as exc:  # síť, změna API… – smyčka nesmí umřít
            errors += 1
            log(f"Chyba: {exc}")
            if errors == 10:
                telegram(f"⚠️ Cimrman watchdog: 10 chyb po sobě.\n<code>{html.escape(str(exc))[:500]}</code>")
        if deadline is not None and time.monotonic() + args.interval > deadline:
            break
        time.sleep(args.interval if errors == 0 else min(300, args.interval * 2 ** min(errors, 5)))
    if errors and checks == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
