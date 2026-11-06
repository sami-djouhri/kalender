"""Zuschnitt gemessener Zeit: eine Regel an einer Stelle statt in fuenf Meldern.

Ein Melder liefert, was er sieht, und das ist roher als die Wahrheit. Der Laptop
sieht Minecraft im Vordergrund, auch wenn jemand nur die Welt sucht und wieder
zuklappt; der Leser im Browser sieht eine sichtbare Seite, auch wenn zwischen zwei
Kapiteln jemand Kaffee holt. Daraus eine brauchbare Zahl zu machen ist **eine**
Aufgabe und gehoert deshalb **einmal** hierher, nicht in jeden Melder.

Warum nicht im Melder
---------------------
Fuenf Melder mit je eigenem Zuschnitt sind fuenf Regeln, die auseinanderlaufen, und
ihre Abweichung sieht man nicht: alle liefern plausible Minuten. Dazu kommt der
praktische Grund, dass ein Melder auf einem fremden Wirt laeuft (Laptop, node1) und
eine Regelaenderung dort ein Ausrollen kostet.

Warum beim Auswerten und nicht beim Aufnehmen
---------------------------------------------
Die Rohlage bleibt erhalten. Das ist derselbe Grund, aus dem `ZeitIst` eine
eigene Tabelle ist und nicht zwei Spalten an `TimeBlock` (Begruendung im Modell):
wer beim Einliefern presst, muss sofort entscheiden und kann es nicht revidieren.
Stellt sich die Luecken-Schwelle als falsch heraus, rechnet ein geaenderter Zuschnitt
die Vergangenheit mit, statt sie verloren zu haben.

Dazu ein handfester Grund: der Schach-Melder schickt nach jedem Zug den
**Gesamtstand** der Partie und keinen Zuwachs (so holt ein fehlgeschlagener Versuch
sich von selbst nach). Wer beim Aufnehmen verbindet, muesste bestehende Zeilen
zusammenfuehren und loeschen; beim Auswerten ist dasselbe eine Summe.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Luecken bis hierher gelten als dieselbe Taetigkeit. Wer beim Lesen aufsteht und
# nach zehn Minuten weiterliest, hat nicht zweimal gelesen.
LUECKE_MINUTEN = 15

# Was kuerzer ist, war kein Tun. Ein Blick in ein Fenster ist kein Spielen, und eine
# Liste voller Zweiminuten-Eintraege macht die Bilanz unleserlich, ohne sie genauer
# zu machen.
MINDEST_MINUTEN = 10

# Fuer genaue Melder gilt eine Minute. Eine Eigenapp meldet keine Zufallstreffer:
# wer einen Zug macht, spielt Schach, und niemand hat versehentlich eine Seite
# gelesen. Dort kostet die Zehn-Minuten-Schwelle echte Minuten.
#
# ⚠️ Woran ein genauer Melder erkannt wird, entscheidet der MELDER (Feld `genau` im
# Eingang), nicht eine Liste der Eigenapps hier. Eine aufgezaehlte Liste von
# Melder-Namen veraltet still und in beide Richtungen, und genau diese Fehlerklasse
# hat dieses Vorhaben ueberhaupt erst ausgeloest: der Verteiler in `scheduler.py`
# kannte drei Kategorien, der Habit „Schach" trug eine vierte und wurde nie
# eingeplant, ohne eine Zeile Fehlermeldung. Wer genau messen kann, weiss es selbst.
MINDEST_MINUTEN_GENAU = 1


@dataclass
class Stueck:
    """Ein zugeschnittener Zeitraum. Traegt, was die Bilanz braucht, und nicht mehr."""

    start: datetime
    ende: datetime
    quelle: str
    taetigkeit: str | None
    art: str
    genau: bool = False

    @property
    def minuten(self) -> int:
        return int((self.ende - self.start).total_seconds() // 60)


def mindestdauer(genau: bool) -> int:
    """Die Schwelle, die fuer diese Messung gilt. Eine Stelle, zwei Werte."""
    return MINDEST_MINUTEN_GENAU if genau else MINDEST_MINUTEN


def _schluessel(zeile) -> tuple[str, str | None, str, bool]:
    """Was als dieselbe Taetigkeit gilt: Quelle, Kennung, Art, Genauigkeit.

    Die Art gehoert dazu, weil derselbe Melder verschiedene Arten liefern kann
    (`bibliothek` meldet Lesen, der Timer meldet, was man ihm sagt). Ohne sie
    verschmelzen zwei verschiedene Taetigkeiten zu einem langen Block.

    `genau` gehoert dazu, weil es die Schwelle der Gruppe bestimmt. Es gemeinsam
    zu gruppieren ist die ehrlichere Wahl: liefert ein Melder einmal genau und
    einmal nicht, bleiben beide Staende getrennt, statt dass der eine den anderen
    mitzieht.
    """
    return (zeile.quelle, zeile.taetigkeit, zeile.art, bool(getattr(zeile, "genau", False)))


def zuschneiden(zeilen) -> list[Stueck]:
    """Rohe Messungen zu brauchbaren Stuecken: verbinden, dann verwerfen.

    `zeilen` sind `ZeitIst`-Objekte oder beliebige Objekte mit `start`, `ende`/`end`,
    `quelle`, `taetigkeit`, `art` und optional `genau`. Die Reihenfolge der Eingabe
    ist unerheblich.

    Zuerst verbinden, dann verwerfen, und diese Reihenfolge ist der Kern: drei
    Abschnitte von je vier Minuten mit kurzen Luecken sind zwoelf Minuten Tun. Wer
    erst verwirft, loescht alle drei und meldet null.

    Die Mindestdauer kommt aus der Zeile selbst (`genau`), es gibt hier
    absichtlich keinen Parameter dafuer. Ein Aufrufer, der die Schwelle setzen
    koennte, waere ein zweiter Ort fuer dieselbe Regel, und zwei Orte heisst zwei
    Antworten auf dieselbe Frage.
    """
    gruppen: dict[tuple, list] = {}
    for zeile in zeilen:
        ende = getattr(zeile, "ende", None) or zeile.end
        gruppen.setdefault(_schluessel(zeile), []).append((zeile.start, ende))

    stuecke: list[Stueck] = []
    for (quelle, taetigkeit, art, genau), eintraege in gruppen.items():
        eintraege.sort(key=lambda e: e[0])
        offen_start, offen_ende = eintraege[0]
        for start, ende in eintraege[1:]:
            luecke = (start - offen_ende).total_seconds() / 60
            if luecke <= LUECKE_MINUTEN:
                # Auch Ueberlappungen landen hier (negative Luecke). `max` sorgt
                # dafuer, dass eine enthaltene Messung das Ende nicht zurueckzieht.
                offen_ende = max(offen_ende, ende)
            else:
                stuecke.append(Stueck(offen_start, offen_ende, quelle, taetigkeit, art, genau))
                offen_start, offen_ende = start, ende
        stuecke.append(Stueck(offen_start, offen_ende, quelle, taetigkeit, art, genau))

    behalten = [s for s in stuecke if s.minuten >= mindestdauer(s.genau)]
    behalten.sort(key=lambda s: s.start)
    return behalten
