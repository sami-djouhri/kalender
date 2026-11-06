"""Sleep schedule configuration per day type."""

BUFFER_MINUTES = 120

# daytype -> {wake: (hour, minute), bed: (hour, minute)}
SLEEP_SCHEDULE = {
    "arbeit": {"wake": (5, 30), "bed": (21, 30)},
    "schule": {"wake": (6, 0), "bed": (22, 0)},
    "frei": {"wake": (8, 0), "bed": (0, 0)},
    "wochenende": {"wake": (8, 0), "bed": (0, 0)},
    "urlaub": {"wake": (8, 0), "bed": (0, 0)},
    "feiertag": {"wake": (8, 0), "bed": (0, 0)},
    "krank": {"wake": (9, 0), "bed": (1, 0)},
}


def get_schedule(daytype: str) -> dict:
    """Return sleep schedule for a day type. Falls back to 'frei'."""
    return SLEEP_SCHEDULE.get(daytype, SLEEP_SCHEDULE["frei"])


def get_buffer_start(daytype: str) -> tuple[int, int]:
    """Return (hour, minute) when the buffer zone starts (BUFFER_MINUTES before bed)."""
    sched = get_schedule(daytype)
    bed_h, bed_m = sched["bed"]
    total_bed = bed_h * 60 + bed_m
    # Handle midnight/past-midnight: treat as 24:00 / 25:00
    if total_bed == 0:
        total_bed = 24 * 60
    elif total_bed < 12 * 60:
        total_bed += 24 * 60
    buf_start = total_bed - BUFFER_MINUTES
    return (buf_start // 60, buf_start % 60)
