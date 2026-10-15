#!/usr/bin/env python3
"""
edt_discord.py — Envoi quotidien des emplois du temps M1 Info S7 (UBO) vers Discord.

- Récupère les calendriers publics iCal (ADE / UBO) listés dans config.json
- Extrait les événements du jour (fuseau Europe/Paris)
- Déduplique les événements présents dans plusieurs calendriers
- Fusionne les créneaux consécutifs identiques (même cours, même salle, écart <= 30 min)
- Met en forme un embed Discord compact, lisible sur téléphone
- Envoie un embed par salon via le webhook correspondant

Variables d'environnement :
  EDT_DATE    : date cible au format YYYY-MM-DD (défaut : aujourd'hui à Paris)
  EDT_DRY_RUN : si "1"/"true", simulation sans aucun envoi vers Discord
  WEBHOOK_*   : URLs des webhooks Discord, une par salon (cf. config.json)

Code de sortie : 0 si tout est OK, 1 si au moins un envoi a échoué.
100 % bibliothèque standard Python — aucune dépendance à installer.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

# --------------------------------------------------------------------------
# Constantes
# --------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
TZ = ZoneInfo("Europe/Paris")
UTC = ZoneInfo("UTC")

HTTP_ATTEMPTS = 3          # tentatives de récupération / d'envoi
HTTP_TIMEOUT = 30          # secondes
MERGE_GAP_MAX = 30 * 60    # fusion des créneaux si l'écart est <= 30 minutes
EMBED_MAX_DESC = 4096      # limite Discord

PVP_GROUPS = {"PVP1", "PVP2", "PVP3"}
TIIL_GROUP = "TIILA"

JOURS_FR = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS_FR = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
           "août", "septembre", "octobre", "novembre", "décembre"]

# Ligne "NOM Prénom" : nom en MAJUSCULES (accentuées), prénom en Casse Titre.
# Exemple valide : "THETIOT Franck"
# Exemples rejetés : "Enseignant non encore défini", "LP CREB", "L3 RT B", "Amphi A"
PROF_RE = re.compile(
    r"^[A-ZÉÈÊÀÂÎÔÛÜÇ][A-ZÉÈÊÀÂÎÔÛÜÇ'\-]+"
    r"(?:\s+[A-ZÉÈÊÀÂÎÔÛÜÇ][A-ZÉÈÊÀÂÎÔÛÜÇ'\-]+)*"
    r"\s+[A-ZÉÈÊÀÂÎÔÛÜÇ][a-zéèêàâîôûüçñ\-]+"
    r"(?:\s+[A-ZÉÈÊÀÂÎÔÛÜÇ][a-zéèêàâîôûüçñ\-]+)*$"
)

# --------------------------------------------------------------------------
# Parsing iCal
# --------------------------------------------------------------------------

def unescape_ics(text: str) -> str:
    """Retire l'échappement iCal : \\n (retour ligne), \\, \\; et \\\\."""
    return re.sub(
        r"\\(.)",
        lambda m: {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}.get(m.group(1), m.group(1)),
        text,
    )


def parse_ics_dt(value: str, params: str):
    """Convertit une valeur DTSTART/DTEND iCal en datetime conscient du fuseau.

    Formats gérés : 20261016T101500Z (UTC), 20261016T101500 (naif -> Paris),
    20261016 (journée entière -> 00:00 Paris, marquée all_day).
    """
    value = value.strip()
    if not value:
        return None, True
    if re.fullmatch(r"\d{8}", value):                      # journée entière
        return datetime.strptime(value, "%Y%m%d").replace(tzinfo=TZ), True
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), False
    naive = None
    for fmt in ("%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            naive = datetime.strptime(value, fmt)
            break
        except ValueError:
            continue
    if naive is None:
        return None, True
    tzid = None
    m = re.search(r"TZID=([^;:]+)", params or "")
    if m:
        try:
            tzid = ZoneInfo(m.group(1).strip('"'))
        except Exception:
            tzid = None
    return naive.replace(tzinfo=tzid or TZ), False


def parse_ics(text: str, calendar_name: str):
    """Extrait les VEVENT d'un flux iCal en dictionnaires Python."""
    text = re.sub(r"\r?\n[ \t]", "", text)  # dépliage des lignes longues
    events = []
    for block in re.findall(r"BEGIN:VEVENT.*?END:VEVENT", text, re.S):
        fields = {}
        for line in block.splitlines():
            if ":" not in line:
                continue
            head, value = line.split(":", 1)
            name, _, params = head.partition(";")
            fields[name.strip().upper()] = (params, value)
        if "DTSTART" not in fields:
            continue
        start, all_day = parse_ics_dt(fields["DTSTART"][1], fields["DTSTART"][0])
        if start is None:
            continue
        end, _ = parse_ics_dt(
            fields.get("DTEND", fields["DTSTART"])[1],
            fields.get("DTEND", fields["DTSTART"])[0],
        )
        if end is None:
            end = start
        title = unescape_ics(fields.get("SUMMARY", ("", ""))[1]).strip()
        if not title:
            title = "(sans titre)"
        room = unescape_ics(fields.get("LOCATION", ("", ""))[1]).strip()
        if "," in room:  # plusieurs salles dans un seul champ LOCATION
            room = ", ".join(p.strip() for p in room.split(",") if p.strip())
        desc = unescape_ics(fields.get("DESCRIPTION", ("", ""))[1])
        events.append({
            "title": title,
            "start": start,
            "end": end,
            "room": room,
            "prof": extract_prof(desc),
            "all_day": all_day,
            "groups": {calendar_name},
        })
    return events


def extract_prof(description: str):
    """Cherche une ligne « NOM Prénom » dans la description iCal."""
    profs = []
    for line in description.splitlines():
        line = line.strip()
        if not line or len(line) > 60:
            continue
        low = line.lower()
        if "enseignant" in low or "exporté" in low or "exporte" in low:
            continue
        if PROF_RE.match(line) and line not in profs:
            profs.append(line)
    return " & ".join(profs[:2])

# --------------------------------------------------------------------------
# Récupération HTTP (avec tentatives)
# --------------------------------------------------------------------------

def http_get(url: str):
    last_error = None
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "ubo-edt-discord/1.0"})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return response.read().decode("utf-8", errors="replace"), None
        except Exception as exc:
            last_error = exc
            if attempt < HTTP_ATTEMPTS:
                time.sleep(2 * attempt)
    return None, str(last_error)


def send_webhook(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    last_error = "erreur inconnue"
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, data=data, method="POST", headers={
                "Content-Type": "application/json",
                "User-Agent": "ubo-edt-discord/1.0",
            })
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as response:
                return True, response.status
        except urllib.error.HTTPError as exc:
            if exc.code == 429:  # rate limit Discord
                try:
                    retry_after = float(exc.headers.get("Retry-After", "2"))
                except (TypeError, ValueError):
                    retry_after = 2.0
                time.sleep(min(retry_after, 15))
                last_error = "HTTP 429 (rate limit)"
                continue
            last_error = "HTTP %s" % exc.code
        except Exception as exc:
            last_error = str(exc)
        if attempt < HTTP_ATTEMPTS:
            time.sleep(2 * attempt)
    return False, last_error

# --------------------------------------------------------------------------
# Déduplication et fusion
# --------------------------------------------------------------------------

def event_key(event: dict):
    return (event["title"].strip().lower(), event["start"], event["end"], event["room"])


def collect_events(events_per_calendar: dict):
    """Fusionne les mêmes événements trouvés dans plusieurs calendriers."""
    merged = {}
    for calendar_name, events in events_per_calendar.items():
        for event in events:
            key = event_key(event)
            if key in merged:
                merged[key]["groups"].add(calendar_name)
            else:
                event["groups"] = {calendar_name}
                merged[key] = event
    return list(merged.values())


def merge_slots(events):
    """Fusionne les créneaux consécutifs (même titre + même salle, écart <= 30 min)."""
    ordered = sorted(events, key=lambda e: e["start"])
    merged = []
    for event in ordered:
        if merged:
            previous = merged[-1]
            gap = (event["start"] - previous["end"]).total_seconds()
            if (event["title"] == previous["title"]
                    and event["room"] == previous["room"]
                    and 0 <= gap <= MERGE_GAP_MAX):
                previous["end"] = max(previous["end"], event["end"])
                previous["parts"] += 1
                continue
        item = dict(event)
        item["parts"] = event.get("parts", 1)
        merged.append(item)
    return merged

# --------------------------------------------------------------------------
# Mise en forme Discord
# --------------------------------------------------------------------------

def pvp_label(event: dict) -> str:
    """Étiquette d'un événement PVP : « Tous PVP » ou la liste des groupes."""
    pvp = sorted(event["groups"] & PVP_GROUPS)
    if len(pvp) == len(PVP_GROUPS):
        label = "Tous PVP"
    else:
        label = " + ".join(pvp)
    if TIIL_GROUP in event["groups"]:
        label = "TIIL-A + " + label
    return label


def format_event(event: dict, label=None):
    start = event["start"].astimezone(TZ)
    end = event["end"].astimezone(TZ)
    if event.get("all_day"):
        hours = "**Journée**"
    else:
        hours = "**%s – %s**" % (start.strftime("%H:%M"), end.strftime("%H:%M"))
    line = "%s — %s" % (hours, event["title"])
    notes = []
    if label:
        notes.append(label)
    if event.get("parts", 1) >= 2:
        notes.append("fusion de %d créneaux" % event["parts"])
    if notes:
        line += " *(%s)*" % ", ".join(notes)
    details = []
    if event["room"]:
        details.append("📍 " + event["room"])
    if event["prof"]:
        details.append("👤 " + event["prof"])
    if details:
        line += "\n> " + " • ".join(details)
    return line


def build_description(channel: dict, events: list, main_names: set):
    """Construit la description de l'embed d'un salon."""
    main = [e for e in events if e["groups"] & set(channel["main"])]
    combined_names = set(channel["combined"])
    main_keys = {event_key(e) for e in main}
    combined = [e for e in events
                if e["groups"] & combined_names
                and event_key(e) not in main_keys]

    common = [e for e in combined
              if len(e["groups"] & combined_names) >= 2 and e["groups"] & main_names]
    tiil = [e for e in combined
            if not (len(e["groups"] & combined_names) >= 2 and e["groups"] & main_names)
            and len(e["groups"] & combined_names) == 1 and TIIL_GROUP in e["groups"]]
    pvp = [e for e in combined if e not in common and e not in tiil]

    sections = []
    if main:
        sections.append(("📘 Cours " + " + ".join(channel["main"]),
                         merge_slots(main), None))
    if common:
        sections.append(("📢 Événements communs", merge_slots(common), None))
    if tiil:
        sections.append(("🚀 Projet — TIIL-A", merge_slots(tiil), None))
    if pvp:
        sections.append(("🤝 Projet PVP", merge_slots(pvp), pvp_label))

    parts = []
    for title, section_events, label_fn in sections:
        parts.append("**%s**" % title)
        for event in section_events:
            parts.append(format_event(event, label_fn(event) if label_fn else None))
        parts.append("")
    description = "\n".join(parts).strip()
    if not description:
        description = "Aucun cours aujourd'hui 🎉"
    if len(description) > EMBED_MAX_DESC:
        description = description[:EMBED_MAX_DESC - 1] + "…"
    return description


def build_payload(channel: dict, target: date, events: list,
                   main_names: set, warnings: list):
    human_date = "%s %d %s" % (JOURS_FR[target.weekday()], target.day,
                               MOIS_FR[target.month - 1])
    description = build_description(channel, events, main_names)
    for warning in warnings:
        description += "\n\n⚠️ %s" % warning
    if len(description) > EMBED_MAX_DESC:
        description = description[:EMBED_MAX_DESC - 1] + "…"
    group_label = " + ".join(channel["main"])
    return {
        "username": "Chronos",
        "allowed_mentions": {"parse": []},
        "embeds": [{
            "title": "📅 %s — %s" % (group_label, human_date),
            "description": description,
            "color": int(channel["color"], 16),
            "footer": {"text": "UBO • M1 Info S7 — envoi automatique quotidien"},
            "timestamp": datetime.now(UTC).isoformat(),
        }],
    }

# --------------------------------------------------------------------------
# Programme principal
# --------------------------------------------------------------------------

def main():
    with open(CONFIG_PATH, encoding="utf-8") as handle:
        config = json.load(handle)

    dry_run = os.environ.get("EDT_DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
    date_str = os.environ.get("EDT_DATE", "").strip()
    if date_str:
        try:
            target = date.fromisoformat(date_str)
        except ValueError:
            print("✗ EDT_DATE invalide (attendu : YYYY-MM-DD) : %r" % date_str)
            return 1
    else:
        target = datetime.now(TZ).date()

    print("→ Date cible : %s (fuseau Europe/Paris)%s"
          % (target.isoformat(), " — SIMULATION" if dry_run else ""))

    # Récupération des calendriers
    events_per_calendar = {}
    unavailable = []
    for name, url in config["calendars"].items():
        text, error = http_get(url)
        if error:
            print("  ✗ %s : %s" % (name, error))
            unavailable.append(name)
            continue
        day_events = [e for e in parse_ics(text, name)
                      if e["start"].astimezone(TZ).date() == target]
        print("  ✓ %s : %d événement(s) aujourd'hui" % (name, len(day_events)))
        events_per_calendar[name] = day_events

    events = collect_events(events_per_calendar)
    main_names = {n for ch in config["channels"] for n in ch["main"]}
    warnings = ["Calendrier %s indisponible — sections concernées incomplètes." % n
                for n in unavailable]
    if warnings:
        print("  ⚠ %d calendrier(s) indisponible(s) : %s" % (len(unavailable), ", ".join(unavailable)))

    # Envoi (ou simulation) salon par salon
    failures = 0
    for channel in config["channels"]:
        payload = build_payload(channel, target, events, main_names, warnings)
        print()
        print("=== Salon #%s ===" % channel["name"])
        if dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            continue
        webhook_url = os.environ.get(channel["webhook_env"], "").strip()
        if not webhook_url:
            print("✗ Webhook manquant : définissez le secret %s" % channel["webhook_env"])
            failures += 1
            continue
        ok, info = send_webhook(webhook_url, payload)
        if ok:
            print("✓ Envoyé (%s)" % info)
        else:
            print("✗ Échec de l'envoi : %s" % info)
            failures += 1

    print()
    if dry_run:
        print("✓ Simulation terminée — rien n'a été envoyé à Discord.")
        return 0
    if failures:
        print("✗ %d salon(s) en échec." % failures)
        return 1
    print("✓ Tous les salons ont été mis à jour.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
