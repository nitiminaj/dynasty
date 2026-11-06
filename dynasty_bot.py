"""Dynasty-Bot: schickt dir per Telegram
  1. Ausfälle in deiner Startaufstellung (Out, Doubtful, IR, fraglich)
  2. neue Trades, Pickups und Drops in deiner Liga
  3. Waiver-Tipps: Free Agents, deren Rolle gerade wächst

Einrichtung (einmalig):
  - Telegram öffnen, @BotFather anschreiben, /newbot -> Token kopieren
  - deinem neuen Bot eine beliebige Nachricht schicken
  - im Browser https://api.telegram.org/bot<TOKEN>/getUpdates öffnen, dort steht "chat":{"id":...}
  - unten USER, TOKEN und CHAT eintragen
Start:  python dynasty_bot.py
Täglich automatisch: Windows-Aufgabenplanung oder cron (z. B. jeden Morgen um 8 Uhr).
Beim ersten Lauf werden alte Transaktionen nur gemerkt, nicht gesendet.
"""
import json, os, urllib.parse, urllib.request
from functools import lru_cache

USER = "NKandiah"
SEASON = 2026
TOKEN = "9194880360:AAFWvIITX_nYyk_Uln2svut8eUWeQHYqBhM"
CHAT = "9035160094"

API = "https://api.sleeper.app/v1"
SEEN = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
OUT = {"Out", "IR", "PUP", "Sus", "NA", "COV", "Doubtful"}


def get(path):
    return json.load(urllib.request.urlopen(API + path, timeout=60))


def send(text):
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text[:4000]}).encode()
    urllib.request.urlopen("https://api.telegram.org/bot%s/sendMessage" % TOKEN, data, timeout=30)


@lru_cache(maxsize=None)
def stats(week):
    return get("/stats/nfl/regular/%d/%d" % (SEASON, week))


def opps(weeks, pid):
    rows = [(stats(w).get(pid) or {}) for w in weeks]
    games = [r for r in rows if r.get("off_snp")]
    if not games:
        return 0.0
    return sum(r.get("rec_tgt", 0) + r.get("rush_att", 0) for r in games) / len(games)


def main():
    week = get("/state/nfl")["week"]
    uid = get("/user/" + USER)["user_id"]
    players = get("/players/nfl")
    name = lambda i: (players.get(i) or {}).get("full_name") or i
    first = not os.path.exists(SEEN)
    seen = [] if first else json.load(open(SEEN))
    msgs = []

    for lg in get("/user/%s/leagues/nfl/%d" % (uid, SEASON)):
        lid, title = lg["league_id"], lg["name"]
        rosters = get("/league/%s/rosters" % lid)
        users = {u["user_id"]: (u.get("metadata") or {}).get("team_name") or u["display_name"]
                 for u in get("/league/%s/users" % lid)}
        rn = {r["roster_id"]: users.get(r["owner_id"], "?") for r in rosters}
        me = next((r for r in rosters if r["owner_id"] == uid), None)
        if not me:
            continue

        # 1. Ausfälle in der Startaufstellung
        for i in me.get("starters") or []:
            st = (players.get(i) or {}).get("injury_status")
            if st in OUT or i in (me.get("reserve") or []):
                msgs.append("%s: %s fällt aus (%s). Startaufstellung ändern." % (title, name(i), st or "IR-Slot"))
            elif st == "Questionable":
                msgs.append("%s: %s ist fraglich. Vor dem Spiel prüfen." % (title, name(i)))

        # 2. Neue Transaktionen in der Liga
        for w in range(1, max(week, 1) + 1):
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

        # 3. Waiver-Aufsteiger (letzte 2 Wochen gegen die 3 davor)
        if week >= 6:
            taken = {i for r in rosters for i in (r.get("players") or [])}
            recent, older = [week - 2, week - 1], [week - 5, week - 4, week - 3]
            found = []
            for pid in stats(week - 1):
                p = players.get(pid) or {}
                if pid in taken or p.get("position") not in ("RB", "WR", "TE"):
                    continue
                now, before = opps(recent, pid), opps(older, pid)
                if now >= 6 and now - before >= 3:
                    found.append((now - before, p, now, before))
            for _, p, now, before in sorted(found, key=lambda x: -x[0])[:5]:
                msgs.append("%s: Waiver-Tipp %s (%s, %s): Chancen pro Spiel %.1f -> %.1f" % (
                    title, p.get("full_name"), p.get("position"), p.get("team") or "FA", before, now))

    json.dump(seen, open(SEEN, "w"))
    if msgs:
        send("\n\n".join(msgs))
    print("%d Nachrichten gesendet" % len(msgs))


if __name__ == "__main__":
    main()
