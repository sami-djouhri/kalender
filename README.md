# kalender

![Python](https://img.shields.io/badge/Python-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)
![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite&logoColor=white)
![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)

A personal calendar that plans with you instead of only recording: appointments,
tasks, goals, habits and contacts, plus a day type that other systems can act
on. Part of the [Saganta Suite](https://github.com/sami-djouhri/saganta-suite),
usable on its own.

Frontend is hand-written: a month, week and year grid in plain JavaScript, no
calendar library, no build step. All user-facing text is German.

## The day type

Every day resolves to exactly one type along a fixed chain:

```
holiday > sick > vacation > school > work > free
```

`/api/day-type/today` answers that question for anything that wants to act on
it, most obviously home automation deciding when to wake someone. It is the one
place that decides; there is deliberately no second implementation anywhere.

## The habit scheduler

Habits are not reminders. `scheduler.py` distributes sessions over the week by
itself: it respects each habit's time windows, avoids collisions with existing
appointments, skips sick days, caps the daily load, and merges adjacent sessions
of the same habit. A proposal can be accepted, dismissed or started; a dismissed
one gets rescheduled elsewhere in the same week.

On top of that there is an adaptive layer that is off until you feed it. A
morning check-in (sleep, energy, mood) produces a day capacity, and planning
respects it. Without a check-in the behaviour is byte-for-byte the old one.

## Three things worth knowing

**Naive timestamps are wall-clock local time, not UTC.** A weekly 10:00 slot
stays at 10:00 across daylight saving instead of drifting twice a year. That is
right for a personal calendar and wrong for almost everything else, so it is
enforced in one place (`backend/wanduhr.py`) rather than re-decided at every
module boundary. It had been re-decided at every module boundary, and one
module read the same values as UTC: appointments after 22:00 on the last day of
a month view were invisible.

**FastAPI ignores query parameters it does not know**, so a caller with a typo
gets a plausible answer to a different question. This has caused four separate
bugs here, including a client that was silently calendar-blind for months.
`backend/parameter_wache.py` rejects unknown parameters, with a warning-only
mode to run first so you can see what would break.

**The feed token travels as a query parameter** and cannot travel any other way,
because calendar subscriptions cannot send a header. So it ends up in the access
log of every request: 79 % of log lines carried it in clear text. A logging
filter masks the value and keeps the parameter name, so it stays visible that a
call was token-authenticated.

## Multi-tenant

Every row belongs to an owner. The only rows shared across tenants are public
holidays, and those are protected twice: they carry no owner **and** live in a
reserved calendar. Before that, any tenant could delete the 25th of December for
everyone, which would flip the day type that a wake-up alarm hangs on.

## Tests

```bash
./run-tests.sh        # expects "Ran 253+ tests ... OK"
./simulieren.sh       # 84 days of simulated everyday life, then probes over it
```

The test runner states its expected count on purpose. Nine test modules used to
be skipped silently because an optional dependency was missing, which meant 147
tests passing looked exactly like 192 tests passing.

`simulieren.sh` is the counterpart to the unit tests: those check the parts, it
checks the interplay over weeks. Five of the findings above came out of its
first run.

## License

AGPL-3.0.

## About this snapshot

The recipe, not the data. Operating notes for the instance this grew on, the
secrets vault and the home-network compose overlay are not in here.

The development history stays private; the public one starts at the first
release and grows with each one.
