# Epoche: Der adaptive Sekretär (Kalender-Engine v2)

> Ziel (Owner-Vision 2026-07-18): Der Kalender plant sich sinnvoll **selbst** und sagt
> mir laufend, was ich als Nächstes tun soll. Meine Zeit wird mathematisch + KI-gestützt
> optimal getimed: Arbeit/Schule/Lernen/Todos/Termine/Pausen/Erholung/Schlaf/Hobbys.
> Burnout verhindern, ungenutzte Zeit nutzen, messbarer Fortschritt.

## Was vorher schon da war (nicht neu bauen)
- `scheduler.py`: Habit-Session-Scheduling mit Strain-System, Wochen-Caps, Lern-Caps,
  Ruhetage, Ziel-Override, Pool-Todo-Auto-Plan (`auto_plan_todos`, `compute_free_slots`).
- `secretary.py`: Morgens verbindlich planen, Konflikt-Erkennung, Briefing/Abend-Nudge,
  60s-Tick über den `session_notifications`-Runner.
- Day-Type-System (arbeit/schule/urlaub/krank/feiertag/wochenende/frei), Sleep-Schedule.
- `DailyLoad(strain, is_off_day, deferred_minutes)`, `DailyReview(energy_level)`,
  `Todo.energy_required` (war reserviert, NICHT verdrahtet), Habit-Quoten
  (`target_hours_per_week`, `weekly_minimum_hours`).

## Neu in dieser Epoche (additiv, `secretary.py` bleibt unangetastet: Namensfilter)

### 1. Morgen-Check-in: das Tagessignal (`DailyCheckIn`)
Wie fit fühlst du dich? (Schlaf gut/schlecht, Energie hoch/mittel/niedrig, Stimmung,
körperlich belastbar ja/nein). Steuert den ganzen Tag: schlecht geschlafen → Tag nicht
randvoll, harte/körperliche Aufgaben verschoben.

### 2. Kapazitäts-Modell (`daily_energy.py`)
`compute_day_capacity(day)` = Check-in + Day-Type + Strain → `{level, factor, cap_minutes,
allow_physical, reason}`. **Rückwärtskompatibel:** ohne Check-in und ohne hohen Strain =
`normal` (factor 1.0), Verhalten identisch zu vorher.
- Energie niedrig / Schlaf schlecht → `geschont` (factor ~0.5, kein körperlich-Hartes).
- Off-Day + Energie hoch → `geladen` (körperliche Aktivität bevorzugt, höherer Cap).
- Hoher Strain (>0.7) → gedämpft.

### 3. Energie-bewusste Planung (`scheduler.auto_plan_todos`, opt-in)
`energy_required` zählt jetzt: an geschonten Tagen werden `hoch`-Energie-Todos übersprungen,
das geplante Gesamtvolumen wird per Kapazitätsfaktor gedeckelt (Luft lassen = Burnout-Schutz).
Greift nur, wenn für den Tag ein Check-in existiert → Bestandstests unberührt.

### 4. Vorschlags-Engine (`suggestions.py`): „sag mir was ich tun soll"
`build_suggestions(day, now)` = gerankte Vorschläge aus ALLEN Lebensbereichen, gefiltert
nach freier Zeit + Kapazität + Day-Type:
- Hobbys/Habits mit gefährdeter Wochenquote („Gitarre: 1,5 h offen").
- Sport (Fitness-App: Muskel-Recovery), bei niedriger Energie unterdrückt, dann Erholung/Buch.
- Einkauf (Lager kritisch + MealPrep-Einkaufsliste): „Einkaufen, 3 Artikel offen".
- Todos (fällig/überfällig).
- Erholung explizit, wenn `geschont`.
- **Entscheidungs-Modus:** konkurrieren zwei Optionen um denselben Slot → „Wozu hast du Lust?
  Sport oder Buch?" (nach langem Arbeitstag eher Buch).

### 5. Messbarer Fortschritt (`build_progress`)
Pro Habit: Ziel vs. erreicht diese Woche, %, Streak, Quote-gefährdet. Balance über Kategorien.

### 6. Cross-App-Konnektoren (`cross_app.py`, fail-soft, read-only, headerlos→Owner)
- Fitness `GET /api/progress/muscle-freshness`, `GET /api/workouts`
- MealPrep `GET /shopping/current`
- Lager `GET /api/critical`, `GET /api/stats/low-stock`
Timeout ~3 s, jeder Fehler = Skip (Planung bricht nie).

### 7. Proaktive Push-Nudges (`assistant.py`-Tick, im 60s-Runner)
- Morgens ohne Check-in → „Wie hast du geschlafen?" (Check-in-Prompt).
- Nach Arbeit/Schule + freie Zeit → „Hast du heute Lust auf Sport?" mit Top-Vorschlag.
Idempotent über `SecretaryRun`-Marker (`checkin_prompt`, `activity_nudge`).

### 8. API (`routers/assistant.py`, JWT-gated wie der Rest)
`GET/POST /api/assistant/checkin`, `GET /api/assistant/capacity`,
`GET /api/assistant/suggestions`, `GET /api/assistant/progress`, `GET /api/assistant/today`
(angereichertes Briefing: Termine + Kapazität + Check-in-Status + Top-Vorschläge).

### 9. Surface: kalender-bff proxied die neuen Endpunkte, Saganta-Kalender zeigt
Check-in-Karte, „Was jetzt?"-Vorschläge und Fortschrittsbalken.

## Prinzipien
- Fest bleibt fest (Termine/Geburtstage = harte Blocker). Hobbys sind aufschiebbar,
  notfalls unter Quote (Burnout-Schutz vor Quotentreue).
- Deterministisch + regelbasiert (kein LLM-Zwang); Cross-App fail-soft.
- „Weniger Dashboards, mehr Vernetzung", der Kalender verdrahtet Fitness/MealPrep/Lager,
  statt neue UIs zu bauen.
</content>
</invoke>
