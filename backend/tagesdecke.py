"""Die Tagesdecke: der wache Tag, lückenlos benannt.

Warum es das gibt
-----------------
Der Planer dieses Dienstes hat bis 2026-09-14 nur Löcher gefüllt. Er kannte
"freie Slots zwischen Terminen" und legte Aufgaben hinein, solange welche im Pool
lagen. Was danach übrig blieb, hatte keinen Namen. Damit ließ sich die Frage "was
mache ich heute wann und wie lange" nicht beantworten, denn der größere Teil des
Tages kam in der Planung schlicht nicht vor.

Die Decke dreht das um. Sie geht vom wachen Fenster aus (Aufstehen bis
Schlafengehen) und sorgt dafür, dass **jede Minute darin einen Namen trägt**.
Erholung ist dabei kein Rest, sondern ein geplanter Block wie jeder andere: wer
Erholung nicht plant, plant sie weg.

Zwei Wahrheiten über den Tagesrand, jetzt eine
---------------------------------------------
`scheduler.DAY_PLAN_WINDOW_START/END` stand fest auf 08:00 bis 22:00, während
`sleep_schedule.SLEEP_SCHEDULE` für einen Arbeitstag 05:30 als Aufstehzeit führte.
Die zweieinhalb Stunden davor waren für die Planung unsichtbar, obwohl sie der
Wecker in Home Assistant längst kannte. Diese Datei nimmt `sleep_schedule` als
Quelle und reicht das Fenster an den Planer durch.

Was hier NICHT passiert
-----------------------
Die Decke schreibt nicht in `events`. An dieser Tabelle hängen die vier
Tagestyp-Kalender und damit `/api/day-type/today`, das die 05:35-Weckkette
auslöst. Eine Schicht, die täglich ein Dutzend Blöcke erzeugt, hat dort nichts
verloren. Sichtbar wird sie über das Kalender-Raster und einen eigenen iCal-Feed.

Arbeitsteilung mit den Nachbar-Apps
-----------------------------------
Der Kalender entscheidet **wann** Zeit für etwas ist, nie **was** genau. Für einen
Trainingsblock heißt das: die Decke reserviert die Zeit und begründet die Lage,
den Inhalt holt die Fitness-App zum Zeitpunkt selbst. Beide Seiten können sich ändern, ohne
die andere anzufassen. Owner-Entscheid 2026-09-14.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from backend.models import DailyGoal, Event, HabitSession, TimeBlock, Todo, ZeitIst, utcnow
from backend.recurrence import expand_events
from backend.routers.daytype import _get_daytype_display
from backend.sleep_schedule import BUFFER_MINUTES, get_schedule
from backend.system_calendars import (
    DAYTYPE_CALENDARS,
    FEIERTAG_CALENDAR_ID,
    GEBURTSTAGE_CALENDAR_ID,
)
from backend.wanduhr import als_wanduhr, tagesgrenzen

# Das Vokabular der Decke. Es gilt für Soll (TimeBlock.art) und Ist (ZeitIst.art),
# damit ein Soll-Ist-Vergleich ohne Übersetzungstabelle auskommt.
ARTEN = {
    "fix": "Termin",              # aus events, fremdbestimmt (Arbeit, Schule, Verabredung)
    "gewohnheit": "Gewohnheit",   # HabitSession
    "ziel": "Tagesziel",          # DailyGoal mit Zeitblock
    "aufgabe": "Aufgabe",         # Todo mit Zeitfenster
    "training": "Training",       # reservierte Zeit; Inhalt kommt von der Fitness-App
    "erholung": "Erholung",       # bewusst geplant, nicht der Rest
    "grundlast": "Grundlast",     # Essen, Körperpflege, Haushalt, Wege
    "puffer": "Puffer",           # bewusst offen gelassen
    "schlaf": "Schlaf",           # nur im Ist relevant (Uhr), nie geplant
}

# Arten, die aus einer anderen Tabelle gespiegelt werden. Sie werden bei jedem
# Aufruf frisch gelesen und erst beim Festschreiben zu eigenen Zeilen. Die
# Oberfläche braucht die Unterscheidung: an einem offenen Tag hat das Verschieben
# eines gespiegelten Blocks keinen Bestand, weil er beim nächsten Aufruf wieder
# aus seiner Quelle kommt. Dort gehört der Termin selbst verschoben.
GESPIEGELTE_ARTEN = {"fix", "gewohnheit", "ziel", "aufgabe"}

# Grundlast: die Zeit, die jeden Tag anfällt und in keiner Aufgabenliste steht.
# Ohne sie plant der Tag sich selbst voll und die erste Mahlzeit verdrängt den
# ersten Termin. Werte sind Vorgaben für einen Alltag, keine Messung.
MORGENROUTINE_MINUTEN = 40
MITTAGESSEN_MINUTEN = 40
ABENDESSEN_MINUTEN = 50
# Zeitfenster, in denen eine Mahlzeit gesucht wird, und die Zeit darin, die
# bevorzugt wird. ★ Beides wird gebraucht: die erste Fassung kannte nur das
# Fenster, prüfte „schneidet die Lücke das Mittagsfenster" und legte den Block
# dann an den Anfang der Lücke. An einem freien Tag mit einer einzigen großen
# Lücke ab 08:00 ergab das Mittagessen um 08:40 und Abendessen um 09:20, beide
# formal im Recht und offensichtlich falsch.
MITTAG_FENSTER = (time(11, 30), time(14, 30))
MITTAG_WUNSCH = time(12, 30)
ABEND_FENSTER = (time(17, 30), time(20, 30))
ABEND_WUNSCH = time(18, 30)

# Tagesabschnitte für die Benennung freier Zeit. Ohne sie entsteht auf einem
# freien Tag ein einziger Block „Freie Zeit" über elf Stunden, und ein Block über
# elf Stunden ist keine Planung, sondern ein Loch mit Namen.
ABSCHNITTE = (
    (time(0, 0), "Vormittag"),
    (time(12, 0), "Nachmittag"),
    (time(17, 0), "Abend"),
)

# Kürzer als das hier wird nicht benannt, sondern Puffer. Ein Block von sieben
# Minuten ist keine Erholung, sondern ein Übergang, und ihn zu betiteln macht die
# Ansicht unlesbar, ohne etwas zu planen.
MIN_BLOCK_MINUTEN = 20
# Nach einem langen Pflichtblock (Arbeit, Schule) zuerst ankommen, bevor die
# Planung wieder etwas verlangt.
LANGER_BLOCK_STUNDEN = 6
ANKOMMEN_MINUTEN = 30
# Training: darunter lohnt der Weg zur Matte nicht, darüber wird es ein Termin.
TRAINING_MINUTEN = 60

_DAYTYPE_CALENDAR_IDS = set(DAYTYPE_CALENDARS.values()) | {
    FEIERTAG_CALENDAR_ID,
    GEBURTSTAGE_CALENDAR_ID,
}
# Diese beiden Tagestyp-Kalender tragen echte Zeiten (Mo bis Do 7:00 bis 16:00 etc.)
# und sind damit die Struktur des Tages. Urlaub, Krank und Feiertag sind
# ganztägig und beschreiben den Tag, statt ihn zu belegen: als Block würden sie
# die Decke von 00:00 bis 24:00 zudecken und jede Planung verhindern.
_STRUKTUR_KALENDER = {DAYTYPE_CALENDARS["arbeit"], DAYTYPE_CALENDARS["schule"]}


def _minuten(von: datetime, bis: datetime) -> int:
    return int((bis - von).total_seconds() // 60)


def wach_fenster(db: Session, tag: date) -> tuple[datetime, datetime]:
    """Von wann bis wann ist dieser Tag wach? Berliner Wanduhr, zonenlos.

    Quelle ist `sleep_schedule` je Tagestyp. Das Fenster endet spätestens um
    Mitternacht: ein `TimeBlock` trägt ein `date`, und ein Block, der in den
    Folgetag läuft, würde dort als ganztägige Belegung erscheinen und die Decke
    des nächsten Tages blockieren. Die Krank-Vorgabe (Bett um 01:00) wird deshalb
    auf 24:00 gekappt.
    """
    daytype = _get_daytype_display(db, tag)
    plan = get_schedule(daytype)
    wach_h, wach_m = plan["wake"]
    bett_h, bett_m = plan["bed"]

    beginn = datetime(tag.year, tag.month, tag.day, wach_h, wach_m)
    bett_minuten = bett_h * 60 + bett_m
    if bett_minuten <= 12 * 60:
        # 00:00 heißt Mitternacht am Ende dieses Tages, 01:00 heißt danach.
        bett_minuten += 24 * 60
    bett_minuten = min(bett_minuten, 24 * 60)
    ende = datetime(tag.year, tag.month, tag.day) + timedelta(minutes=bett_minuten)
    if ende <= beginn:
        # Kann nur bei einer widersprüchlichen Vorgabe auftreten. Lieber ein
        # leeres Fenster als eine negative Spanne, die weiter unten durchschlägt.
        return beginn, beginn
    return beginn, ende


def ausklang_beginn(db: Session, tag: date) -> datetime:
    """Ab wann der Tag ausläuft (Pufferzone vor dem Bett, `sleep_schedule`)."""
    _, ende = wach_fenster(db, tag)
    return ende - timedelta(minutes=BUFFER_MINUTES)


def _block(
    start: datetime,
    ende: datetime,
    art: str,
    titel: str,
    *,
    begruendung: str | None = None,
    herkunft_typ: str | None = None,
    herkunft_id: str | None = None,
    status: str = "geplant",
    block_id: str | None = None,
) -> dict:
    return {
        "id": block_id,
        "start": start,
        "ende": ende,
        "minuten": _minuten(start, ende),
        "art": art,
        "titel": titel,
        "begruendung": begruendung,
        "status": status,
        "herkunft_typ": herkunft_typ,
        "herkunft_id": herkunft_id,
    }


def sammle_fixpunkte(db: Session, tag: date) -> list[dict]:
    """Alles, was an diesem Tag schon eine Zeit hat, aus seiner jeweiligen Quelle.

    Bewusst nicht aus `time_blocks`: solange ein Tag nicht festgeschrieben ist,
    ist die Quelle die Wahrheit. Ein verschobener Termin soll die Decke sofort
    verschieben, nicht erst bei der nächsten Neuplanung.
    """
    beginn, ende = tagesgrenzen(tag)
    punkte: list[dict] = []

    # Fenster grosszuegig ziehen: eine Reihe, die gestern begann, kann heute noch
    # laufen. Das Zuschneiden auf den Tag passiert unten.
    roh = (
        db.query(Event)
        .filter(Event.start < ende + timedelta(days=1), Event.end > beginn - timedelta(days=1))
        .all()
    )
    # Wiederholungsreihen zu echten Instanzen dieses Tages auffalten. Ohne das
    # fehlt jeder wöchentliche Termin in der Decke, obwohl er im Kalender steht.
    # ⚠️ `expand_events` liefert Instanz-Wörterbücher, keine Event-Objekte, und es
    # gibt fuer ein nicht-wiederkehrendes Event immer genau eine Instanz zurueck,
    # ohne zu pruefen, ob sie das Fenster beruehrt (siehe Docstring dort). Der
    # Zuschnitt unten ist deshalb Pflicht und nicht nur Kosmetik.
    for ereignis in expand_events(roh, beginn, ende):
        kalender_id = ereignis.get("calendar_id")
        if kalender_id in _DAYTYPE_CALENDAR_IDS and kalender_id not in _STRUKTUR_KALENDER:
            continue
        if ereignis.get("all_day"):
            # Ganztägiges beschreibt den Tag, es belegt ihn nicht.
            continue
        e_start = max(als_wanduhr(ereignis["start"]), beginn)
        e_ende = min(als_wanduhr(ereignis["end"]), ende)
        if e_ende <= e_start:
            continue
        punkte.append(
            _block(
                e_start, e_ende, "fix", ereignis["title"],
                herkunft_typ="event", herkunft_id=str(ereignis["id"]),
            )
        )

    sitzungen = (
        db.query(HabitSession)
        .filter(
            HabitSession.start < ende,
            HabitSession.end > beginn,
            HabitSession.status.notin_(["dismissed", "overridden"]),
        )
        .all()
    )
    for sitzung in sitzungen:
        titel = sitzung.habit.name if sitzung.habit else "Gewohnheit"
        punkte.append(
            _block(
                max(als_wanduhr(sitzung.start), beginn),
                min(als_wanduhr(sitzung.end), ende),
                "gewohnheit", titel,
                herkunft_typ="habit_session", herkunft_id=str(sitzung.id),
            )
        )

    ziele = (
        db.query(DailyGoal)
        .filter(
            DailyGoal.scheduled_start.isnot(None),
            DailyGoal.scheduled_end.isnot(None),
            DailyGoal.scheduled_start < ende,
            DailyGoal.scheduled_end > beginn,
            DailyGoal.status.in_(("planned", "active")),
        )
        .all()
    )
    for ziel in ziele:
        punkte.append(
            _block(
                max(als_wanduhr(ziel.scheduled_start), beginn),
                min(als_wanduhr(ziel.scheduled_end), ende),
                "ziel", ziel.title,
                herkunft_typ="goal", herkunft_id=str(ziel.id),
            )
        )

    aufgaben = (
        db.query(Todo)
        .filter(
            Todo.scheduled_start.isnot(None),
            Todo.scheduled_end.isnot(None),
            Todo.scheduled_start < ende,
            Todo.scheduled_end > beginn,
            Todo.status.notin_(["done", "cancelled"]),
            Todo.completed == False,  # noqa: E712
        )
        .all()
    )
    for aufgabe in aufgaben:
        punkte.append(
            _block(
                max(als_wanduhr(aufgabe.scheduled_start), beginn),
                min(als_wanduhr(aufgabe.scheduled_end), ende),
                "aufgabe", aufgabe.title,
                herkunft_typ="todo", herkunft_id=str(aufgabe.id),
            )
        )

    punkte.sort(key=lambda b: (b["start"], b["ende"]))
    return _entlappen(punkte)


def _entlappen(bloecke: list[dict]) -> list[dict]:
    """Überlappungen auflösen: der früher beginnende Block behält seine Zeit.

    Doppelbuchungen sind im Kalender erlaubt und kommen vor. Eine Decke muss
    trotzdem eine Folge sein, sonst summieren sich die Minuten über den Tag
    hinaus. Der spätere Block wird gekürzt oder fällt weg. Was dabei verloren
    geht, meldet `baue_decke` als `ueberlappungen`, statt es zu verschweigen.
    """
    ergebnis: list[dict] = []
    cursor: datetime | None = None
    for block in bloecke:
        start = block["start"] if cursor is None else max(block["start"], cursor)
        if block["ende"] <= start:
            continue
        if start != block["start"]:
            block = {**block, "start": start, "minuten": _minuten(start, block["ende"])}
        ergebnis.append(block)
        cursor = block["ende"]
    return ergebnis


def _luecken(beginn: datetime, ende: datetime, belegt: list[dict]) -> list[tuple[datetime, datetime]]:
    """Was zwischen den Fixpunkten im wachen Fenster offen bleibt."""
    offen: list[tuple[datetime, datetime]] = []
    cursor = beginn
    for block in belegt:
        if block["ende"] <= beginn or block["start"] >= ende:
            continue
        start = max(block["start"], beginn)
        if start > cursor:
            offen.append((cursor, start))
        cursor = max(cursor, min(block["ende"], ende))
    if cursor < ende:
        offen.append((cursor, ende))
    return [(a, b) for a, b in offen if b > a]


def _ankerzeit(
    von: datetime, bis: datetime, fenster: tuple[time, time], wunsch: time, dauer: int
) -> datetime | None:
    """Wann in [von, bis) eine Mahlzeit liegen soll, oder None, wenn sie nicht passt.

    Gesucht wird die Wunschzeit, geklemmt in das erlaubte Fenster und in die
    tatsächlich offene Spanne. Passt sie nicht mehr ganz hinein, gibt es keinen
    Anker: lieber keine Mahlzeit in dieser Lücke als eine um halb neun morgens.
    """
    tag = von.date()
    f_von = max(datetime.combine(tag, fenster[0]), von)
    f_bis = min(datetime.combine(tag, fenster[1]), bis)
    if f_bis - f_von < timedelta(minutes=dauer):
        return None
    ziel = datetime.combine(tag, wunsch)
    spaetester = f_bis - timedelta(minutes=dauer)
    return min(max(ziel, f_von), spaetester)


def _abschnitt(zeitpunkt: datetime) -> str:
    """Vormittag, Nachmittag oder Abend, für die Benennung freier Zeit."""
    name = ABSCHNITTE[0][1]
    for grenze, bezeichnung in ABSCHNITTE:
        if zeitpunkt.time() >= grenze:
            name = bezeichnung
    return name


def _freie_zeit(von: datetime, bis: datetime, zustand: dict) -> list[dict]:
    """Eine offene Strecke als Erholung benennen, an Tagesabschnitten gegliedert.

    ★ Die Gliederung ist der Punkt. Am Stück ergab das auf einem freien Tag einen
    Block „Freie Zeit" von 11:10 bis 22:00, und elf Stunden mit einem Namen sind
    keine Planung. Geschnitten wird an den Abschnittsgrenzen und am Beginn des
    Ausklangs, also dort, wo sich der Charakter der Zeit tatsächlich ändert.
    """
    stuecke: list[dict] = []
    cursor = von
    while cursor < bis:
        grenzen = [bis, zustand["ausklang"]] if cursor < zustand["ausklang"] else [bis]
        for grenze, _ in ABSCHNITTE:
            kandidat = datetime.combine(cursor.date(), grenze)
            if kandidat > cursor:
                grenzen.append(kandidat)
        schnitt = min(g for g in grenzen if g > cursor)
        if _minuten(cursor, schnitt) < MIN_BLOCK_MINUTEN and schnitt < bis:
            # Zu kurz für einen eigenen Block: mit dem nächsten Stück zusammen.
            schnitt = min(bis, schnitt + timedelta(minutes=MIN_BLOCK_MINUTEN))
        if _minuten(cursor, schnitt) < MIN_BLOCK_MINUTEN:
            # Ein Rest von zehn Minuten zwischen zwei Terminen ist keine Erholung,
            # sondern der Weg von einem zum anderen. Ihn „Freie Zeit" zu nennen
            # verspricht etwas, das die Zeit nicht halten kann.
            stuecke.append(_block(cursor, schnitt, "puffer", "Puffer",
                                  begruendung="Übergang"))
        elif cursor >= zustand["ausklang"]:
            stuecke.append(_block(cursor, schnitt, "erholung", "Ausklang",
                                  begruendung="letzte Stunden vor dem Schlafen bewusst ruhig"))
        else:
            stuecke.append(_block(
                cursor, schnitt, "erholung", f"Freie Zeit am {_abschnitt(cursor)}",
                begruendung=zustand["erholung_grund"],
            ))
        cursor = schnitt
    return stuecke


def _fuelle_luecke(
    von: datetime,
    bis: datetime,
    zustand: dict,
) -> list[dict]:
    """Eine offene Spanne benennen.

    Zwei Schritte, und die Trennung ist der Kern: zuerst werden **Anker** an ihre
    richtige Uhrzeit gesetzt (Mahlzeiten wollen zur Mahlzeit, nicht zum
    Lückenbeginn), danach werden die Zwischenräume als freie Zeit benannt. Die
    erste Fassung hatte beides vermischt und alles der Reihe nach an den Cursor
    gelegt, was Mittagessen um 08:40 ergab.
    """
    if _minuten(von, bis) < MIN_BLOCK_MINUTEN:
        return [_block(von, bis, "puffer", "Puffer", begruendung="zu kurz zum Verplanen")]

    anker: list[dict] = []
    cursor = von

    # Was unmittelbar am Anfang der Spanne steht, weil es dort hingehört.
    if zustand["erster_block"]:
        ende = min(cursor + timedelta(minutes=MORGENROUTINE_MINUTEN), bis)
        anker.append(_block(cursor, ende, "grundlast", "Morgenroutine",
                            begruendung="die erste Stunde nach dem Aufstehen gehört sich selbst"))
        cursor = ende
        zustand["erster_block"] = False
    if zustand.pop("nach_langem_block", False) and _minuten(cursor, bis) >= MIN_BLOCK_MINUTEN:
        ende = min(cursor + timedelta(minutes=ANKOMMEN_MINUTEN), bis)
        anker.append(_block(cursor, ende, "erholung", "Ankommen",
                            begruendung="nach einem langen Pflichtblock zuerst herunterkommen"))
        cursor = ende

    # Mahlzeiten an ihre Uhrzeit, nicht an den Cursor.
    for erledigt, fenster, wunsch, dauer, titel in (
        ("mittag", MITTAG_FENSTER, MITTAG_WUNSCH, MITTAGESSEN_MINUTEN, "Mittagessen"),
        ("abend", ABEND_FENSTER, ABEND_WUNSCH, ABENDESSEN_MINUTEN, "Abendessen"),
    ):
        if zustand[erledigt]:
            continue
        start = _ankerzeit(cursor, bis, fenster, wunsch, dauer)
        if start is None:
            continue
        anker.append(_block(start, start + timedelta(minutes=dauer), "grundlast", titel,
                            begruendung="Mahlzeit fällt täglich an"))
        zustand[erledigt] = True

    # Training: in die erste Strecke, die lang genug ist und vor dem Ausklang liegt.
    if zustand["training_offen"]:
        start = _training_platz(cursor, bis, anker, zustand)
        if start is not None:
            anker.append(_block(start, start + timedelta(minutes=TRAINING_MINUTEN),
                                "training", "Training",
                                begruendung=zustand["training_grund"]))
            zustand["training_offen"] = False

    anker.sort(key=lambda b: b["start"])

    # Zwischenräume benennen.
    gefuellt: list[dict] = []
    lauf = von
    for block in anker:
        if block["start"] > lauf:
            gefuellt.extend(_freie_zeit(lauf, block["start"], zustand))
        gefuellt.append(block)
        lauf = block["ende"]
    if lauf < bis:
        gefuellt.extend(_freie_zeit(lauf, bis, zustand))
    return gefuellt


def _training_platz(
    von: datetime, bis: datetime, belegt: list[dict], zustand: dict
) -> datetime | None:
    """Erster Platz für eine Trainingseinheit, der lang genug und nicht zu spät ist."""
    grenze = min(bis, zustand["ausklang"])
    kanten = sorted(belegt, key=lambda b: b["start"])
    cursor = von
    for block in kanten:
        if block["start"] > cursor and _minuten(cursor, min(block["start"], grenze)) >= TRAINING_MINUTEN:
            return cursor
        cursor = max(cursor, block["ende"])
    if _minuten(cursor, grenze) >= TRAINING_MINUTEN:
        return cursor
    return None


def _training_grund(capacity: dict) -> tuple[bool, str]:
    """Trägt der Tag einen Trainingsblock, und warum (nicht)?

    Der Kalender entscheidet nur die Zeit. Was trainiert wird, beantwortet die
    Fitness-App, wenn der Block läuft.

    ★★ Hier stand zuerst `not capacity["allow_physical"]` als Ausschluss. Das ist
    falsch, und zwar auf die unangenehme Art. `allow_physical` verlangt Level
    `geladen` oder `normal`; ein Arbeitstag **ohne jeden Check-in** landet über
    die Tagestyp-Basis (45 Punkte) bei `geschont` und fiel damit durch. Ergebnis:
    an keinem einzigen Arbeitstag wurde Training eingeplant, begründet mit „heute
    körperlich nicht belastbar", obwohl der Mensch nichts dergleichen gesagt
    hatte. Eine Behauptung über seinen Körper, erfunden aus dem Umstand, dass er
    arbeitet.

    Deshalb: verzichtet wird nur auf **Angabe** (Check-in sagt nicht belastbar),
    bei Krankheit oder bei tatsächlich erschöpfter Kapazität. Ein Tag ohne
    Aussage bekommt seinen Block, denn genau dafür ist er da.
    """
    if capacity.get("day_type") == "krank":
        return False, "Krankheitstag"
    checkin = capacity.get("checkin") or {}
    if checkin.get("physical_ready") is False:
        return False, "du hast heute angegeben, körperlich angeschlagen zu sein"
    if capacity.get("level") == "erschöpft":
        return False, "Kapazität heute im roten Bereich"
    if capacity.get("prefer_physical"):
        return True, "freier Tag mit guter Kapazität, beste Gelegenheit"
    if not capacity.get("has_checkin"):
        return True, "Platz ist da, und ohne Check-in wird nichts über den Tag angenommen"
    return True, "Kapazität trägt eine Einheit"


def baue_decke(db: Session, tag: date, now: datetime | None = None) -> dict:
    """Die lueckenlose Decke eines Tages.

    Ist der Tag festgeschrieben, wird das Protokoll gelesen. Sonst wird frisch
    gerechnet: Fixpunkte aus ihren Quellen, dazwischen die eigenen Bloecke.
    """
    from backend.daily_energy import compute_day_capacity

    beginn, ende = wach_fenster(db, tag)
    capacity = compute_day_capacity(db, tag)

    if ist_festgeschrieben(db, tag):
        zeilen = (
            db.query(TimeBlock)
            .filter(TimeBlock.date == tag)
            .order_by(TimeBlock.start)
            .all()
        )
        bloecke = [
            _block(
                als_wanduhr(z.start), als_wanduhr(z.end), z.art, z.titel,
                begruendung=z.begruendung, status=z.status,
                herkunft_typ=z.herkunft_typ, herkunft_id=z.herkunft_id,
                block_id=z.id,
            )
            for z in zeilen
        ]
        return _antwort(tag, beginn, ende, bloecke, capacity, festgeschrieben=True)

    fixpunkte = sammle_fixpunkte(db, tag)
    manuelle = _manuelle_bloecke(db, tag)
    belegt = _entlappen(sorted(fixpunkte + manuelle, key=lambda b: (b["start"], b["ende"])))

    langer_block_ende: datetime | None = None
    for block in belegt:
        if block["art"] == "fix" and block["minuten"] >= LANGER_BLOCK_STUNDEN * 60:
            langer_block_ende = block["ende"]

    training_moeglich, training_grund = _training_grund(capacity)
    zustand = {
        "erster_block": True,
        "mittag": False,
        "abend": False,
        # ★ Nur `training` schließt einen weiteren Trainingsblock aus, nicht
        # `gewohnheit`. Die erste Fassung zählte jede Gewohnheit als Einheit mit,
        # womit an einem Tag mit 45 Minuten Gitarre kein Sport mehr geplant
        # wurde. Ob eine Gewohnheit körperlich ist, steht nirgends; sie einfach
        # dafür zu halten, ist geraten. Ein Block zu viel lässt sich wegziehen,
        # ein nie geplanter fällt nicht auf.
        "training_offen": training_moeglich and not _hat_art(belegt, ("training",)),
        "training_grund": training_grund,
        "ausklang": ausklang_beginn(db, tag),
        "erholung_grund": _erholung_grund(capacity),
        "nach_langem_block": False,
    }

    decke = list(belegt)
    for von, bis in _luecken(beginn, ende, belegt):
        zustand["nach_langem_block"] = langer_block_ende is not None and von == langer_block_ende
        decke.extend(_fuelle_luecke(von, bis, zustand))

    decke.sort(key=lambda b: (b["start"], b["ende"]))
    antwort = _antwort(tag, beginn, ende, decke, capacity, festgeschrieben=False)
    if not training_moeglich:
        antwort["hinweise"].append(f"Kein Training geplant: {training_grund}.")
    return antwort


def _hat_art(bloecke: list[dict], arten: tuple[str, ...]) -> bool:
    return any(b["art"] in arten for b in bloecke)


def _erholung_grund(capacity: dict) -> str:
    level = capacity.get("level")
    if level == "erschöpft":
        return "heute zählt Erholung mehr als Erledigen"
    if level == "geschont":
        return "Tag bewusst nicht randvoll"
    return "freie Zeit, bewusst als Block statt als Rest"


def _manuelle_bloecke(db: Session, tag: date) -> list[dict]:
    """Von Hand gesetzte Bloecke eines noch offenen Tages.

    Sie ueberleben eine Neuplanung. Wer einen Block selbst gesetzt hat, will ihn
    nicht beim naechsten Aufruf verlieren.
    """
    zeilen = (
        db.query(TimeBlock)
        .filter(TimeBlock.date == tag, TimeBlock.quelle == "manuell")
        .order_by(TimeBlock.start)
        .all()
    )
    return [
        _block(
            als_wanduhr(z.start), als_wanduhr(z.end), z.art, z.titel,
            begruendung=z.begruendung, status=z.status,
            herkunft_typ=z.herkunft_typ, herkunft_id=z.herkunft_id, block_id=z.id,
        )
        for z in zeilen
    ]


def _antwort(
    tag: date,
    beginn: datetime,
    ende: datetime,
    bloecke: list[dict],
    capacity: dict,
    *,
    festgeschrieben: bool,
) -> dict:
    gezaehlt = [b for b in bloecke if b["status"] != "verworfen"]
    summe: dict[str, int] = {}
    for block in gezaehlt:
        summe[block["art"]] = summe.get(block["art"], 0) + block["minuten"]
    wach = _minuten(beginn, ende)
    belegt = sum(summe.values())
    return {
        "datum": tag,
        "wach_von": beginn,
        "wach_bis": ende,
        "wach_minuten": wach,
        "verplant_minuten": belegt,
        "offen_minuten": max(0, wach - belegt),
        "summe_je_art": summe,
        "bloecke": bloecke,
        "capacity": capacity,
        "festgeschrieben": festgeschrieben,
        "hinweise": [],
    }


def ist_festgeschrieben(db: Session, tag: date) -> bool:
    """Wurde dieser Tag vom Spiegel zum Protokoll gemacht?

    ★ Gelesen wird ein ausdrücklicher Marker, nicht die Gestalt der Daten. Der
    erste Entwurf schloss von "es liegen gespiegelte Blöcke als Zeilen vor" auf
    festgeschrieben. Das ist an einem Tag ohne einen einzigen Termin falsch, und
    genau der ist beim Nachtragen der Normalfall: er enthält nur Erholung und
    Grundlast, also nichts Gespiegeltes. Der Tag galt weiter als offen und wurde
    bei jedem Aufruf neu gerechnet.
    """
    return (
        db.query(TimeBlock)
        .filter(TimeBlock.date == tag, TimeBlock.festgeschrieben_am.isnot(None))
        .first()
        is not None
    )


def festschreiben(db: Session, tag: date, now: datetime | None = None) -> dict:
    """Die gerechnete Decke eines Tages zum Protokoll machen.

    Ab hier ist der Tag eine Aufzeichnung und kein Spiegel mehr: ein spaeter
    verschobener Termin aendert ihn nicht mehr. Genau das will man, bevor jemand
    abends korrigiert, was er tatsaechlich getan hat.

    Idempotent: ein bereits festgeschriebener Tag wird unveraendert
    zurueckgegeben, damit ein zweiter Aufruf die Korrekturen nicht ueberschreibt.
    """
    if ist_festgeschrieben(db, tag):
        return baue_decke(db, tag, now=now)

    decke = baue_decke(db, tag, now=now)
    stempel = utcnow()
    vorhandene = {
        z.id: z for z in db.query(TimeBlock).filter(TimeBlock.date == tag).all()
    }
    for block in decke["bloecke"]:
        if block["id"] in vorhandene:
            # Von Hand gesetzte Blöcke gab es schon vor dem Festschreiben. Sie
            # werden mitgestempelt, sonst hinge der Marker nur an den erzeugten
            # Zeilen und ein Tag, der ausschließlich aus manuellen Blöcken
            # besteht, bliebe unerkannt offen.
            vorhandene[block["id"]].festgeschrieben_am = stempel
            continue
        db.add(
            TimeBlock(
                date=tag,
                start=block["start"],
                end=block["ende"],
                art=block["art"],
                titel=block["titel"],
                begruendung=block["begruendung"],
                status=block["status"],
                quelle="plan",
                herkunft_typ=block["herkunft_typ"],
                herkunft_id=block["herkunft_id"],
                festgeschrieben_am=stempel,
            )
        )
    db.commit()
    return baue_decke(db, tag, now=now)


def block_aendern(
    db: Session,
    block_id: str,
    *,
    start: datetime | None = None,
    ende: datetime | None = None,
    titel: str | None = None,
    art: str | None = None,
    status: str | None = None,
) -> TimeBlock | None:
    """Einen Block verschieben, umbenennen, umwidmen oder verwerfen.

    Jede Aenderung stempelt `quelle=manuell`: der Block ist damit vor der
    naechsten automatischen Planung geschuetzt.
    """
    zeile = db.query(TimeBlock).filter(TimeBlock.id == block_id).first()
    if zeile is None:
        return None
    if start is not None:
        zeile.start = als_wanduhr(start)
    if ende is not None:
        zeile.end = als_wanduhr(ende)
    if titel is not None:
        zeile.titel = titel
    if art is not None:
        zeile.art = art
    if status is not None:
        zeile.status = status
    if zeile.end <= zeile.start:
        raise ValueError("Ende muss nach dem Beginn liegen")
    zeile.date = zeile.start.date()
    zeile.quelle = "manuell"
    db.commit()
    db.refresh(zeile)
    return zeile


def block_anlegen(
    db: Session,
    tag: date,
    start: datetime,
    ende: datetime,
    art: str,
    titel: str,
    begruendung: str | None = None,
) -> TimeBlock:
    """Einen Block von Hand setzen, etwa das, was man stattdessen getan hat."""
    start = als_wanduhr(start)
    ende = als_wanduhr(ende)
    if ende <= start:
        raise ValueError("Ende muss nach dem Beginn liegen")
    if art not in ARTEN:
        raise ValueError(f"Unbekannte Art: {art}")
    zeile = TimeBlock(
        date=tag,
        start=start,
        end=ende,
        art=art,
        titel=titel,
        begruendung=begruendung,
        status="bestaetigt",
        quelle="manuell",
    )
    db.add(zeile)
    db.commit()
    db.refresh(zeile)
    return zeile


def neu_ordnen(db: Session, tag: date, reihenfolge: list[str]) -> dict:
    """Die Blöcke eines Tages in eine neue Reihenfolge bringen.

    Nötig, weil die Decke lückenlos ist: wer einen Block vor einen anderen zieht,
    verschiebt nicht nur diesen einen, sondern alle dahinter. Das im Browser zu
    rechnen hieße, den Tag in einem Dutzend einzelner Aufrufe umzuschreiben, von
    denen jeder scheitern kann. Ein abgebrochener Lauf hinterließe eine Decke mit
    Löchern oder Überlappungen, und zwar genau die, deren Abwesenheit die
    Kernzusage dieses Moduls ist.

    Die Dauern bleiben, nur die Lage ändert sich. Blöcke, die nicht in
    `reihenfolge` stehen, behalten ihren Platz am Ende in bisheriger Ordnung.

    ★ Verworfene Blöcke bekommen ebenfalls eine Zeit und bleiben in der Kette.
    Sie zu überspringen wäre naheliegend, weil sie nicht zur Bilanz zählen, risse
    aber genau dort ein Loch in die Decke, wo vorher etwas geplant war, und die
    Lückenlosigkeit ist die Kernzusage dieses Moduls. Sichtbar bleibt der Block
    als das, was er ist: hier war etwas vorgesehen und fand nicht statt. Wer die
    Zeit anders gefüllt hat, legt einen eigenen Block an.

    Nur an einem festgeschriebenen Tag sinnvoll: an einem offenen Tag kämen die
    gespiegelten Blöcke beim nächsten Aufruf wieder aus ihrer Quelle.
    """
    if not ist_festgeschrieben(db, tag):
        raise ValueError(
            "Der Tag ist noch nicht festgeschrieben. Erst festschreiben, dann ordnen."
        )

    zeilen = {
        z.id: z
        for z in db.query(TimeBlock).filter(TimeBlock.date == tag).all()
    }
    unbekannt = [kennung for kennung in reihenfolge if kennung not in zeilen]
    if unbekannt:
        raise ValueError(f"Unbekannte Blöcke: {', '.join(unbekannt)}")

    beginn, _ = wach_fenster(db, tag)
    # Erst die genannten in der gewünschten Folge, dann der Rest nach Startzeit.
    genannt = [zeilen[kennung] for kennung in reihenfolge]
    rest = sorted(
        (z for kennung, z in zeilen.items() if kennung not in set(reihenfolge)),
        key=lambda z: als_wanduhr(z.start),
    )

    cursor = beginn
    for zeile in genannt + rest:
        dauer = _minuten(als_wanduhr(zeile.start), als_wanduhr(zeile.end))
        zeile.start = cursor
        zeile.end = cursor + timedelta(minutes=dauer)
        zeile.quelle = "manuell"
        cursor = zeile.end
    db.commit()
    return baue_decke(db, tag)


def abgleich(db: Session, tag: date) -> dict:
    """Soll gegen Ist: was geplant war, was gemessen wurde, was auseinanderging.

    Die gemessenen Intervalle (`ZeitIst`, von einem Geraet geliefert) liegen quer
    zu den Bloecken. Zugeordnet wird deshalb ueber die groesste zeitliche
    Ueberdeckung, und das Ergebnis ist bewusst nur eine Auswertung: es aendert
    keine Zeile. Wer sich dabei vertut, hat einen falschen Bericht und keine
    falschen Daten.
    """
    decke = baue_decke(db, tag)
    beginn, ende = tagesgrenzen(tag)
    messungen = (
        db.query(ZeitIst)
        .filter(ZeitIst.start < ende, ZeitIst.end > beginn)
        .order_by(ZeitIst.start)
        .all()
    )

    ist_je_art: dict[str, int] = {}
    zuordnung: list[dict] = []
    for messung in messungen:
        m_start = als_wanduhr(messung.start)
        m_ende = als_wanduhr(messung.end)
        ist_je_art[messung.art] = ist_je_art.get(messung.art, 0) + _minuten(m_start, m_ende)

        bester: dict | None = None
        beste_deckung = 0
        for block in decke["bloecke"]:
            deckung = _minuten(
                max(m_start, block["start"]), min(m_ende, block["ende"])
            )
            if deckung > beste_deckung:
                bester, beste_deckung = block, deckung
        zuordnung.append({
            "ist_id": messung.id,
            "start": m_start,
            "ende": m_ende,
            "art": messung.art,
            "titel": messung.titel,
            "quelle": messung.quelle,
            "block_id": bester["id"] if bester else None,
            "block_titel": bester["titel"] if bester else None,
            "deckung_minuten": beste_deckung,
            "passt": bool(bester and bester["art"] == messung.art),
        })

    soll_je_art = decke["summe_je_art"]
    arten = sorted(set(soll_je_art) | set(ist_je_art))
    return {
        "datum": tag,
        "hat_messungen": bool(messungen),
        "soll_je_art": soll_je_art,
        "ist_je_art": ist_je_art,
        "abweichung_je_art": {
            art: ist_je_art.get(art, 0) - soll_je_art.get(art, 0) for art in arten
        },
        "verworfen": [b for b in decke["bloecke"] if b["status"] == "verworfen"],
        "zuordnung": zuordnung,
    }


def rueckblick(db: Session, von: date, bis: date) -> dict:
    """Wofuer die Zeit ueber mehrere Tage ging, und was regelmaessig ausfaellt.

    Die zweite Zahl ist die interessantere: ein Block, der Woche fuer Woche
    geplant und verworfen wird, ist eine Planung, die nicht zum Leben passt.
    """
    zeilen = (
        db.query(TimeBlock)
        .filter(TimeBlock.date >= von, TimeBlock.date <= bis)
        .all()
    )
    gelebt: dict[str, int] = {}
    ausgefallen: dict[str, int] = {}
    for zeile in zeilen:
        minuten = _minuten(als_wanduhr(zeile.start), als_wanduhr(zeile.end))
        if zeile.status == "verworfen":
            ausgefallen[zeile.art] = ausgefallen.get(zeile.art, 0) + minuten
        else:
            gelebt[zeile.art] = gelebt.get(zeile.art, 0) + minuten
    tage = (bis - von).days + 1
    return {
        "von": von,
        "bis": bis,
        "tage": tage,
        "festgeschriebene_tage": len({z.date for z in zeilen}),
        "gelebt_je_art": gelebt,
        "ausgefallen_je_art": ausgefallen,
        "schnitt_je_tag": {
            art: round(minuten / tage) for art, minuten in gelebt.items()
        },
    }
