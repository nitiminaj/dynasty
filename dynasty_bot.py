"""Dynasty-Bot für Sleeper mit Telegram-Nachrichten.

Zwei Modi:
  python dynasty_bot.py        Tagesbericht (nur um 8 und 20 Uhr Schweizer Zeit, sonst passiert nichts):
                               Ausfälle in deiner Aufstellung, neue Transaktionen, Waiver-Tipps
  python dynasty_bot.py now    Tagesbericht sofort, zum Testen
Außerdem: Schreibst du dem Bot in Telegram "update", bekommst du beim nächsten Lauf den vollen Bericht.
  python dynasty_bot.py fast   Schnellprüfung (für alle 15 Minuten): neue Trades/Pickups/Drops der Liga,
                               Eilmeldungen bei geändertem Verletzungsstatus deiner Spieler und
                               NFL-News (RSS) zu deinen Spielern, echten NFL-Trades und Verletzungen

Einrichtung: Lege im selben Ordner eine Textdatei config.json an mit diesem Inhalt:
  {"USER": "dein_sleeper_name", "TOKEN": "dein_telegram_token", "CHAT": "deine_chat_id"}
(optional zusätzlich "SEASON": 2026). So bleiben deine Daten erhalten, wenn du das Skript austauschst.
Beim ersten Lauf werden nur Stände gemerkt, es wird nichts gesendet.
"""
import json, os, re, sys, time, urllib.parse, urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from functools import lru_cache

# Deine Zugangsdaten stehen in der Datei config.json im selben Ordner (siehe unten), nicht hier im Skript.
_cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CFG = json.load(open(_cfg, encoding="utf-8")) if os.path.exists(_cfg) else {}
USER = CFG.get("USER", "DEIN_SLEEPER_NAME")
SEASON = int(CFG.get("SEASON", 2026))
TOKEN = CFG.get("TOKEN", "TELEGRAM_BOT_TOKEN")
CHAT = str(CFG.get("CHAT", "TELEGRAM_CHAT_ID"))

API = "https://api.sleeper.app/v1"
BASE = os.path.dirname(os.path.abspath(__file__))
SEEN, PL, INJ, NEWS, LAST = (os.path.join(BASE, f) for f in ("seen.json", "players.json", "injuries.json", "news.json", "last_daily.json"))
OFFSET = os.path.join(BASE, "tg_offset.json")
REPORT_HOURS = (8, 20)  # Tagesbericht nur um 8 und 20 Uhr Schweizer Zeit
# News-Feeds. Bei "Feed nicht lesbar" im Protokoll die Adresse anpassen oder die Zeile entfernen.
# (ESPN liefert vom GitHub-Server aus eine leere Antwort und ist deshalb nicht mehr dabei.)
FEEDS = ["https://profootballtalk.nbcsports.com/feed/",
         "https://www.cbssports.com/rss/headlines/nfl/",
         "https://www.rotowire.com/rss/news.php?sport=NFL"]
KEYS = re.compile(r"\b(trade[sd]?|trading|acquire[sd]?|released|waived|injured reserve|season-ending|"
                  r"out for the season|torn|acl|suspended|suspension)\b", re.I)
OUT = {"Out", "IR", "PUP", "Sus", "NA", "COV", "Doubtful"}
FAST = len(sys.argv) > 1 and sys.argv[1] == "fast"


def zurich_now():
    """Schweizer Zeit. Fallback ohne Zeitzonen-Datenbank (z. B. Windows): Sommerzeit von Hand berechnet."""
    try:
        return datetime.now(ZoneInfo("Europe/Zurich"))
    except Exception:
        u = datetime.now(timezone.utc).replace(tzinfo=None)

        def last_sunday(month):
            d = datetime(u.year, month, 31, 1)
            while d.weekday() != 6:
                d -= timedelta(days=1)
            return d

        return u + timedelta(hours=2 if last_sunday(3) <= u < last_sunday(10) else 1)


def asked_for_update():
    """True, wenn du dem Bot in Telegram "update" geschrieben hast (nur Nachrichten aus deinem eigenen Chat)."""
    off = load(OFFSET, None)
    url = "https://api.telegram.org/bot%s/getUpdates?timeout=0" % TOKEN + ("&offset=%d" % off if off else "")
    try:
        res = json.load(urllib.request.urlopen(url, timeout=30)).get("result", [])
    except Exception as e:
        print("Telegram nicht lesbar:", e)
        return False
    if res or off is None:
        json.dump(res[-1]["update_id"] + 1 if res else 0, open(OFFSET, "w"))
    if off is None:  # erster Lauf: alte Nachrichten ignorieren
        return False
    for u in res:
        m = u.get("message") or {}
        txt = (m.get("text") or "").strip().lower().lstrip("/").split("@")[0]
        if str((m.get("chat") or {}).get("id")) == CHAT and txt in ("update", "status"):
            return True
    return False


def get(path):
    return json.load(urllib.request.urlopen(API + path, timeout=60))


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
    urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TOKEN, data, timeout=30)


def load(path, default):
    return json.load(open(path)) if os.path.exists(path) else default


def players():
    """Spielerdaten lokal zwischenspeichern: Sleeper bittet darum, die große Liste selten abzurufen."""
    hours = 3 if FAST else 20
    if os.path.exists(PL) and time.time() - os.path.getmtime(PL) < hours * 3600:
        return json.load(open(PL))
    keep = ("full_name", "position", "team", "injury_status", "search_rank")
    data = {i: {k: p.get(k) for k in keep} for i, p in get("/players/nfl").items()}
    json.dump(data, open(PL, "w"))
    return data


@lru_cache(maxsize=None)
def stats(week):
    return get("/stats/nfl/regular/%d/%d" % (SEASON, week))


def feed_items(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    root = ET.fromstring(urllib.request.urlopen(req, timeout=30).read())
    for e in root.iter():
        if e.tag.split("}")[-1] in ("item", "entry"):
            kid = lambda n: next((c for c in e if c.tag.split("}")[-1] == n), None)
            t, l, d = kid("title"), kid("link"), kid("description")
            if d is None:
                d = kid("summary")
            yield ((t.text or "").strip() if t is not None else "",
                   ((l.text or l.get("href") or "").strip()) if l is not None else "",
                   (d.text or "") if d is not None else "")


def news(mynames, P):
    """Neue Schlagzeilen: zu deinen Spielern oder echte NFL-Trades, Entlassungen, Verletzungen relevanter Spieler."""
    seen = load(NEWS, None)
    first = seen is None
    seen = seen or []
    hot = {p["full_name"].lower() for p in P.values() if p.get("full_name")
           and (p.get("search_rank") or 99999) <= 300 and p.get("position") in ("QB", "RB", "WR", "TE")}
    mine = {n.lower() for n in mynames if n}
    out = []
    for url in FEEDS:
        try:
            items = list(feed_items(url))
        except Exception as e:
            print("Feed nicht lesbar:", url, e)
            continue
        for title, link, desc in items:
            key = title.lower()
            if not title or key in seen:
                continue
            seen.append(key)
            if first:
                continue
            text = (title + " " + re.sub("<[^>]+>", " ", desc)).lower()
            if any(n in text for n in mine):
                out.append("NEWS zu deinem Spieler: %s\n%s" % (title, link))
            elif KEYS.search(title) and any(n in text for n in hot):
                out.append("NFL-NEWS: %s\n%s" % (title, link))
    json.dump(seen[-800:], open(NEWS, "w"))
    return out


def opps(weeks, pid):
    games = [r for r in ((stats(w).get(pid) or {}) for w in weeks) if r.get("off_snp")]
    if not games:
        return 0.0
    return sum(r.get("rec_tgt", 0) + r.get("rush_att", 0) for r in games) / len(games)


def main():
    if USER.startswith("DEIN_") or TOKEN.startswith("TELEGRAM_"):
        sys.exit("Bitte config.json anlegen (siehe Anleitung oben in dynasty_bot.py).")
    global FAST
    asked = asked_for_update()
    if asked:
        FAST = False  # auf Anfrage immer der volle Bericht
    slot = None
    if not FAST and "now" not in sys.argv and not asked:  # Tagesbericht nur zu den Berichtszeiten und nur einmal pro Zeitfenster
        z = zurich_now()
        slot = z.strftime("%Y-%m-%d-%H")
        if z.hour not in REPORT_HOURS or load(LAST, "") == slot:
            print("Kein Berichtszeitpunkt (nur %s Uhr, einmal pro Zeitfenster)." % " und ".join(map(str, REPORT_HOURS)))
            return
    week = get("/state/nfl")["week"]
    uid = get("/user/" + USER)["user_id"]
    P = players()
    name = lambda i: (P.get(i) or {}).get("full_name") or i
    first = not os.path.exists(SEEN)
    seen = load(SEEN, [])
    inj_old = load(INJ, None)
    inj_new, role, msgs, mynames = {}, {}, [], set()
    weeks = range(max(1, week - 1), week + 2) if FAST else range(1, max(week, 1) + 1)

    for lg in get("/user/%s/leagues/nfl/%d" % (uid, SEASON)):
        lid, title = lg["league_id"], lg["name"]
        rosters = get("/league/%s/rosters" % lid)
        users = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u["display_name"]
                 for u in get("/league/%s/users" % lid)}
        rn = {r["roster_id"]: users.get(r["owner_id"], "?") for r in rosters}
        me = next((r for r in rosters if r["owner_id"] == uid), None)
        if not me:
            continue
        starters = me.get("starters") or []

        for i in me.get("players") or []:
            inj_new[i] = (P.get(i) or {}).get("injury_status") or "gesund"
            mynames.add(name(i))
            if i in starters:
                role[i] = title

        if not FAST:  # Erinnerung an Ausfälle in der Startaufstellung
            for i in starters:
                st = (P.get(i) or {}).get("injury_status")
                if st in OUT or i in (me.get("reserve") or []):
                    msgs.append("%s: %s fällt aus (%s). Startaufstellung ändern." % (title, name(i), st or "IR-Slot"))
                elif st == "Questionable":
                    msgs.append("%s: %s ist fraglich. Vor dem Spiel prüfen." % (title, name(i)))

        for w in weeks:  # neue Transaktionen
            for t in get("/league/%s/transactions/%d" % (lid, w)):
                tid = t.get("transaction_id")
                if t.get("status") != "complete" or tid in seen:
                    continue
                seen.append(tid)
                if first:
                    continue
                who = " / ".join(rn.get(r, "?") for r in t.get("roster_ids", []))
                add = ", ".join(name(i) for i in (t.get("adds") or {})) or "-"
                drop = ", ".join(name(i) for i in (t.get("drops") or {})) or "-"
                msgs.append("%s [%s] %s | +%s | -%s" % (title, t.get("type"), who, add, drop))

        if not FAST and week >= 6:  # Waiver-Aufsteiger
            taken = {i for r in rosters for i in (r.get("players") or [])}
            recent, older = [week - 2, week - 1], [week - 5, week - 4, week - 3]
            found = []
            for pid in stats(week - 1):
                p = P.get(pid) or {}
                if pid in taken or p.get("position") not in ("RB", "WR", "TE"):
                    continue
                now, before = opps(recent, pid), opps(older, pid)
                if now >= 6 and now - before >= 3:
                    found.append((now - before, p, now, before))
            for _, p, now, before in sorted(found, key=lambda x: -x[0])[:5]:
                msgs.append("%s: Waiver-Tipp %s (%s, %s): Chancen pro Spiel %.1f -> %.1f" % (
                    title, p.get("full_name"), p.get("position"), p.get("team") or "FA", before, now))

    if inj_old is not None:  # Eilmeldung bei geändertem Verletzungsstatus
        for i, st in inj_new.items():
            old = inj_old.get(i)
            if old is not None and old != st:
                extra = " (Starter in %s)" % role[i] if i in role else ""
                msgs.insert(0, "EILMELDUNG: %s%s: %s -> %s" % (name(i), extra, old, st))

    msgs += news(mynames, P)
    json.dump(inj_new, open(INJ, "w"))
    json.dump(seen, open(SEEN, "w"))
    if asked and not msgs:
        msgs.append("Update: keine Ausfälle, keine neuen Transaktionen, keine Waiver-Aufsteiger.")
    if msgs:
        send("\n\n".join(msgs))
    if slot:
        json.dump(slot, open(LAST, "w"))
    print("%d Nachrichten gesendet" % len(msgs))


if __name__ == "__main__":
    main()
