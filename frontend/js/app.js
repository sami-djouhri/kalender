// --- State ---
let token = localStorage.getItem('kalender_token') || '';
let calendars = [];
let contacts = [];
let habits = [];
let projects = [];
let habitSessions = [];
let notifiedSessionIds = new Set();
let cachedEvents = [];
let goalDates = new Set();
let calendarStartDate = null;
let calendarEndDate = null;

// View state
let currentView = localStorage.getItem('kalender_view') || 'week';
let weekViewDate = null; // Monday of current week view
let weekTodos = [];
let weekDayTypes = [];
let weekNowLineInterval = null;
const TODO_EVENT_PREFIX = 'todo-';

// Fixed system calendar IDs
const ARBEIT_CAL_ID = 'daytype-arbeit-0000-0000-000000000000';
const SCHULE_CAL_ID = 'daytype-schule-0000-0000-000000000000';
const URLAUB_CAL_ID = 'daytype-urlaub-0000-0000-000000000000';
const KRANK_CAL_ID = 'daytype-krank-0000-0000-000000000000';
const FEIERTAG_CAL_ID = 'daytype-feiertag-0000-0000-000000000000';
const GEBURTSTAGE_CAL_ID = 'system-geburtstage-0000-0000-000000000000';
const TERMINE_CAL_ID = 'system-termine-0000-0000-000000000000';
const DAYTYPE_CAL_IDS = new Set([
    ARBEIT_CAL_ID,
    SCHULE_CAL_ID,
    URLAUB_CAL_ID,
    KRANK_CAL_ID,
    FEIERTAG_CAL_ID,
    GEBURTSTAGE_CAL_ID,
]);

// --- API helpers ---
async function api(path, options = {}) {
    const headers = { 'Content-Type': 'application/json', ...options.headers };
    if (token) headers['Authorization'] = `Bearer ${token}`;
    const res = await fetch(path, { ...options, headers });
    if (res.status === 401) {
        token = '';
        localStorage.removeItem('kalender_token');
        showLogin();
        throw new Error('Unauthorized');
    }
    return res;
}

async function apiJson(path, options = {}) {
    const res = await api(path, options);
    if (res.status === 204) return null;
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Fehler' }));
        throw new Error(err.detail || 'Fehler');
    }
    return res.json();
}

async function apiDelete(path) {
    const res = await api(path, { method: 'DELETE' });
    if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: 'Fehler' }));
        throw new Error(err.detail || 'Fehler');
    }
}

// --- Auth ---
function showLogin() {
    document.getElementById('login-screen').classList.remove('hidden');
    document.getElementById('app').classList.add('hidden');
}

function showApp() {
    document.getElementById('login-screen').classList.add('hidden');
    document.getElementById('app').classList.remove('hidden');
}

document.getElementById('login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const pw = document.getElementById('login-password').value;
    const errEl = document.getElementById('login-error');
    errEl.classList.add('hidden');
    try {
        const data = await apiJson('/api/auth/login', {
            method: 'POST',
            body: JSON.stringify({ password: pw }),
        });
        token = data.access_token;
        localStorage.setItem('kalender_token', token);
        document.getElementById('login-password').value = '';
        await initApp();
    } catch (err) {
        errEl.textContent = 'Falsches Passwort';
        errEl.classList.remove('hidden');
    }
});

document.getElementById('btn-logout').addEventListener('click', async () => {
    await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
    token = '';
    localStorage.removeItem('kalender_token');
    showLogin();
});

// --- Password Change ---
document.getElementById('btn-change-password').addEventListener('click', () => {
    document.getElementById('pw-current').value = '';
    document.getElementById('pw-new').value = '';
    document.getElementById('pw-confirm').value = '';
    document.getElementById('pw-error').classList.add('hidden');
    document.getElementById('pw-success').classList.add('hidden');
    openModal('password-modal');
});

document.getElementById('password-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const errEl = document.getElementById('pw-error');
    const successEl = document.getElementById('pw-success');
    errEl.classList.add('hidden');
    successEl.classList.add('hidden');

    const current = document.getElementById('pw-current').value;
    const newPw = document.getElementById('pw-new').value;
    const confirm = document.getElementById('pw-confirm').value;

    if (newPw !== confirm) {
        errEl.textContent = 'Neue Passwoerter stimmen nicht ueberein';
        errEl.classList.remove('hidden');
        return;
    }

    try {
        await apiJson('/api/auth/change-password', {
            method: 'POST',
            body: JSON.stringify({ current_password: current, new_password: newPw }),
        });
        successEl.textContent = 'Passwort geaendert';
        successEl.classList.remove('hidden');
        document.getElementById('pw-current').value = '';
        document.getElementById('pw-new').value = '';
        document.getElementById('pw-confirm').value = '';
        setTimeout(() => closeModal('password-modal'), 1500);
    } catch (err) {
        errEl.textContent = err.message || 'Fehler beim Aendern';
        errEl.classList.remove('hidden');
    }
});

// --- Konto & Datenschutz (DSGVO) ---
const ACCOUNT_LABELS = {
    calendars: 'Kalender', events: 'Termine', contacts: 'Kontakte',
    projects: 'Projekte', habits: 'Habits', habit_sessions: 'Habit-Sitzungen',
    todos: 'Aufgaben', todo_completions: 'Aufgaben-Erledigungen',
    goals: 'Tagesziele', reviews: 'Tagesreviews', daily_load: 'Tages-Last',
};

async function loadAccountSummary() {
    const el = document.getElementById('account-summary');
    if (!el) return;
    try {
        const s = await apiJson('/api/account/summary');
        const rows = Object.entries(s)
            .filter(([, n]) => n > 0)
            .map(([k, n]) => `<div class="account-summary-row"><span>${ACCOUNT_LABELS[k] || k}</span><span>${n}</span></div>`);
        el.innerHTML = rows.length ? rows.join('') : '<div class="widget-empty">Keine Daten gespeichert</div>';
    } catch {
        el.textContent = 'Uebersicht konnte nicht geladen werden.';
    }
}

const btnAccount = document.getElementById('btn-account');
if (btnAccount) {
    btnAccount.addEventListener('click', () => {
        const c = document.getElementById('account-delete-confirm');
        if (c) c.value = '';
        const msg = document.getElementById('account-delete-msg');
        if (msg) msg.classList.add('hidden');
        openModal('account-modal');
        loadAccountSummary();
    });
}

const btnAccountExport = document.getElementById('btn-account-export');
if (btnAccountExport) {
    btnAccountExport.addEventListener('click', async () => {
        btnAccountExport.disabled = true;
        try {
            const resp = await api('/api/account/export');
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            const today = new Date().toISOString().slice(0, 10);
            a.href = url;
            a.download = `saganta-kalender-export-${today}.json`;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch {
            if (typeof showToast === 'function') showToast('Export fehlgeschlagen');
        } finally {
            btnAccountExport.disabled = false;
        }
    });
}

const btnAccountDelete = document.getElementById('btn-account-delete');
if (btnAccountDelete) {
    btnAccountDelete.addEventListener('click', async () => {
        const input = document.getElementById('account-delete-confirm');
        const msg = document.getElementById('account-delete-msg');
        const phrase = (input.value || '').trim().toUpperCase();
        if (phrase !== 'KONTO LOESCHEN') {
            msg.textContent = 'Bitte genau "KONTO LOESCHEN" eingeben.';
            msg.classList.remove('hidden');
            return;
        }
        if (typeof window.confirm === 'function' && !window.confirm('Wirklich alle Daten unwiderruflich loeschen?')) return;
        btnAccountDelete.disabled = true;
        try {
            await apiJson('/api/account/delete', {
                method: 'POST',
                body: JSON.stringify({ confirm: 'KONTO LOESCHEN' }),
            });
            // Nach Löschung abmelden
            await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
            token = '';
            localStorage.removeItem('kalender_token');
            closeModal('account-modal');
            showLogin();
        } catch (e) {
            msg.textContent = 'Loeschung fehlgeschlagen.';
            msg.classList.remove('hidden');
            btnAccountDelete.disabled = false;
        }
    });
}

// --- Sidebar Toggle ---
function isMobile() {
    return window.matchMedia('(max-width: 768px)').matches;
}

function openSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (isMobile()) {
        sidebar.classList.add('open');
        document.getElementById('sidebar-overlay').classList.remove('hidden');
    } else {
        sidebar.classList.remove('collapsed');
        localStorage.setItem('kalender_sidebar', 'open');
    }
}

function closeSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (isMobile()) {
        sidebar.classList.remove('open');
        document.getElementById('sidebar-overlay').classList.add('hidden');
    } else {
        sidebar.classList.add('collapsed');
        localStorage.setItem('kalender_sidebar', 'collapsed');
    }
}

function toggleSidebar() {
    const sidebar = document.getElementById('sidebar');
    if (isMobile()) {
        sidebar.classList.contains('open') ? closeSidebar() : openSidebar();
    } else {
        sidebar.classList.contains('collapsed') ? openSidebar() : closeSidebar();
    }
}

// Restore desktop sidebar state
(function() {
    const saved = localStorage.getItem('kalender_sidebar');
    if (saved === 'collapsed' && !isMobile()) {
        document.getElementById('sidebar').classList.add('collapsed');
    }
})();

document.getElementById('btn-sidebar-toggle').addEventListener('click', toggleSidebar);
document.getElementById('sidebar-overlay').addEventListener('click', closeSidebar);

// --- Swipe Gestures ---
(function() {
    let touchStartX = 0;
    let touchStartY = 0;
    let tracking = false;

    function isSidebarOpen() {
        const sidebar = document.getElementById('sidebar');
        return isMobile() ? sidebar.classList.contains('open') : !sidebar.classList.contains('collapsed');
    }

    document.addEventListener('touchstart', function(e) {
        touchStartX = e.touches[0].clientX;
        touchStartY = e.touches[0].clientY;
        tracking = true;
    }, { passive: true });

    document.addEventListener('touchend', function(e) {
        if (!tracking) return;
        tracking = false;
        const touchEndX = e.changedTouches[0].clientX;
        const touchEndY = e.changedTouches[0].clientY;
        const dx = touchEndX - touchStartX;
        const dy = touchEndY - touchStartY;
        const absDx = Math.abs(dx);
        const absDy = Math.abs(dy);

        // Sidebar swipe from left edge
        if (touchStartX < 40 && dx > 60 && absDx > absDy && !isSidebarOpen()) {
            openSidebar();
            return;
        }
        if (isSidebarOpen() && dx < -60 && absDx > absDy) {
            closeSidebar();
            return;
        }

        // Only handle view swipes if not in a modal and not sidebar
        if (document.querySelector('.modal:not(.hidden)')) return;
        if (isSidebarOpen()) return;

        if (currentView === 'week') {
            // Vertical swipe up -> switch to year view
            if (dy < -80 && absDy > absDx) {
                switchView('year');
                return;
            }
            // Horizontal swipe -> change week
            if (absDx > 60 && absDx > absDy) {
                if (dx < -60) navigateWeek(1);
                else if (dx > 60) navigateWeek(-1);
                return;
            }
        } else if (currentView === 'year') {
            // Horizontal swipe in year view -> switch to week view
            if (absDx > 60 && absDx > absDy && touchStartX >= 40) {
                switchView('week');
                return;
            }
        }
    }, { passive: true });
})();

// --- Pinch-zoom prevention ---
document.addEventListener('touchmove', function(e) {
    if (e.touches.length > 1) {
        e.preventDefault();
    }
}, { passive: false });

// --- Init ---
async function initApp() {
    showApp();
    // Was diese Instanz an Orten/Karte/Wegzeit kann. Bewusst ohne await auf das
    // Ergebnis zu warten: die Antwort wird erst gebraucht, wenn jemand einen
    // Kontakt oeffnet, und der Start soll daran nicht haengen.
    ladeOrtKonfig();
    // Trigger auto-scheduling for current + next 3 weeks
    await apiJson('/api/habits/schedule-ahead', { method: 'POST' }).catch(() => {});
    await loadCalendars();
    await loadFocusStrip();
    await loadDashboardWidget();
    await loadTodosWidget();
    await loadHabitsWidget();
    initCustomCalendar();
    initWeekView();
    applyViewState();
    setViewToggleLabel();
    initNotifications();
    startNotificationPolling();

    // Auto-refresh every 5 minutes for wall-display / kiosk use
    setInterval(() => {
        refreshCalendar();
        loadFocusStrip();
        loadDashboardWidget();
        loadTodosWidget();
        loadHabitsWidget();
        if (currentView === 'week') refreshWeekView();
    }, 5 * 60 * 1000);
}

function formatRelativeDashboardDay(value) {
    if (!value) return "";
    const dt = new Date(value);
    const dateStr = formatDate(dt);
    const today = new Date();
    const todayStr = formatDate(today);
    const tomorrow = new Date(today.getTime() + 86400000);
    const tomorrowStr = formatDate(tomorrow);
    if (dateStr === todayStr) return "Heute";
    if (dateStr === tomorrowStr) return "Morgen";
    return dt.toLocaleDateString("de-DE", { weekday: "short", day: "2-digit", month: "2-digit" });
}

function formatDashboardEventSummary(event) {
    if (!event) return "Keine offenen Termine";
    const dayLabel = formatRelativeDashboardDay(event.start);
    if (event.all_day) return `${dayLabel} · ${escapeHtml(event.title)}`;
    const timeLabel = new Date(event.start).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" });
    const prefix = dayLabel === "Heute" ? timeLabel : `${dayLabel}, ${timeLabel}`;
    return `${prefix} · ${escapeHtml(event.title)}`;
}

async function loadFocusStrip() {
    const container = document.getElementById("focus-strip");
    if (!container) return;
    try {
        const [dashboard, habitSessions] = await Promise.all([
            apiJson("/api/dashboard"),
            apiJson("/api/habits/sessions/today").catch(() => []),
        ]);

        const todoStats = dashboard.todos || {};
        const nextEvent = dashboard.next_event || null;
        const nextHabit = habitSessions.find(s => s.status === "pending" || s.status === "accepted") || null;

        const nextEventText = formatDashboardEventSummary(nextEvent);
        const nextHabitText = nextHabit
            ? `${new Date(nextHabit.start).toLocaleTimeString("de-DE", { hour: "2-digit", minute: "2-digit" })} · ${escapeHtml(nextHabit.habit_name || "Habit")}`
            : "Keine Session offen";
        const tomorrowText = dashboard.tomorrow_type
            ? `${escapeHtml(dashboard.tomorrow_weekday)} · ${escapeHtml(dashboard.tomorrow_type)}`
            : "Morgen noch frei";

        const totalOpen = todoStats.total_open || 0;
        const overdueCount = todoStats.overdue_count || 0;
        const importantCount = todoStats.important_count || 0;
        const todayCount = todoStats.today_count || 0;

        // Tagesfokus: Ziel schlaegt Habit (Ziel = Outcome des Tages).
        const topGoal = (dashboard.goals || []).find(g => g.status === 'planned' || g.status === 'active') || null;
        let focusLabel = 'Fokus';
        let focusValue;
        let focusMeta;
        if (topGoal) {
            const blk = (topGoal.scheduled_start && topGoal.scheduled_end)
                ? `${new Date(topGoal.scheduled_start).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}–${new Date(topGoal.scheduled_end).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })} · `
                : '';
            focusValue = `${topGoal.priority} · ${escapeHtml(topGoal.title)}`;
            focusMeta = `${blk}${nextHabit ? 'danach ' + nextHabitText : 'Tagesfokus'}`;
        } else {
            focusValue = nextHabit ? 'Habit aktiv' : 'Morgen';
            focusMeta = nextHabit ? nextHabitText : tomorrowText;
        }

        container.innerHTML = `
            <div class="focus-card">
                <div class="focus-label">Heute</div>
                <div class="focus-value">${escapeHtml(dashboard.weekday)}</div>
                <div class="focus-meta">${escapeHtml(dashboard.day_type)}</div>
            </div>
            <div class="focus-card">
                <div class="focus-label">Naechster Termin</div>
                <div class="focus-value">${nextEvent ? (nextEvent.is_now ? "Jetzt" : "Im Blick") : "Leer"}</div>
                <div class="focus-meta">${nextEventText}</div>
            </div>
            <div class="focus-card">
                <div class="focus-label">Erinnerungen</div>
                <div class="focus-value">${totalOpen} offen</div>
                <div class="focus-list">
                    <div class="focus-item"><span>Ueberfaellig</span><strong>${overdueCount}</strong></div>
                    <div class="focus-item"><span>Heute</span><strong>${todayCount}</strong></div>
                    <div class="focus-item"><span>Wichtig</span><strong>${importantCount}</strong></div>
                </div>
            </div>
            <div class="focus-card${topGoal ? ' focus-card-goal' : ''}">
                <div class="focus-label">${focusLabel}</div>
                <div class="focus-value">${focusValue}</div>
                <div class="focus-meta">${focusMeta}</div>
            </div>
        `;
    } catch {
        container.innerHTML = "";
    }
}

// --- Dashboard Widget ---
async function loadDashboardWidget() {
    const container = document.getElementById('dashboard-widget');
    try {
        const data = await apiJson('/api/dashboard');
        let html = '';

        // Today card
        const dateObj = new Date(data.today + 'T00:00:00');
        const dayNum = dateObj.getDate();
        const monthNames = ['Januar', 'Februar', 'Maerz', 'April', 'Mai', 'Juni', 'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'];
        const dateStr = `${dayNum}. ${monthNames[dateObj.getMonth()]} ${dateObj.getFullYear()}`;

        html += `<div class="widget-card">
            <div class="widget-today">
                <div>
                    <div class="widget-today-date">${dateStr}</div>
                    <div class="widget-today-weekday">${escapeHtml(data.weekday)}</div>
                </div>
                <span class="widget-badge badge-${data.day_type}">${escapeHtml(data.day_type)}</span>
            </div>
        </div>`;

        // Tomorrow preview
        if (data.tomorrow_type) {
            html += `<div class="widget-card widget-tomorrow">
                <div class="widget-tomorrow-row">
                    <span class="widget-section-title">Morgen</span>
                    <span class="widget-badge badge-${data.tomorrow_type}">${escapeHtml(data.tomorrow_type)}</span>
                </div>
            </div>`;
        }

        // Sekretär: Konflikte + Auto-Planung
        html += renderSecretaryCard(data);

        // Tagesziele (Fokus/Outcome, kein Todo)
        html += renderGoalsCard(data);

        // Today's events
        html += `<div class="widget-card">
            <div class="widget-section-title">Heute</div>`;
        if (data.termine.length === 0) {
            html += `<div class="widget-empty">Keine Termine</div>`;
        } else {
            for (const t of data.termine) {
                const timeStr = t.all_day ? 'Ganztaegig' : new Date(t.start).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
                html += `<div class="widget-event-item">
                    <span class="widget-event-dot" style="background:${t.color}"></span>
                    <span class="widget-event-title">${escapeHtml(t.title)}</span>
                    <span class="widget-event-time">${timeStr}</span>
                </div>`;
            }
        }
        html += `</div>`;

        // Birthdays
        if (data.birthdays.length > 0) {
            html += `<div class="widget-card">
                <div class="widget-section-title">Geburtstage</div>`;
            for (const b of data.birthdays) {
                const daysText = b.days_until === 0 ? 'Heute!' : (b.days_until === 1 ? 'Morgen' : `in ${b.days_until} Tagen`);
                html += `<div class="widget-birthday-item">
                    <div class="widget-birthday-name">
                        <span class="widget-birthday-icon">&#127874;</span>
                        <span>${escapeHtml(b.name)}</span>
                        <span class="widget-birthday-age">(${b.age})</span>
                    </div>
                    <span class="widget-birthday-days">${daysText}</span>
                </div>`;
            }
            html += `</div>`;
        }

        // Next Feiertag
        if (data.next_feiertag) {
            const ft = data.next_feiertag;
            const daysText = ft.days_until === 0 ? 'Heute!' : (ft.days_until === 1 ? 'Morgen' : `in ${ft.days_until} Tagen`);
            html += `<div class="widget-card">
                <div class="widget-section-title">Naechster Feiertag</div>
                <div class="widget-feiertag-item">
                    <span class="widget-feiertag-name">${escapeHtml(ft.name)}</span>
                    <span class="widget-feiertag-days">${daysText}</span>
                </div>
            </div>`;
        }

        container.innerHTML = html;
        wireSecretaryCard(container);
        wireGoalsCard(container);
    } catch {
        container.innerHTML = '';
    }
}

// --- Sekretär: Konflikte + verbindliche Auto-Planung ---
const CONFLICT_ICON = { doppelbuchung: '⚠', ueberfaellig: '⏰', ueberlast: '\u{1F4CB}' };

function renderSecretaryCard(data) {
    const sec = data.secretary || {};
    const conflicts = sec.conflicts || [];
    let html = `<div class="widget-card widget-secretary">
        <div class="widget-section-title widget-secretary-head">
            <span>Sekret&auml;r</span>
            <button type="button" class="secretary-plan-btn" title="Offene Aufgaben verbindlich in freie Zeitfenster planen">Tag planen</button>
        </div>
        <form class="capture-form" autocomplete="off">
            <input type="text" class="capture-input" maxlength="1000"
                placeholder="Schnell erfassen: z.B. Zahnarzt morgen 15 Uhr" />
        </form>
        <div class="capture-preview" hidden></div>`;
    if (conflicts.length === 0) {
        html += `<div class="widget-empty secretary-clear">Keine Konflikte. Alles im Griff.</div>`;
    } else {
        for (const c of conflicts) {
            const icon = CONFLICT_ICON[c.type] || '⚠';
            html += `<div class="secretary-conflict sev-${c.severity || 'mittel'}">
                <span class="secretary-conflict-icon">${icon}</span>
                <span class="secretary-conflict-msg">${escapeHtml(c.message)}</span>
            </div>`;
        }
    }
    html += `<div class="secretary-plan-result" hidden></div></div>`;
    return html;
}

const CAPTURE_TYPE_LABEL = { event: 'Termin', todo: 'Aufgabe' };

function _fmtCaptureDate(iso) {
    if (!iso) return '';
    const d = new Date(iso + 'T00:00:00');
    return d.toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit' });
}

function wireCaptureForm(card) {
    const form = card.querySelector('.capture-form');
    const input = card.querySelector('.capture-input');
    const preview = card.querySelector('.capture-preview');
    if (!form || !input || !preview) return;
    let current = null;

    const renderPreview = (c) => {
        current = c;
        const parts = [];
        parts.push(`<span class="capture-type capture-type-${c.type}">${CAPTURE_TYPE_LABEL[c.type] || c.type}</span>`);
        parts.push(`<span class="capture-title">${escapeHtml(c.title)}</span>`);
        const meta = [];
        if (c.date) meta.push(_fmtCaptureDate(c.date));
        if (c.start_time) meta.push(c.start_time + (c.end_time ? '–' + c.end_time : ''));
        if (meta.length) parts.push(`<span class="capture-meta">${escapeHtml(meta.join(' · '))}</span>`);
        preview.innerHTML = `<div class="capture-suggestion">${parts.join(' ')}</div>
            <div class="capture-actions">
                <button type="button" class="capture-commit">Anlegen</button>
                <button type="button" class="capture-cancel">Verwerfen</button>
            </div>`;
        preview.hidden = false;
        preview.querySelector('.capture-commit').addEventListener('click', doCommit);
        preview.querySelector('.capture-cancel').addEventListener('click', () => {
            preview.hidden = true; current = null;
        });
    };

    const doCommit = async () => {
        if (!current) return;
        const btn = preview.querySelector('.capture-commit');
        if (btn) { btn.disabled = true; btn.textContent = 'Lege an…'; }
        try {
            await apiJson('/api/capture/commit', {
                method: 'POST',
                body: JSON.stringify({
                    type: current.type, title: current.title, date: current.date,
                    start_time: current.start_time, end_time: current.end_time,
                }),
            });
            input.value = ''; preview.hidden = true; current = null;
            if (typeof showToast === 'function') showToast('Angelegt');
            if (typeof loadFocusStrip === 'function') loadFocusStrip();
            if (typeof refreshCalendar === 'function') refreshCalendar();
            await loadDashboardWidget();
        } catch (e) {
            if (typeof showToast === 'function') showToast('Konnte nicht angelegt werden');
            if (btn) { btn.disabled = false; btn.textContent = 'Anlegen'; }
        }
    };

    form.addEventListener('submit', async (ev) => {
        ev.preventDefault();
        const text = (input.value || '').trim();
        if (!text) return;
        try {
            const c = await apiJson('/api/capture', { method: 'POST', body: JSON.stringify({ text }) });
            renderPreview(c);
        } catch (e) {
            if (typeof showToast === 'function') showToast('Konnte nicht verstanden werden');
        }
    });
}

function wireSecretaryCard(container) {
    const card = container.querySelector('.widget-secretary');
    if (!card) return;
    wireCaptureForm(card);
    const btn = card.querySelector('.secretary-plan-btn');
    const result = card.querySelector('.secretary-plan-result');
    if (!btn) return;
    btn.addEventListener('click', async () => {
        btn.disabled = true;
        const prev = btn.textContent;
        btn.textContent = 'Plane…';
        try {
            const r = await apiJson('/api/secretary/plan-day', { method: 'POST' });
            if (result) {
                const n = r.planned_count || 0;
                result.hidden = false;
                result.textContent = n === 0
                    ? 'Nichts einzuplanen, keine offenen Pool-Aufgaben oder kein freier Slot.'
                    : `${n} Aufgabe(n) verbindlich eingeplant. R&uuml;ckg&auml;ngig per Aufgaben-Ansicht.`;
                result.innerHTML = result.textContent;
            }
            if (typeof loadFocusStrip === 'function') loadFocusStrip();
            if (typeof refreshCalendar === 'function') refreshCalendar();
            await loadDashboardWidget();
        } catch (e) {
            if (typeof showToast === 'function') showToast('Tagesplanung fehlgeschlagen');
        } finally {
            btn.disabled = false;
            btn.textContent = prev;
        }
    });
}

// --- Tagesziele (Fokus/Outcome, kein Todo) ---
const GOAL_PRIO_LABEL = { A: 'Fokus', B: 'Nebenziel', C: 'Optional' };
const GOAL_STATUS_LABEL = {
    planned: 'geplant', active: 'aktiv', achieved: 'erreicht',
    partial: 'teilweise', abandoned: 'aufgegeben', missed: 'verfehlt',
};

function _fmtGoalBlock(g) {
    if (!g.scheduled_start || !g.scheduled_end) return '';
    const opts = { hour: '2-digit', minute: '2-digit' };
    const s = new Date(g.scheduled_start).toLocaleTimeString('de-DE', opts);
    const e = new Date(g.scheduled_end).toLocaleTimeString('de-DE', opts);
    return `${s}–${e}`;
}

function renderGoalsCard(data) {
    const goals = data.goals || [];
    let html = `<div class="widget-card widget-goals">
        <div class="widget-section-title widget-goals-head">
            <span>Tagesziele</span>
            <button type="button" class="goal-add-btn" title="Ziel hinzufuegen">+ Ziel</button>
        </div>
        <form class="goal-add-form" hidden>
            <input type="text" class="goal-add-title" maxlength="300" placeholder="Tagesfokus / gewuenschtes Ergebnis" />
            <div class="goal-add-row">
                <select class="goal-add-prio">
                    <option value="A">A · Fokus</option>
                    <option value="B" selected>B · Nebenziel</option>
                    <option value="C">C · Optional</option>
                </select>
                <input type="text" class="goal-add-cat" maxlength="50" placeholder="Kategorie (opt.)" />
            </div>
            <div class="goal-add-row">
                <input type="time" class="goal-add-start" title="Zeitblock von (opt.)" />
                <input type="time" class="goal-add-end" title="Zeitblock bis (opt.)" />
                <button type="submit" class="goal-add-save">Anlegen</button>
            </div>
        </form>`;
    if (goals.length === 0) {
        html += `<div class="widget-empty">Kein Ziel fuer heute</div>`;
    } else {
        for (const g of goals) {
            const abandoned = g.status === 'abandoned' || g.status === 'missed';
            const block = _fmtGoalBlock(g);
            html += `<div class="goal-item goal-prio-${g.priority} ${abandoned ? 'goal-inactive' : ''}" data-goal-id="${g.id}">
                <div class="goal-item-main">
                    <span class="goal-prio-badge">${g.priority}</span>
                    <span class="goal-title">${escapeHtml(g.title)}</span>
                </div>
                <div class="goal-item-meta">
                    <span class="goal-status goal-status-${g.status}">${GOAL_STATUS_LABEL[g.status] || g.status}</span>
                    ${block ? `<span class="goal-block">${block}</span>` : ''}
                    ${!abandoned ? `<button type="button" class="goal-abandon-btn" data-goal-id="${g.id}" title="Ziel aufgeben">aufgeben</button>` : ''}
                </div>
                ${g.abandoned_reason ? `<div class="goal-reason">${escapeHtml(g.abandoned_reason)}</div>` : ''}
            </div>`;
        }
    }
    html += `</div>`;
    return html;
}

function wireGoalsCard(container) {
    const card = container.querySelector('.widget-goals');
    if (!card) return;
    const addBtn = card.querySelector('.goal-add-btn');
    const form = card.querySelector('.goal-add-form');
    const titleInput = card.querySelector('.goal-add-title');
    if (addBtn && form) {
        addBtn.addEventListener('click', () => {
            form.hidden = !form.hidden;
            if (!form.hidden && titleInput) titleInput.focus();
        });
        form.addEventListener('submit', async (ev) => {
            ev.preventDefault();
            const title = (titleInput.value || '').trim();
            if (!title) return;
            const priority = card.querySelector('.goal-add-prio').value;
            const category = (card.querySelector('.goal-add-cat').value || '').trim() || null;
            const start = card.querySelector('.goal-add-start').value;
            const end = card.querySelector('.goal-add-end').value;
            const today = new Date();
            const dateStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
            const payload = { title, date: dateStr, priority, category };
            // optionaler Zeitblock (naiv = Europe/Berlin serverseitig)
            if (start && end && start < end) {
                payload.scheduled_start = `${dateStr}T${start}:00`;
                payload.scheduled_end = `${dateStr}T${end}:00`;
            }
            try {
                await apiJson('/api/goals', {
                    method: 'POST',
                    body: JSON.stringify(payload),
                });
                await loadDashboardWidget();
                if (typeof loadFocusStrip === 'function') loadFocusStrip();
                if (typeof refreshCalendar === 'function') refreshCalendar();
            } catch (e) {
                if (typeof showToast === 'function') showToast('Ziel konnte nicht angelegt werden');
            }
        });
    }
    card.querySelectorAll('.goal-abandon-btn').forEach((btn) => {
        btn.addEventListener('click', async () => {
            const id = btn.dataset.goalId;
            const reason = (typeof window.prompt === 'function') ? '' : '';
            try {
                await apiJson(`/api/goals/${id}/abandon`, {
                    method: 'POST',
                    body: JSON.stringify({ abandoned_reason: null }),
                });
                await loadDashboardWidget();
                if (typeof reloadCalendar === 'function') reloadCalendar();
            } catch (e) {
                if (typeof showToast === 'function') showToast('Ziel konnte nicht aufgegeben werden');
            }
        });
    });
}

// --- Todo display helpers ---
function formatDashboardTodoHint(todo) {
    const parts = [];
    if (todo.is_overdue) {
        const days = Math.abs(todo.days_until || 0);
        parts.push(days === 1 ? "Gestern" : `${days} Tage ueberfaellig`);
    } else if (todo.due_date) {
        parts.push(todo.due_time ? `Heute ${todo.due_time}` : "Heute");
    } else {
        parts.push("Ohne Datum");
    }
    if (todo.recurrence) parts.push("Wiederkehrend");
    return parts.join(" · ");
}

// --- Todos Sidebar Widget ---
async function loadTodosWidget() {
    const container = document.getElementById("todos-widget");
    if (!container) return;
    try {
        const dashboard = await apiJson("/api/dashboard");
        const todoData = dashboard.todos || {};
        const groups = [
            { label: "Ueberfaellig", items: todoData.overdue || [], cls: "todo-sidebar-overdue" },
            { label: "Heute", items: todoData.today || [], cls: "todo-sidebar-today" },
            { label: "Ohne Datum", items: todoData.floating || [], cls: "todo-sidebar-floating" },
        ];
        const total = groups.reduce((sum, group) => sum + group.items.length, 0);
        if (total === 0) {
            container.innerHTML = "";
            return;
        }

        let html = `<div class="widget-card">
            <div class="widget-section-title">To-Dos</div>
            <div class="todo-widget-summary">
                <span>${todoData.total_open || total} offen</span>
                <span>${todoData.overdue_count || 0} ueberfaellig</span>
                <span>${todoData.important_count || 0} wichtig</span>
            </div>`;

        for (const group of groups) {
            if (group.items.length === 0) continue;
            html += `<div class="todo-sidebar-group ${group.cls}">
                <div class="todo-sidebar-group-title">${group.label}</div>`;
            for (const t of group.items.slice(0, 6)) {
                const prioCls = `todo-prio-${t.priority}`;
                const recurIcon = t.recurrence ? `<span class="todo-recur-icon" title="Wiederkehrend">&#8635;</span>` : "";
                const hint = formatDashboardTodoHint(t);
                html += `<div class="todo-sidebar-item ${prioCls}" data-todo-id="${t.id}">
                    <input type="checkbox" class="todo-sidebar-check" data-todo-id="${t.id}">
                    <span class="todo-sidebar-title">${escapeHtml(t.title)}${recurIcon}</span>
                    <span class="todo-sidebar-time">${escapeHtml(hint)}</span>
                </div>`;
            }
            if (group.items.length > 6) {
                html += `<div class="todo-sidebar-more">+${group.items.length - 6} weitere</div>`;
            }
            html += "</div>";
        }
        html += "</div>";
        container.innerHTML = html;

        container.querySelectorAll(".todo-sidebar-check").forEach(cb => {
            cb.addEventListener("change", async () => {
                const todoId = cb.dataset.todoId;
                try {
                    if (cb.checked) {
                        await apiJson(`/api/todos/${todoId}/complete`, { method: "POST" });
                    } else {
                        await apiJson(`/api/todos/${todoId}/uncomplete`, { method: "POST" });
                    }
                    refreshCalendar();
                    loadFocusStrip();
                    loadDashboardWidget();
                    loadTodosWidget();
                    if (currentView === "week") refreshWeekView();
                } catch (err) {
                    showToast(err.message, "error");
                    cb.checked = !cb.checked;
                }
            });
        });
    } catch {
        container.innerHTML = "";
    }
}

// --- Calendars ---
async function loadCalendars() {
    calendars = await apiJson('/api/calendars');
}

// --- Custom Calendar Grid ---
function initCustomCalendar() {
    const today = new Date();
    const rollingStart = new Date(today);
    rollingStart.setDate(rollingStart.getDate() - 30);

    calendarStartDate = new Date(rollingStart.getFullYear(), rollingStart.getMonth(), 1);
    calendarEndDate = new Date(rollingStart.getFullYear(), rollingStart.getMonth() + 14, 0);

    refreshCalendar().then(() => waitForTodayThenScroll());
}

async function refreshCalendar() {
    await fetchCalendarEvents();
    renderCalendarGrid();
}

async function fetchCalendarEvents() {
    try {
        const startStr = calendarStartDate.toISOString();
        const endDate = new Date(calendarEndDate);
        const endStr = endDate.toISOString();
        const dueFrom = formatDate(calendarStartDate);
        const dueTo = formatDate(endDate);
        const params = new URLSearchParams({ start: startStr, end: endStr });

        const [events, habitSessionEvents, todoEvents, goalRows] = await Promise.all([
            apiJson(`/api/events?${params}`),
            apiJson(`/api/habits/sessions/calendar?${params}`).catch(() => []),
            apiJson(`/api/todos?due_from=${dueFrom}&due_to=${dueTo}&include_without_due_date=false`).catch(() => []),
            apiJson(`/api/goals`).catch(() => []),
        ]);

        // Tage mit aktiven Tageszielen → kompakter Punkt-Indikator im Kalender.
        goalDates = new Set(
            (goalRows || [])
                .filter(g => g.status !== 'abandoned' && g.status !== 'missed')
                .map(g => g.date)
        );

        const mapped = events.map(evt => {
            const cal = calendars.find(c => c.id === evt.calendar_id);
            const isDaytype = DAYTYPE_CAL_IDS.has(evt.calendar_id);
            const isFeiertag = evt.calendar_id === FEIERTAG_CAL_ID;
            const isBirthday = evt.calendar_id === GEBURTSTAGE_CAL_ID;
            const isBgDaytype = isDaytype && !isBirthday;

            const display = isBgDaytype ? 'background' : 'auto';
            const title = isFeiertag ? evt.title : (isBgDaytype ? '' : evt.title);

            let evtStart = evt.start;
            let evtEnd = evt.end;
            let allDay = evt.all_day;
            if (isBgDaytype && !evt.all_day) {
                evtStart = evt.start.slice(0, 10) + 'T00:00:00';
                evtEnd = evt.start.slice(0, 10) + 'T23:59:59';
                allDay = true;
            }

            return {
                id: evt.id,
                title: title,
                start: new Date(evtStart),
                end: evtEnd ? new Date(evtEnd) : null,
                allDay: allDay,
                display: display,
                backgroundColor: cal ? cal.color : '#3788d8',
                borderColor: cal ? cal.color : '#3788d8',
                editable: !isDaytype,
                extendedProps: {
                    calendar_id: evt.calendar_id,
                    description: evt.description,
                    location: evt.location,
                    lat: evt.lat,
                    lon: evt.lon,
                    travel_mode: evt.travel_mode,
                    travel_for_event_id: evt.travel_for_event_id,
                    recurrence_rule: evt.recurrence_rule,
                    recurrence_exdates: evt.recurrence_exdates,
                    reminder_minutes: evt.reminder_minutes,
                    is_recurring_instance: evt.is_recurring_instance,
                    series_id: evt.series_id || evt.id,
                    is_daytype: isDaytype,
                    is_birthday: isBirthday,
                    is_habit_session: false,
                    real_start: evt.start,
                    real_end: evt.end,
                    real_all_day: evt.all_day,
                },
            };
        });

        const habitMapped = habitSessionEvents.map(h => ({
            ...h,
            start: new Date(h.start),
            end: h.end ? new Date(h.end) : null,
        }));

        const todoMapped = todoEvents
            .filter(todo => todo.due_date)
            .map(todo => {
                const dueTime = todo.due_time || '09:00';
                const startIso = `${todo.due_date}T${dueTime}:00`;
                const endDateTime = new Date(startIso);
                endDateTime.setMinutes(endDateTime.getMinutes() + 30);
                return {
                    id: `${TODO_EVENT_PREFIX}${todo.id}`,
                    title: todo.title,
                    start: new Date(startIso),
                    end: endDateTime,
                    allDay: !todo.due_time,
                    display: 'auto',
                    backgroundColor: '#f59e0b',
                    borderColor: '#f59e0b',
                    editable: false,
                    extendedProps: {
                        is_todo: true,
                        todo_id: todo.id,
                        todo_priority: todo.priority,
                        todo_completed: todo.completed,
                        todo_recurrence: todo.recurrence,
                        description: todo.description,
                        location: null,
                        recurrence_rule: null,
                        is_daytype: false,
                        is_birthday: false,
                        is_habit_session: false,
                        real_start: startIso,
                        real_end: endDateTime.toISOString(),
                        real_all_day: !todo.due_time,
                    },
                };
            });

        cachedEvents = mapped.concat(habitMapped, todoMapped);
    } catch (err) {
        console.error('Failed to fetch calendar events:', err);
    }
}

function getEventById(id) {
    return cachedEvents.find(e => String(e.id) === String(id)) || null;
}

function findTodoById(todoId) {
    return allTodos.find(t => t.id === todoId) || weekTodos.find(t => t.id === todoId) || null;
}

async function openTodoFromCalendar(todoId) {
    let todo = findTodoById(todoId);
    if (!todo) {
        todo = await apiJson(`/api/todos/${todoId}`).catch(() => null);
    }
    if (todo) openEditTodo(todo);
}

function eventMatchesDate(evt, dateStr) {
    const evtDate = formatDate(evt.start);
    if (!evt.end) return evtDate === dateStr;
    const endDate = formatDate(new Date(evt.end.getTime() - 1));
    if (dateStr >= evtDate && dateStr <= endDate) return true;
    if (evt.allDay && evt.end) return dateStr >= evtDate && dateStr < formatDate(evt.end);
    return false;
}

function getEventsForDate(dateStr) {
    return cachedEvents.filter(evt => eventMatchesDate(evt, dateStr));
}

function renderCalendarGrid() {
    const container = document.getElementById('calendar');
    const today = new Date();
    const todayStr = formatDate(today);

    // Build month list
    const months = [];
    const cursor = new Date(calendarStartDate);
    while (cursor <= calendarEndDate) {
        months.push({ year: cursor.getFullYear(), month: cursor.getMonth() });
        cursor.setMonth(cursor.getMonth() + 1);
    }

    const MONTH_NAMES = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
                         'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'];
    const WD_SHORT = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];

    // Global sticky weekday header (rendered once)
    let html = '<div class="weekday-header-sticky"><div class="weekday-header-inner">';
    for (const wd of WD_SHORT) {
        html += `<span class="weekday-label">${wd}</span>`;
    }
    html += '</div></div>';

    for (const { year, month } of months) {
        const daysInMonth = new Date(year, month + 1, 0).getDate();
        const firstDayJS = new Date(year, month, 1).getDay(); // 0=Sun
        const gridColStart = firstDayJS === 0 ? 7 : firstDayJS; // Mo=1..So=7

        html += `<div class="month-section">`;
        html += `<div class="month-title">${MONTH_NAMES[month]} ${year}</div>`;
        html += `<div class="month-grid">`;

        for (let day = 1; day <= daysInMonth; day++) {
            const dateStr = `${year}-${String(month + 1).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
            const dateObj = new Date(year, month, day);
            const dow = dateObj.getDay();
            const isToday = dateStr === todayStr;
            const isWeekend = dow === 0 || dow === 6;

            const colStart = day === 1 && gridColStart > 1 ? ` style="grid-column-start:${gridColStart}"` : '';

            // Events for this day
            const dayEvents = getEventsForDate(dateStr);
            const bgEvents = dayEvents.filter(e => e.display === 'background');
            const fgEvents = dayEvents.filter(e => e.display !== 'background');

            // Day type detection
            let dayTypeLabel = '';
            let dayTypeCls = '';
            for (const bg of bgEvents) {
                const calId = bg.extendedProps.calendar_id;
                if (calId === FEIERTAG_CAL_ID) { dayTypeCls = ' has-feiertag'; dayTypeLabel = bg.title || 'Feiertag'; }
                else if (calId === KRANK_CAL_ID) { dayTypeCls = ' has-krank'; dayTypeLabel = 'Krank'; }
                else if (calId === URLAUB_CAL_ID) { dayTypeCls = ' has-urlaub'; dayTypeLabel = bg.title || 'Urlaub'; }
                else if (calId === SCHULE_CAL_ID) { dayTypeCls = ' has-schule'; dayTypeLabel = 'Schule'; }
                else if (calId === ARBEIT_CAL_ID) { dayTypeCls = ' has-arbeit'; dayTypeLabel = 'Arbeit'; }
            }

            const hasContent = fgEvents.length > 0 || dayTypeLabel || isToday;

            const hasGoal = goalDates && goalDates.has(dateStr);

            let cls = 'day-cell';
            if (isToday) cls += ' is-today';
            if (isWeekend) cls += ' is-weekend';
            if (hasContent || hasGoal) cls += ' has-content';
            if (hasGoal) cls += ' has-goal';
            cls += dayTypeCls;

            html += `<div class="${cls}" data-date="${dateStr}"${colStart}>`;
            html += `<div class="day-number">${day}${hasGoal ? '<span class="day-goal-dot" title="Tagesziel"></span>' : ''}</div>`;

            const maxShow = 2;
            const visible = fgEvents.slice(0, maxShow);
            const overflow = fgEvents.length - maxShow;

            if (visible.length > 0 || dayTypeLabel) {
                html += `<div class="day-events">`;
                if (dayTypeLabel) {
                    const labelType = dayTypeCls.replace(' has-', '');
                    html += `<span class="daytype-chip label-${labelType}">${escapeHtml(dayTypeLabel)}</span>`;
                }
                for (const evt of visible) {
                    const color = evt.backgroundColor || '#3788d8';
                    const isHabit = evt.extendedProps?.is_habit_session;
                    const isBday = evt.extendedProps?.is_birthday;
                    let pc = 'event-pill';
                    if (isBday) pc += ' pill-birthday';
                    if (isHabit) { pc += ' pill-habit'; if (evt.extendedProps?.status === 'pending') pc += ' pill-pending'; }
                    const sTypeShort = isHabit && evt.extendedProps?.session_type ? { input: 'I', review: 'R' }[evt.extendedProps.session_type] || '' : '';
                    const sTypeTag = sTypeShort ? `<span class="pill-session-type">[${sTypeShort}]</span> ` : '';
                    const pillLabel = isHabit ? `<span class="pill-habit-icon"></span>${sTypeTag}${escapeHtml(evt.title || '')}` : escapeHtml(evt.title || '');
                    html += `<div class="${pc}" style="border-left-color:${color}" data-event-id="${evt.id}">${pillLabel}</div>`;
                }
                if (overflow > 0) {
                    html += `<div class="event-overflow">+${overflow}</div>`;
                }
                html += `</div>`;
            }

            html += `</div>`;
        }

        html += `</div></div>`;
    }

    container.innerHTML = html;

    // Wire up day cell clicks (all real days, for adding events via popup)
    container.querySelectorAll('.day-cell').forEach(cell => {
        cell.addEventListener('click', (e) => {
            if (e.target.closest('.event-pill')) return;
            showDayDetail(cell.dataset.date, cell);
        });
    });

    // Wire up event pill clicks
    container.querySelectorAll('.event-pill').forEach(pill => {
        pill.addEventListener('click', (e) => {
            e.stopPropagation();
            const evtId = pill.dataset.eventId;
            const evt = getEventById(evtId);
            if (!evt) return;
            if (evt.extendedProps.is_daytype) return;
            if (evt.extendedProps.is_todo) {
                openTodoFromCalendar(evt.extendedProps.todo_id);
                return;
            }
            if (evt.extendedProps.is_habit_session) {
                handleHabitSessionClick(evt);
                return;
            }
            openEditEvent(evt);
        });
    });
}

function waitForTodayThenScroll() {
    let attempts = 0;
    const maxAttempts = 30;
    function tryScroll() {
        const todayCell = document.querySelector('.day-cell.is-today');
        if (todayCell && todayCell.getBoundingClientRect().height > 0) {
            scrollToToday(false);
            return;
        }
        if (++attempts < maxAttempts) {
            requestAnimationFrame(tryScroll);
        }
    }
    requestAnimationFrame(tryScroll);
}

function scrollToToday(smooth = true) {
    const todayCell = document.querySelector('.day-cell.is-today');
    if (!todayCell) return;
    const mainEl = document.querySelector('main');
    if (!mainEl) return;
    const cellRect = todayCell.getBoundingClientRect();
    const mainRect = mainEl.getBoundingClientRect();
    const scrollTarget = mainEl.scrollTop + cellRect.top - mainRect.top - mainRect.height / 2 + cellRect.height / 2;
    mainEl.scrollTo({ top: Math.max(0, scrollTarget), behavior: smooth ? 'smooth' : 'instant' });
}

// --- Day Detail Popup ---
const WEEKDAY_NAMES = ['Sonntag', 'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag'];
let hideDayDetailTimer = null;

function showDayDetail(dateStr, dayEl) {
    hideDayDetail();
    // hideDayDetail() sets a 150ms timer to add 'hidden': cancel it
    // so it doesn't hide the popup we're about to show
    if (hideDayDetailTimer) {
        clearTimeout(hideDayDetailTimer);
        hideDayDetailTimer = null;
    }
    const popup = document.getElementById('day-detail-popup');
    popup.dataset.date = dateStr;

    // Position popup near the clicked day cell
    const rect = dayEl.getBoundingClientRect();
    const mainEl = dayEl.closest('main');
    const mainRect = mainEl.getBoundingClientRect();
    const popupWidth = 300;
    let left = rect.left - mainRect.left + mainEl.scrollLeft + rect.width / 2 - popupWidth / 2;
    left = Math.max(8, Math.min(left, mainEl.scrollWidth - popupWidth - 8));
    popup.style.left = left + 'px';
    popup.style.top = (rect.bottom - mainRect.top + mainEl.scrollTop + 4) + 'px';

    // Date header
    const dateObj = new Date(dateStr + 'T00:00:00');
    const dayNum = dateObj.getDate();
    const monthNames = ['Jan', 'Feb', 'Mär', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
    document.getElementById('day-detail-date').textContent =
        `${WEEKDAY_NAMES[dateObj.getDay()]}, ${dayNum}. ${monthNames[dateObj.getMonth()]} ${dateObj.getFullYear()}`;

    // Collect day type and events from calendar
    const fcEvents = cachedEvents;
    let dayType = 'frei';
    let dayTypeTime = '';
    const dayEvents = [];

    for (const evt of fcEvents) {
        const evtDate = formatDate(evt.start);
        // Check overlap: use real dates for daytype, rendered dates for others
        const endDate = evt.end ? formatDate(new Date(evt.end.getTime() - 1)) : evtDate;
        const matches = dateStr >= evtDate && dateStr <= endDate;
        // Also check allDay end (exclusive in FC)
        const matchesAllDay = evt.allDay && evt.end && dateStr >= evtDate && dateStr < formatDate(evt.end);
        if (!matches && !matchesAllDay) continue;

        if (evt.extendedProps.is_daytype && !evt.extendedProps.is_birthday) {
            const calId = evt.extendedProps.calendar_id;
            if (calId === FEIERTAG_CAL_ID) dayType = 'feiertag';
            else if (calId === ARBEIT_CAL_ID) dayType = 'arbeit';
            else if (calId === SCHULE_CAL_ID) dayType = 'schule';
            else if (calId === URLAUB_CAL_ID) dayType = (evt.title || '').includes('1/2') ? 'urlaub (1/2)' : 'urlaub';
            else if (calId === KRANK_CAL_ID) dayType = 'krank';

            // Extract real times from extendedProps
            const realStart = evt.extendedProps.real_start;
            const realEnd = evt.extendedProps.real_end;
            if (realStart && realEnd && !evt.extendedProps.real_all_day) {
                const s = new Date(realStart);
                const e = new Date(realEnd);
                const sh = String(s.getHours()).padStart(2, '0');
                const sm = String(s.getMinutes()).padStart(2, '0');
                const eh = String(e.getHours()).padStart(2, '0');
                const em = String(e.getMinutes()).padStart(2, '0');
                dayTypeTime = `${sh}:${sm} - ${eh}:${em}`;
            }
        } else if (!evt.extendedProps.is_daytype) {
            dayEvents.push(evt);
        }
    }

    // Weekend check
    const dow = dateObj.getDay();
    if (dayType === 'frei' && (dow === 0 || dow === 6)) dayType = 'wochenende';

    // Badge + optional time
    const badge = document.getElementById('day-detail-badge');
    const badgeBase = dayType.replace(' (1/2)', '');
    badge.className = `widget-badge badge-${badgeBase}`;
    badge.textContent = dayTypeTime ? `${dayType} ${dayTypeTime}` : dayType;

    // Events list
    const eventsContainer = document.getElementById('day-detail-events');
    let eventsHtml = '';
    if (dayEvents.length === 0) {
        eventsHtml = '<div class="day-detail-empty">Keine Termine</div>';
    } else {
        eventsHtml = dayEvents.map(evt => {
            const cal = calendars.find(c => c.id === evt.extendedProps.calendar_id);
            const color = evt.extendedProps.is_todo ? '#f59e0b' : (cal ? cal.color : '#3788d8');
            let timeStr = evt.extendedProps.is_todo ? 'Erinnerung' : 'Ganztaegig';
            if (!evt.allDay) {
                const sh = String(evt.start.getHours()).padStart(2, '0');
                const sm = String(evt.start.getMinutes()).padStart(2, '0');
                const ed = evt.end || evt.start;
                const eh = String(ed.getHours()).padStart(2, '0');
                const em = String(ed.getMinutes()).padStart(2, '0');
                timeStr = `${sh}:${sm} - ${eh}:${em}`;
            }
            const loc = evt.extendedProps.location ? `<div class="day-detail-evt-loc">${escapeHtml(evt.extendedProps.location)}</div>` : '';
            const desc = evt.extendedProps.description ? `<div class="day-detail-evt-desc">${escapeHtml(evt.extendedProps.description)}</div>` : '';
            const reminderBadge = evt.extendedProps.is_todo ? '<span class="todo-recur-icon" title="Kalender-Erinnerung">&#128276;</span>' : '';
            return `<div class="day-detail-evt" data-id="${evt.id}">
                <div class="day-detail-evt-header">
                    <span class="day-detail-evt-dot" style="background:${color}"></span>
                    <span class="day-detail-evt-title">${reminderBadge}${escapeHtml(evt.title)}</span>
                    <span class="day-detail-evt-time">${timeStr}</span>
                </div>
                ${loc}${desc}
            </div>`;
        }).join('');
    }

    // Habit sessions for this day
    const habitEvts = fcEvents.filter(evt => {
        if (!evt.extendedProps.is_habit_session) return false;
        const evtDate = formatDate(evt.start);
        return evtDate === dateStr;
    });
    if (habitEvts.length > 0) {
        const now = new Date();
        eventsHtml += '<div class="day-detail-habit-section">';
        eventsHtml += '<div class="widget-section-title" style="margin-bottom:0.3rem">Habit Sessions</div>';
        const sessionTypeLabels2 = { input: 'Input', review: 'Review' };
        for (const hevt of habitEvts) {
            const color = hevt.backgroundColor || '#4a9eff';
            const sh = String(hevt.start.getHours()).padStart(2, '0');
            const sm = String(hevt.start.getMinutes()).padStart(2, '0');
            const ed = hevt.end || hevt.start;
            const eh = String(ed.getHours()).padStart(2, '0');
            const em = String(ed.getMinutes()).padStart(2, '0');
            const timeStr = `${sh}:${sm}-${eh}:${em}`;
            const status = hevt.extendedProps.status;
            const sessionId = hevt.extendedProps.session_id;
            const sType = hevt.extendedProps.session_type || 'standard';
            const typeBadge2 = sType !== 'standard' ? ` <span class="session-type-badge type-${sType}">${sessionTypeLabels2[sType] || sType}</span>` : '';
            let actionBtns = '';
            if (status === 'pending') {
                actionBtns = `<div class="habit-session-actions">
                    <button class="btn-start-early" data-session="${sessionId}" data-action="start_early" title="Jetzt starten">&#9654;</button>
                    <button class="btn-dismiss" data-session="${sessionId}" data-action="dismissed" title="Verschieben">&#10007;</button>
                </div>`;
            } else if (status === 'accepted') {
                actionBtns = `<div class="habit-session-actions">
                    <button class="btn-cancel" data-session="${sessionId}" data-action="cancelled" title="Abbrechen">&#9724;</button>
                </div>`;
            } else if (status === 'completed') {
                actionBtns = `<div class="habit-session-actions"><span class="habit-session-done" title="Erledigt">&#10003;</span></div>`;
            } else if (status === 'overridden') {
                actionBtns = `<div class="habit-session-actions"><span class="habit-session-overridden" title="Durch Tagesziel verdraengt">durch Ziel verdraengt</span></div>`;
            }
            eventsHtml += `<div class="day-detail-habit-session status-${status}" style="--habit-color:${color}">
                <div class="day-detail-habit-info">
                    <span class="day-detail-habit-dot" style="background:${color}"></span>
                    <span class="day-detail-habit-name">${escapeHtml(hevt.title)}${typeBadge2}</span>
                    <span class="day-detail-habit-time">${timeStr}</span>
                </div>
                ${actionBtns}
            </div>`;
        }
        eventsHtml += '</div>';
    }

    eventsContainer.innerHTML = eventsHtml;

    // Ziele + Pool-Vorschlaege asynchron nachladen (geordnete Tagesansicht)
    enrichDayDetail(dateStr, eventsContainer);

    // Click on event to edit
    eventsContainer.querySelectorAll('.day-detail-evt').forEach(el => {
        el.addEventListener('click', () => {
            const evtId = el.dataset.id;
            const fcEvt = getEventById(evtId);
            if (fcEvt) {
                hideDayDetail();
                if (fcEvt.extendedProps?.is_todo) {
                    openTodoFromCalendar(fcEvt.extendedProps.todo_id);
                    return;
                }
                openEditEvent(fcEvt);
            }
        });
    });

    // Habit session action buttons in day detail
    eventsContainer.querySelectorAll('.habit-session-actions button').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            const sessionId = btn.dataset.session;
            const action = btn.dataset.action;
            try {
                const result = await apiJson(`/api/habits/sessions/${sessionId}/action`, {
                    method: 'POST',
                    body: JSON.stringify({ action }),
                });
                hideDayDetail();
                refreshCalendar();
                loadFocusStrip();
                loadHabitsWidget();
                if (result && result.off_day_activated) {
                    showToast(result.message || 'Ruhetag aktiviert');
                } else {
                    const toasts = { accepted: 'Session gestartet', dismissed: 'Session verschoben', cancelled: 'Session abgebrochen, Rest verschoben', start_early: 'Session frueher gestartet' };
                    showToast(toasts[action] || 'Erledigt');
                }
            } catch (err) {
                showToast(err.message, 'error');
            }
        });
    });

    // Highlight currently active daytype button
    popup.querySelectorAll('.daytype-btn').forEach(btn => btn.classList.remove('active'));
    for (const evt of fcEvents) {
        if (evt.extendedProps.is_daytype && !evt.extendedProps.is_birthday) {
            const evtDate = formatDate(evt.start);
            if (evtDate === dateStr || (evt.allDay && evt.end && dateStr >= evtDate && dateStr < formatDate(evt.end))) {
                const calId = evt.extendedProps.calendar_id;
                if (calId === ARBEIT_CAL_ID) popup.querySelector('.daytype-arbeit')?.classList.add('active');
                if (calId === SCHULE_CAL_ID) popup.querySelector('.daytype-schule')?.classList.add('active');
                if (calId === URLAUB_CAL_ID) {
                    if ((evt.title || '').includes('1/2')) {
                        popup.querySelector('.daytype-urlaub-half')?.classList.add('active');
                    } else {
                        popup.querySelector('.daytype-urlaub')?.classList.add('active');
                    }
                }
                if (calId === KRANK_CAL_ID) popup.querySelector('.daytype-krank')?.classList.add('active');
                break;
            }
        }
    }

    popup.classList.remove('hidden');
    popup.style.display = '';
    requestAnimationFrame(() => {
        requestAnimationFrame(() => popup.classList.add('visible'));
    });

    setTimeout(() => {
        document.addEventListener('click', onOutsideClickDayDetail);
    }, 0);
}

// Ziele + Pool-Todo-Vorschlaege fuer einen Tag in das Tag-Detail einweben.
async function enrichDayDetail(dateStr, container) {
    let goals = [];
    let plan = { suggestions: [] };
    let review = null;
    try {
        [goals, plan, review] = await Promise.all([
            apiJson(`/api/goals?date=${dateStr}`).catch(() => []),
            apiJson(`/api/schedule/day?date=${dateStr}`).catch(() => ({ suggestions: [] })),
            apiJson(`/api/reviews/${dateStr}`).catch(() => null),
        ]);
    } catch { return; }
    // Popup koennte zwischenzeitlich geschlossen/neu gerendert sein.
    if (!container || !container.isConnected) return;

    // --- Tagesziele (oben, prominent) ---
    if (goals.length) {
        const block = document.createElement('div');
        block.className = 'day-detail-goals';
        let html = '<div class="widget-section-title" style="margin-bottom:0.3rem">Tagesziele</div>';
        for (const g of goals) {
            const abandoned = g.status === 'abandoned' || g.status === 'missed';
            const t = (g.scheduled_start && g.scheduled_end)
                ? `${new Date(g.scheduled_start).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}–${new Date(g.scheduled_end).toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' })}`
                : '';
            const actions = abandoned ? '' : `
                <div class="goal-detail-actions">
                    <button data-goal="${g.id}" data-status="active" title="Aktiv setzen">aktiv</button>
                    <button data-goal="${g.id}" data-status="achieved" title="Erreicht">erreicht</button>
                    <button data-goal="${g.id}" data-status="partial" title="Teilweise">teilweise</button>
                    <button data-goal="${g.id}" data-abandon="1" title="Ziel aufgeben">aufgeben</button>
                </div>`;
            html += `<div class="goal-item goal-prio-${g.priority} ${abandoned ? 'goal-inactive' : ''}">
                <div class="goal-item-main">
                    <span class="goal-prio-badge">${g.priority}</span>
                    <span class="goal-title">${escapeHtml(g.title)}</span>
                </div>
                <div class="goal-item-meta">
                    <span class="goal-status goal-status-${g.status}">${GOAL_STATUS_LABEL[g.status] || g.status}</span>
                    ${t ? `<span class="goal-block">${t}</span>` : ''}
                    ${(g.linked_todo_ids && g.linked_todo_ids.length) ? `<span class="goal-linked" title="verknuepfte To-Dos">&#128279; ${g.linked_todo_ids.length}</span>` : ''}
                </div>
                ${actions}
            </div>`;
        }
        block.innerHTML = html;
        container.prepend(block);

        block.querySelectorAll('.goal-detail-actions button').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const id = btn.dataset.goal;
                try {
                    if (btn.dataset.abandon) {
                        await apiJson(`/api/goals/${id}/abandon`, { method: 'POST', body: JSON.stringify({ abandoned_reason: null }) });
                        showToast('Ziel aufgegeben: Zeit wieder frei');
                    } else {
                        await apiJson(`/api/goals/${id}`, { method: 'PUT', body: JSON.stringify({ status: btn.dataset.status }) });
                        showToast('Ziel-Status aktualisiert');
                    }
                    refreshCalendar();
                    loadDashboardWidget();
                    const cell = document.querySelector(`.day-cell[data-date="${dateStr}"]`);
                    if (cell) showDayDetail(dateStr, cell);
                } catch (err) { showToast(err.message || 'Fehler', 'error'); }
            });
        });
    }

    // --- Vorgeschlagene Pool-Todos (in freie Slots) ---
    const sugg = (plan && plan.suggestions) || [];
    if (sugg.length) {
        const block = document.createElement('div');
        block.className = 'day-detail-pool';
        let html = '<div class="widget-section-title" style="margin:0.4rem 0 0.3rem">Vorgeschlagene To-Dos</div>';
        for (const s of sugg) {
            html += `<div class="pool-suggestion">
                <span class="pool-sugg-min">${s.minutes} min</span>
                <span class="pool-sugg-title">${escapeHtml(s.title)}</span>
            </div>`;
        }
        html += `<button class="pool-plan-btn" data-date="${dateStr}">In freie Slots einplanen</button>`;
        block.innerHTML = html;
        container.appendChild(block);

        block.querySelector('.pool-plan-btn').addEventListener('click', async (e) => {
            e.stopPropagation();
            try {
                await apiJson(`/api/schedule/auto-plan?date=${dateStr}&commit=true`, { method: 'POST' });
                showToast('To-Dos in freie Slots eingeplant');
                refreshCalendar();
                const cell = document.querySelector(`.day-cell[data-date="${dateStr}"]`);
                if (cell) showDayDetail(dateStr, cell);
            } catch (err) { showToast(err.message || 'Fehler', 'error'); }
        });
    }

    // --- Tagesreview (kompakt, einklappbar) ---
    const r = review || {};
    const hasReview = r && (r.what_went_well || r.blockers || r.carry_over_to_tomorrow || r.energy_level);
    const rblock = document.createElement('div');
    rblock.className = 'day-detail-review';
    const energies = ['low', 'medium', 'high'];
    const energyLabel = { low: 'niedrig', medium: 'mittel', high: 'hoch' };
    rblock.innerHTML = `
        <div class="widget-section-title review-head">
            <span>Tagesreview</span>
            <button type="button" class="review-toggle">${hasReview ? 'bearbeiten' : '+ Review'}</button>
        </div>
        <form class="review-form" hidden>
            <textarea class="review-good" rows="2" placeholder="Was lief gut?">${escapeHtml(r.what_went_well || '')}</textarea>
            <textarea class="review-block" rows="2" placeholder="Blocker?">${escapeHtml(r.blockers || '')}</textarea>
            <textarea class="review-carry" rows="2" placeholder="Mit nach morgen nehmen?">${escapeHtml(r.carry_over_to_tomorrow || '')}</textarea>
            <div class="review-energy">
                ${energies.map(en => `<label><input type="radio" name="review-energy-${dateStr}" value="${en}" ${r.energy_level === en ? 'checked' : ''}/> ${energyLabel[en]}</label>`).join('')}
            </div>
            <button type="submit" class="review-save">Speichern</button>
        </form>
        ${hasReview ? `<div class="review-summary">${escapeHtml((r.what_went_well || '').slice(0, 80))}${r.energy_level ? ` · Energie: ${energyLabel[r.energy_level]}` : ''}</div>` : ''}`;
    container.appendChild(rblock);

    const rtoggle = rblock.querySelector('.review-toggle');
    const rform = rblock.querySelector('.review-form');
    rtoggle.addEventListener('click', (e) => { e.stopPropagation(); rform.hidden = !rform.hidden; });
    rform.addEventListener('submit', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        const energyEl = rform.querySelector(`input[name="review-energy-${dateStr}"]:checked`);
        try {
            await apiJson(`/api/reviews/${dateStr}`, {
                method: 'PUT',
                body: JSON.stringify({
                    what_went_well: rform.querySelector('.review-good').value || null,
                    blockers: rform.querySelector('.review-block').value || null,
                    carry_over_to_tomorrow: rform.querySelector('.review-carry').value || null,
                    energy_level: energyEl ? energyEl.value : null,
                }),
            });
            showToast('Tagesreview gespeichert');
            const cell = document.querySelector(`.day-cell[data-date="${dateStr}"]`);
            if (cell) showDayDetail(dateStr, cell);
        } catch (err) { showToast(err.message || 'Fehler', 'error'); }
    });
}

function hideDayDetail() {
    const popup = document.getElementById('day-detail-popup');
    popup.classList.remove('visible');
    if (hideDayDetailTimer) clearTimeout(hideDayDetailTimer);
    hideDayDetailTimer = setTimeout(() => {
        popup.classList.add('hidden');
        hideDayDetailTimer = null;
    }, 150);
    document.removeEventListener('click', onOutsideClickDayDetail);
}

function onOutsideClickDayDetail(e) {
    const popup = document.getElementById('day-detail-popup');
    if (!popup.contains(e.target)) {
        hideDayDetail();
    }
}

document.querySelectorAll('.daytype-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
        const dateStr = document.getElementById('day-detail-popup').dataset.date;
        const type = btn.dataset.type;

        if (type === 'event') {
            hideDayDetail();
            openNewEvent(dateStr, dateStr, true);
            return;
        }

        const half = btn.dataset.half === 'true';
        const halfParam = half ? '&half=true' : '';

        try {
            await apiJson(`/api/day-type/set?date=${dateStr}&type=${type}${halfParam}`, { method: 'POST' });
            hideDayDetail();
            refreshCalendar();
            loadFocusStrip();
            loadDashboardWidget();
            loadHabitsWidget();
        } catch (err) {
            showToast(err.message, 'error');
        }
    });
});

// --- Recurrence helpers ---
const RECURRENCE_PRESETS = [
    'FREQ=DAILY',
    'FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR',
    'FREQ=WEEKLY',
    'FREQ=WEEKLY;INTERVAL=2',
    'FREQ=MONTHLY',
    'FREQ=YEARLY',
];

function setRecurrenceSelect(rule) {
    const sel = document.getElementById('evt-recurrence');
    const customGroup = document.getElementById('evt-recurrence-custom-group');
    const customInput = document.getElementById('evt-recurrence-custom');
    if (!rule) {
        sel.value = '';
        customGroup.classList.add('hidden');
        customInput.value = '';
    } else if (RECURRENCE_PRESETS.includes(rule)) {
        sel.value = rule;
        customGroup.classList.add('hidden');
        customInput.value = '';
    } else {
        sel.value = '__custom__';
        customGroup.classList.remove('hidden');
        customInput.value = rule;
    }
}

function getRecurrenceValue() {
    const sel = document.getElementById('evt-recurrence').value;
    if (sel === '__custom__') {
        return document.getElementById('evt-recurrence-custom').value.trim() || null;
    }
    return sel || null;
}

document.getElementById('evt-recurrence').addEventListener('change', function() {
    document.getElementById('evt-recurrence-custom-group').classList.toggle('hidden', this.value !== '__custom__');
});

// Liefert 'this' | 'following' | 'all' | null (abgebrochen) für Serientermine.
function askRecurrenceScope(actionVerb) {
    return new Promise((resolve) => {
        const modal = document.getElementById('recurrence-scope-modal');
        document.getElementById('recurrence-scope-text').textContent =
            `Dieser Termin gehoert zu einer Serie. Was moechtest du ${actionVerb}?`;
        const handler = (e) => {
            const btn = e.target.closest('[data-scope]');
            if (!btn) return;
            cleanup();
            const scope = btn.getAttribute('data-scope');
            resolve(scope || null);
        };
        function cleanup() {
            modal.removeEventListener('click', handler);
            closeModal('recurrence-scope-modal');
        }
        modal.addEventListener('click', handler);
        openModal('recurrence-scope-modal');
    });
}

// --- Event Modal ---
function openNewEvent(start, end, allDay) {
    document.getElementById('event-modal-title').textContent = 'Neues Event';
    document.getElementById('evt-id').value = '';
    document.getElementById('evt-title').value = '';
    document.getElementById('evt-description').value = '';
    setzeTerminOrt(null, null, null);
    document.getElementById('evt-allday').checked = allDay;
    setRecurrenceSelect('');
    document.getElementById('evt-reminder').value = '';
    eventModalContext = { seriesId: null, occDate: null, isInstance: false };

    // Date fields are always type="date"
    const startDate = start.slice(0, 10);
    document.getElementById('evt-start').value = startDate;
    document.getElementById('evt-end').value = (end || start).slice(0, 10);

    // Default times
    document.getElementById('evt-start-time').value = '09:00';
    document.getElementById('evt-end-time').value = '10:00';

    toggleTimeFields(allDay);
    document.getElementById('btn-delete-event').classList.add('hidden');
    openModal('event-modal');
}

// Kontext des aktuell bearbeiteten Events (für Serien-Operationen).
let eventModalContext = { seriesId: null, occDate: null, isInstance: false };

function openEditEvent(fcEvent) {
    document.getElementById('event-modal-title').textContent = 'Event bearbeiten';
    document.getElementById('evt-id').value = fcEvent.id;
    document.getElementById('evt-title').value = fcEvent.title;
    document.getElementById('evt-description').value = fcEvent.extendedProps.description || '';
    setzeTerminOrt(fcEvent.extendedProps.location,
                   fcEvent.extendedProps.lat, fcEvent.extendedProps.lon);
    document.getElementById('evt-allday').checked = fcEvent.allDay;
    setRecurrenceSelect(fcEvent.extendedProps.recurrence_rule || '');
    const rem = fcEvent.extendedProps.reminder_minutes;
    document.getElementById('evt-reminder').value = (rem === 0 || rem) ? String(rem) : '';
    eventModalContext = {
        seriesId: fcEvent.extendedProps.series_id || fcEvent.id,
        occDate: formatDate(fcEvent.start),
        isInstance: !!fcEvent.extendedProps.recurrence_rule,
    };

    document.getElementById('evt-start').value = formatDate(fcEvent.start);
    const endDate = fcEvent.end || fcEvent.start;
    document.getElementById('evt-end').value = formatDate(endDate);

    if (fcEvent.allDay) {
        document.getElementById('evt-start-time').value = '09:00';
        document.getElementById('evt-end-time').value = '10:00';
    } else {
        const sh = String(fcEvent.start.getHours()).padStart(2, '0');
        const sm = String(fcEvent.start.getMinutes()).padStart(2, '0');
        document.getElementById('evt-start-time').value = `${sh}:${sm}`;
        const ed = fcEvent.end || fcEvent.start;
        const eh = String(ed.getHours()).padStart(2, '0');
        const em = String(ed.getMinutes()).padStart(2, '0');
        document.getElementById('evt-end-time').value = `${eh}:${em}`;
    }

    toggleTimeFields(fcEvent.allDay);
    document.getElementById('btn-delete-event').classList.remove('hidden');
    openModal('event-modal');
}

document.getElementById('evt-allday').addEventListener('change', function() {
    toggleTimeFields(this.checked);
});

function toggleTimeFields(allDay) {
    document.getElementById('time-fields').classList.toggle('hidden', allDay);
}

document.getElementById('event-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('evt-id').value;
    const allDay = document.getElementById('evt-allday').checked;
    const startDate = document.getElementById('evt-start').value;
    const endDate = document.getElementById('evt-end').value;

    let startVal, endVal;
    if (allDay) {
        startVal = startDate + 'T00:00:00';
        endVal = endDate + 'T23:59:59';
    } else {
        const startTime = document.getElementById('evt-start-time').value || '09:00';
        const endTime = document.getElementById('evt-end-time').value || '10:00';
        startVal = startDate + 'T' + startTime + ':00';
        endVal = endDate + 'T' + endTime + ':00';
    }

    const recurrence = getRecurrenceValue();
    const remRaw = document.getElementById('evt-reminder').value;
    const reminder = remRaw === '' ? null : parseInt(remRaw, 10);

    const payload = {
        calendar_id: TERMINE_CAL_ID,
        title: document.getElementById('evt-title').value,
        description: document.getElementById('evt-description').value || null,
        location: document.getElementById('evt-location').value || null,
        start: startVal,
        end: endVal,
        all_day: allDay,
        recurrence_rule: recurrence,
        reminder_minutes: reminder,
        lat: terminKoordinaten.lat,
        lon: terminKoordinaten.lon,
        travel_mode: (terminKoordinaten.lat != null && ortKonfig.wegzeit)
            ? (document.getElementById('evt-travel-mode').value || null) : null,
    };

    try {
        if (id && eventModalContext.isInstance) {
            // Serientermin bearbeiten: Geltungsbereich erfragen.
            const scope = await askRecurrenceScope('aendern');
            if (!scope) return;
            const sid = eventModalContext.seriesId;
            if (scope === 'all') {
                // Serie als Ganzes: Anker-Datum/Zeit unangetastet lassen.
                const { start, end, ...rest } = payload;
                await apiJson(`/api/events/${sid}`, { method: 'PUT', body: JSON.stringify(rest) });
            } else if (scope === 'this') {
                await apiDelete(`/api/events/${sid}/instances/${eventModalContext.occDate}`);
                await apiJson('/api/events', { method: 'POST', body: JSON.stringify({ ...payload, recurrence_rule: null }) });
            } else if (scope === 'following') {
                await apiJson(`/api/events/${sid}/truncate?occ_date=${eventModalContext.occDate}`, { method: 'POST' });
                await apiJson('/api/events', { method: 'POST', body: JSON.stringify(payload) });
            }
        } else if (id) {
            await apiJson(`/api/events/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
        } else {
            await apiJson('/api/events', { method: 'POST', body: JSON.stringify(payload) });
        }
        closeModal('event-modal');
        refreshCalendar();
        loadFocusStrip();
        loadDashboardWidget();
        loadHabitsWidget();
        if (currentView === 'week') refreshWeekView();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

document.getElementById('btn-delete-event').addEventListener('click', async () => {
    const id = document.getElementById('evt-id').value;
    if (!id) return;
    try {
        if (eventModalContext.isInstance) {
            const scope = await askRecurrenceScope('loeschen');
            if (!scope) return;
            const sid = eventModalContext.seriesId;
            if (scope === 'all') {
                await apiDelete(`/api/events/${sid}`);
            } else if (scope === 'this') {
                await apiDelete(`/api/events/${sid}/instances/${eventModalContext.occDate}`);
            } else if (scope === 'following') {
                await apiJson(`/api/events/${sid}/truncate?occ_date=${eventModalContext.occDate}`, { method: 'POST' });
            }
        } else {
            if (!confirm('Event loeschen?')) return;
            await apiDelete(`/api/events/${id}`);
        }
        closeModal('event-modal');
        refreshCalendar();
        loadFocusStrip();
        loadDashboardWidget();
        loadHabitsWidget();
        if (currentView === 'week') refreshWeekView();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

// --- Contacts ---
document.getElementById('btn-contacts').addEventListener('click', openContactsList);

async function loadContacts() {
    contacts = await apiJson('/api/contacts');
}

async function openContactsList() {
    await loadContacts();
    renderContactsList();
    openModal('contacts-modal');
}

function renderContactsList() {
    const container = document.getElementById('contacts-list');
    if (contacts.length === 0) {
        container.innerHTML = '<div class="contacts-empty">Noch keine Kontakte vorhanden.</div>';
        return;
    }
    container.innerHTML = contacts.map(c => {
        const details = [];
        if (c.birthday) {
            const bday = new Date(c.birthday + 'T00:00:00');
            details.push(bday.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit', year: 'numeric' }));
        }
        if (c.email) details.push(c.email);
        if (c.phone) details.push(c.phone);
        if (c.address) details.push(c.address);
        return `<div class="contact-list-item" onclick="openEditContact('${c.id}')">
            <div class="contact-info">
                <span class="contact-name">${escapeHtml(c.name)}</span>
                ${details.length ? `<span class="contact-detail">${escapeHtml(details.join(' | '))}</span>` : ''}
            </div>
            <div class="contact-actions">
                <button class="btn-delete-contact" onclick="event.stopPropagation(); deleteContact('${c.id}')" title="Loeschen">&#128465;</button>
            </div>
        </div>`;
    }).join('');
}

document.getElementById('btn-new-contact').addEventListener('click', () => {
    closeModal('contacts-modal');
    openNewContact();
});

function openNewContact() {
    document.getElementById('contact-modal-title').textContent = 'Neuer Kontakt';
    document.getElementById('contact-id').value = '';
    document.getElementById('contact-name').value = '';
    document.getElementById('contact-birthday').value = '';
    document.getElementById('contact-email').value = '';
    document.getElementById('contact-phone').value = '';
    document.getElementById('contact-notes').value = '';
    setzeKontaktAdresse(null, null, null);
    document.getElementById('btn-delete-contact').classList.add('hidden');
    openModal('contact-modal');
}

// --- Ort am Termin: Adressvorschlaege und Wegzeit ----------------------------
// Der Ort war und bleibt ein Freitextfeld. Wer eine Adresse aus den Vorschlaegen
// waehlt, bekommt zusaetzlich Koordinaten, und erst daraus entsteht ein
// Weg-Termin. Wer weiter „bei Oma“ tippt, verliert nichts: der Text wird
// gespeichert wie bisher, es gibt nur keine Wegzeit dazu.
let terminKoordinaten = { lat: null, lon: null };

function zeigeWegZeile() {
    const zeile = document.getElementById('evt-weg');
    const auswahl = document.getElementById('evt-travel-mode');
    const text = document.getElementById('evt-weg-text');
    if (!zeile) return;
    if (!ortKonfig.wegzeit || terminKoordinaten.lat == null) {
        zeile.classList.add('hidden');
        return;
    }
    if (!auswahl.options.length) {
        // Nur anbieten, was der Anbieter wirklich kann. Ein Auswahlpunkt
        // „Bus und Bahn“, der still eine Fusszeit liefert, waere die
        // gefaehrlichere Variante: sie sieht richtig aus, und man merkt den
        // Fehler erst, wenn man zu spaet kommt.
        (ortKonfig.verkehrsmittel || []).filter(m => m.moeglich).forEach(m => {
            const o = document.createElement('option');
            o.value = m.id; o.textContent = m.name;
            auswahl.appendChild(o);
        });
        auswahl.value = ortKonfig.standard_mittel || 'pedestrian';
    }
    const fehlend = (ortKonfig.verkehrsmittel || []).filter(m => !m.moeglich);
    text.textContent = 'Der Weg wird berechnet und als eigener Termin davor eingetragen.'
        + (fehlend.length ? ` Nicht verfügbar: ${fehlend.map(m => m.name).join(', ')}.` : '');
    zeile.classList.remove('hidden');
}

function setzeTerminOrt(text, lat, lon) {
    document.getElementById('evt-location').value = text || '';
    terminKoordinaten = { lat: lat ?? null, lon: lon ?? null };
    zeigeWegZeile();
}

(function terminOrtVerdrahten() {
    const feld = document.getElementById('evt-location');
    const liste = document.getElementById('evt-location-hits');
    if (!feld || !liste) return;
    let warten = null;
    const verstecken = () => { liste.classList.add('hidden'); liste.innerHTML = ''; };

    feld.addEventListener('input', () => {
        terminKoordinaten = { lat: null, lon: null };
        zeigeWegZeile();
        clearTimeout(warten);
        const text = feld.value.trim();
        if (!ortKonfig.adressen || text.length < 3) { verstecken(); return; }
        warten = setTimeout(async () => {
            let treffer = [];
            // Gespeicherte Orte zuerst: „Arbeit“ soll man tippen koennen, ohne
            // die Adresse zu kennen.
            try {
                const orte = await apiJson('/api/places');
                treffer = (orte || [])
                    .filter(o => o.lat != null && o.name.toLowerCase().includes(text.toLowerCase()))
                    .map(o => ({ text: o.name, lat: o.lat, lon: o.lon, genauigkeit: 'ort_gespeichert' }));
            } catch (err) { /* Orte sind Kuer, Adressen die Pflicht */ }
            try {
                const antwort = await apiJson(`/api/places/adressen/suche?q=${encodeURIComponent(text)}`);
                treffer = treffer.concat((antwort && antwort.treffer) || []);
            } catch (err) { /* siehe oben */ }
            if (!treffer.length) { verstecken(); return; }
            liste.innerHTML = '';
            treffer.slice(0, 8).forEach(t => {
                const zeile = document.createElement('div');
                zeile.textContent = t.text;
                if (t.genauigkeit !== 'hausnummer') {
                    const zusatz = document.createElement('span');
                    zusatz.className = 'genauigkeit';
                    zusatz.textContent = { ort_gespeichert: 'gespeicherter Ort',
                                           strasse: 'nur Strasse', ort: 'nur Ort' }[t.genauigkeit] || '';
                    zeile.appendChild(zusatz);
                }
                zeile.onclick = () => { setzeTerminOrt(t.text, t.lat, t.lon); verstecken(); };
                liste.appendChild(zeile);
            });
            liste.classList.remove('hidden');
        }, 300);
    });

    feld.addEventListener('blur', () => setTimeout(verstecken, 200));
})();

// --- Adresse am Kontakt: Vorschlaege und kleine Karte ------------------------
// Was hier geht, entscheidet der Server, nicht das Frontend. `ortKonfig` haelt
// die Antwort von /api/places/konfiguration; ohne Adressdienst gibt es keine
// Vorschlaege, ohne Kartendienst keine Karte, und beides ist ein normaler
// Zustand. Ein Frontend, das eine leere Kartenflaeche zeigt, weil es nicht
// gefragt hat, ist der haeufigste Fehler dieser Bauart.
let ortKonfig = { adressen: false, karte: null, wegzeit: false };
let kontaktKoordinaten = { lat: null, lon: null };

async function ladeOrtKonfig() {
    try {
        ortKonfig = await apiJson('/api/places/konfiguration');
    } catch (err) {
        // Aeltere Instanz oder Endpunkt nicht erreichbar: alles aus, nichts kaputt.
        ortKonfig = { adressen: false, karte: null, wegzeit: false };
    }
}

function zeigeKontaktKarte(lat, lon, titel) {
    const box = document.getElementById('contact-map');
    if (!box) return;
    const rahmen = box.querySelector('iframe');
    if (!ortKonfig.karte || lat == null || lon == null) {
        box.classList.add('hidden');
        rahmen.removeAttribute('src');   // sonst laedt ein verstecktes iframe weiter
        return;
    }
    const frage = new URLSearchParams({
        einbetten: '1',
        markierung: `${lat},${lon}`,
        zoom: '16',
        // „Alltag" statt der Notfall-Vorgabe: wer eine Wohnadresse ansieht, will
        // wissen, was drumherum ist.
        sicht: 'alltag',
    });
    if (titel) frage.set('titel', titel);
    rahmen.src = `${ortKonfig.karte}/?${frage}`;
    box.classList.remove('hidden');
}

function setzeKontaktAdresse(adresse, lat, lon) {
    document.getElementById('contact-address').value = adresse || '';
    kontaktKoordinaten = { lat: lat ?? null, lon: lon ?? null };
    zeigeKontaktKarte(kontaktKoordinaten.lat, kontaktKoordinaten.lon,
                      document.getElementById('contact-name').value);
}

(function adressSucheVerdrahten() {
    const feld = document.getElementById('contact-address');
    const liste = document.getElementById('contact-address-hits');
    if (!feld || !liste) return;
    let warten = null;

    function verstecken() { liste.classList.add('hidden'); liste.innerHTML = ''; }

    feld.addEventListener('input', () => {
        // Getippte Adresse ohne Auswahl: die alten Koordinaten passen nicht mehr
        // zum Text. Sie stehen zu lassen waere schlimmer als sie zu verlieren,
        // denn dann zeigte die Karte das vorige Haus zur neuen Adresse.
        kontaktKoordinaten = { lat: null, lon: null };
        zeigeKontaktKarte(null, null);
        clearTimeout(warten);
        const text = feld.value.trim();
        if (!ortKonfig.adressen || text.length < 3) { verstecken(); return; }
        warten = setTimeout(async () => {
            let antwort;
            try {
                antwort = await apiJson(`/api/places/adressen/suche?q=${encodeURIComponent(text)}`);
            } catch (err) { verstecken(); return; }
            const treffer = (antwort && antwort.treffer) || [];
            if (!treffer.length) {
                liste.innerHTML = '<div class="adress-hinweis">Keine Adresse gefunden. '
                    + 'Der Text bleibt trotzdem gespeichert.</div>';
                liste.classList.remove('hidden');
                return;
            }
            liste.innerHTML = '';
            treffer.forEach(t => {
                const zeile = document.createElement('div');
                zeile.textContent = t.text;
                if (t.genauigkeit !== 'hausnummer') {
                    const zusatz = document.createElement('span');
                    zusatz.className = 'genauigkeit';
                    zusatz.textContent = t.genauigkeit === 'strasse'
                        ? 'nur Strasse' : 'nur Ort';
                    zeile.appendChild(zusatz);
                }
                zeile.onclick = () => { setzeKontaktAdresse(t.text, t.lat, t.lon); verstecken(); };
                liste.appendChild(zeile);
            });
            liste.classList.remove('hidden');
        }, 300);
    });

    feld.addEventListener('blur', () => setTimeout(verstecken, 200));
})();

function openEditContact(id) {
    const c = contacts.find(x => x.id === id);
    if (!c) return;
    closeModal('contacts-modal');
    document.getElementById('contact-modal-title').textContent = 'Kontakt bearbeiten';
    document.getElementById('contact-id').value = c.id;
    document.getElementById('contact-name').value = c.name;
    document.getElementById('contact-birthday').value = c.birthday || '';
    document.getElementById('contact-email').value = c.email || '';
    document.getElementById('contact-phone').value = c.phone || '';
    document.getElementById('contact-notes').value = c.notes || '';
    setzeKontaktAdresse(c.address, c.lat, c.lon);
    document.getElementById('btn-delete-contact').classList.remove('hidden');
    openModal('contact-modal');
}

document.getElementById('contact-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('contact-id').value;
    const payload = {
        name: document.getElementById('contact-name').value,
        birthday: document.getElementById('contact-birthday').value || null,
        email: document.getElementById('contact-email').value || null,
        phone: document.getElementById('contact-phone').value || null,
        notes: document.getElementById('contact-notes').value || null,
        address: document.getElementById('contact-address').value || null,
        lat: kontaktKoordinaten.lat,
        lon: kontaktKoordinaten.lon,
    };
    try {
        if (id) {
            await apiJson(`/api/contacts/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
        } else {
            await apiJson('/api/contacts', { method: 'POST', body: JSON.stringify(payload) });
        }
        closeModal('contact-modal');
        refreshCalendar();
        loadFocusStrip();
        loadDashboardWidget();
        // Reopen contacts list
        await openContactsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

document.getElementById('btn-delete-contact').addEventListener('click', async () => {
    const id = document.getElementById('contact-id').value;
    if (!id) return;
    if (!confirm('Kontakt loeschen?')) return;
    try {
        await apiDelete(`/api/contacts/${id}`);
        closeModal('contact-modal');
        refreshCalendar();
        loadFocusStrip();
        loadDashboardWidget();
        await openContactsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

async function deleteContact(id) {
    if (!confirm('Kontakt loeschen?')) return;
    try {
        await apiDelete(`/api/contacts/${id}`);
        refreshCalendar();
        loadFocusStrip();
        loadDashboardWidget();
        await loadContacts();
        renderContactsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// --- Modal helpers ---
function openModal(id) {
    const modal = document.getElementById(id);
    modal.style.display = 'flex';
    modal.classList.remove('hidden');
    requestAnimationFrame(() => {
        requestAnimationFrame(() => modal.classList.add('modal-visible'));
    });
}

function closeModal(id) {
    const modal = document.getElementById(id);
    modal.classList.remove('modal-visible');
    setTimeout(() => {
        modal.style.display = '';
        modal.classList.add('hidden');
    }, 200);
}

document.querySelectorAll('.btn-cancel').forEach(btn => {
    btn.addEventListener('click', () => {
        const modal = btn.closest('.modal');
        closeModal(modal.id);
    });
});

document.querySelectorAll('.modal').forEach(modal => {
    modal.addEventListener('click', (e) => {
        if (e.target === modal) closeModal(modal.id);
    });
});

// --- Utility ---
function escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function formatDate(date) {
    const y = date.getFullYear();
    const m = String(date.getMonth() + 1).padStart(2, '0');
    const d = String(date.getDate()).padStart(2, '0');
    return `${y}-${m}-${d}`;
}

// --- Toast notifications ---
function showToast(message, type = 'info', duration = 3000) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.className = 'toast';
    if (type === 'error') toast.classList.add('toast-error');
    toast.classList.remove('hidden');
    requestAnimationFrame(() => {
        requestAnimationFrame(() => toast.classList.add('visible'));
    });
    setTimeout(() => {
        toast.classList.remove('visible');
        setTimeout(() => toast.classList.add('hidden'), 300);
    }, duration);
}

// --- Floating "Heute" button + keyboard shortcut ---
document.getElementById('btn-scroll-today').addEventListener('click', scrollToToday);

(function() {
    const mainEl = document.querySelector('main');
    if (mainEl) {
        mainEl.addEventListener('scroll', () => {
            const todayCell = document.querySelector('.day-cell.is-today');
            const btn = document.getElementById('btn-scroll-today');
            if (!todayCell || !btn) return;
            const rect = todayCell.getBoundingClientRect();
            const mainRect = mainEl.getBoundingClientRect();
            const visible = rect.top < mainRect.bottom && rect.bottom > mainRect.top;
            btn.classList.toggle('visible', !visible);
        }, { passive: true });
    }
})();

document.addEventListener('keydown', (e) => {
    // Command-Palette: Cmd/Ctrl+K, global, auch in Eingabefeldern.
    if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
        e.preventDefault();
        const palette = document.getElementById('command-palette');
        palette.classList.contains('hidden') ? openCommandPalette() : closeCommandPalette();
        return;
    }
    if (e.target.matches('input, textarea, select')) return;
    if (document.querySelector('.modal:not(.hidden), .cmd-palette:not(.hidden)')) return;
    if (e.key === 't' || e.key === 'T') {
        if (currentView === 'week') weekGoToday();
        else if (currentView === 'month') monthGoToday();
        else scrollToToday();
    }
    if (e.key === 'w' || e.key === 'W') switchView(currentView === 'week' ? 'year' : 'week');
    if (e.key === 'm' || e.key === 'M') switchView(currentView === 'month' ? 'year' : 'month');
    if (currentView === 'week') {
        if (e.key === 'ArrowLeft') navigateWeek(-1);
        if (e.key === 'ArrowRight') navigateWeek(1);
    }
    if (currentView === 'month') {
        if (e.key === 'ArrowLeft') navigateMonth(-1);
        if (e.key === 'ArrowRight') navigateMonth(1);
    }
});

// --- Command Palette (Cmd/Ctrl+K) ---
const CMD_COMMANDS = [
    { label: 'Neues Event', hint: 'Termin anlegen', icon: '📅', run: () => { const d = formatDate(new Date()); openNewEvent(d, d, false); } },
    { label: 'Neue Aufgabe', hint: 'To-Do anlegen', icon: '✅', run: () => openTodosList() },
    { label: 'Neues Tagesziel', hint: 'Fokus fuer heute', icon: '🎯', run: () => {
        const btn = document.querySelector('.goal-add-btn'); if (btn) { btn.click(); btn.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
    } },
    { label: 'Heute', hint: 'Zum heutigen Tag', icon: '📍', run: () => { currentView === 'week' ? weekGoToday() : scrollToToday(); } },
    { label: 'Wochenansicht', hint: 'Ansicht wechseln', icon: '🗓️', run: () => switchView('week') },
    { label: 'Monatsansicht', hint: 'Ansicht wechseln', icon: '🗓️', run: () => switchView('month') },
    { label: 'Jahresansicht', hint: 'Ansicht wechseln', icon: '🗓️', run: () => switchView('year') },
    { label: 'To-Dos oeffnen', hint: 'Aufgabenliste', icon: '📋', run: () => openTodosList() },
    { label: 'Habits oeffnen', hint: 'Gewohnheiten', icon: '🔁', run: () => openHabitsList() },
    { label: 'Kontakte oeffnen', hint: 'Adressbuch', icon: '👤', run: () => openContactsList() },
];

let cmdActiveIndex = 0;
let cmdResults = [];

function openCommandPalette() {
    const palette = document.getElementById('command-palette');
    const input = document.getElementById('cmd-input');
    palette.classList.remove('hidden');
    input.value = '';
    renderCommandResults('');
    setTimeout(() => input.focus(), 0);
}

function closeCommandPalette() {
    document.getElementById('command-palette').classList.add('hidden');
}

function renderCommandResults(query) {
    const list = document.getElementById('cmd-list');
    const q = query.trim().toLowerCase();
    const isSearch = q.startsWith('/');

    if (isSearch && q.length > 1) {
        runPaletteSearch(q.slice(1).trim());
        return;
    }

    cmdResults = q
        ? CMD_COMMANDS.filter(c => c.label.toLowerCase().includes(q) || c.hint.toLowerCase().includes(q))
        : CMD_COMMANDS.slice();
    cmdActiveIndex = 0;
    list.innerHTML = cmdResults.map((c, i) => `
        <li class="cmd-item ${i === 0 ? 'active' : ''}" data-index="${i}">
            <span class="cmd-icon">${c.icon}</span>
            <span class="cmd-label">${escapeHtml(c.label)}</span>
            <span class="cmd-itemhint">${escapeHtml(c.hint)}</span>
        </li>`).join('') || '<li class="cmd-empty">Keine Treffer</li>';
}

async function runPaletteSearch(term) {
    const list = document.getElementById('cmd-list');
    if (!term) { list.innerHTML = '<li class="cmd-empty">Suchbegriff eingeben…</li>'; return; }
    list.innerHTML = '<li class="cmd-empty">Suche…</li>';
    try {
        const res = await apiJson(`/api/search?q=${encodeURIComponent(term)}`);
        cmdResults = (res.results || []).map(r => ({
            label: r.title,
            hint: r.kind_label || r.kind,
            icon: r.icon || '🔎',
            run: () => handleSearchResult(r),
        }));
        cmdActiveIndex = 0;
        list.innerHTML = cmdResults.length
            ? cmdResults.map((c, i) => `
                <li class="cmd-item ${i === 0 ? 'active' : ''}" data-index="${i}">
                    <span class="cmd-icon">${c.icon}</span>
                    <span class="cmd-label">${escapeHtml(c.label)}</span>
                    <span class="cmd-itemhint">${escapeHtml(c.hint)}</span>
                </li>`).join('')
            : '<li class="cmd-empty">Nichts gefunden</li>';
    } catch (err) {
        list.innerHTML = '<li class="cmd-empty">Suche fehlgeschlagen</li>';
    }
}

function handleSearchResult(r) {
    // Sprung zum Datum des Treffers, falls vorhanden.
    if (r.date) {
        const target = new Date(r.date + 'T12:00:00');
        if (currentView === 'week') {
            weekViewDate = getMonday(target);
            refreshWeekView();
        } else if (currentView === 'month') {
            monthViewDate = new Date(target.getFullYear(), target.getMonth(), 1);
            refreshMonthView();
        } else {
            scrollToToday(true);
        }
    }
    if (r.kind === 'todo') openTodosList();
}

function cmdSetActive(index) {
    const items = document.querySelectorAll('#cmd-list .cmd-item');
    if (!items.length) return;
    cmdActiveIndex = (index + items.length) % items.length;
    items.forEach((el, i) => el.classList.toggle('active', i === cmdActiveIndex));
    items[cmdActiveIndex].scrollIntoView({ block: 'nearest' });
}

function cmdExecuteActive() {
    const cmd = cmdResults[cmdActiveIndex];
    closeCommandPalette();
    if (cmd && typeof cmd.run === 'function') cmd.run();
}

document.getElementById('cmd-input')?.addEventListener('input', (e) => renderCommandResults(e.target.value));
document.getElementById('cmd-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); cmdSetActive(cmdActiveIndex + 1); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); cmdSetActive(cmdActiveIndex - 1); }
    else if (e.key === 'Enter') { e.preventDefault(); cmdExecuteActive(); }
    else if (e.key === 'Escape') { e.preventDefault(); closeCommandPalette(); }
});
document.getElementById('cmd-list')?.addEventListener('click', (e) => {
    const item = e.target.closest('.cmd-item');
    if (!item) return;
    cmdActiveIndex = parseInt(item.dataset.index, 10);
    cmdExecuteActive();
});
document.getElementById('command-palette')?.addEventListener('click', (e) => {
    if (e.target.id === 'command-palette') closeCommandPalette();
});

// --- Habits Widget ---
async function loadHabitsWidget() {
    const container = document.getElementById('habits-widget');
    try {
        const [sessions, progressData] = await Promise.all([
            apiJson('/api/habits/sessions/today'),
            apiJson('/api/habits/weekly-progress').catch(() => []),
        ]);

        let html = '<div class="widget-card">';

        // Strain bar for learning habits
        const learningProgress = progressData.filter(p => p.category === 'lernen');
        if (learningProgress.length > 0) {
            const strain = learningProgress[0].strain || 0;
            const strainPct = Math.round(strain * 100);
            const strainClass = strain < 0.3 ? 'strain-low' : strain < 0.5 ? 'strain-medium' : strain < 0.7 ? 'strain-high' : 'strain-critical';
            const strainLabels = { 'strain-low': 'Niedrig', 'strain-medium': 'Mittel', 'strain-high': 'Hoch', 'strain-critical': 'Kritisch' };
            html += `<div class="strain-bar-container">
                <div class="strain-bar-header">
                    <span class="strain-bar-label">Belastung</span>
                    <span class="strain-bar-value ${strainClass}">${strainLabels[strainClass]}</span>
                </div>
                <div class="strain-bar"><div class="strain-bar-fill ${strainClass}" style="width:${strainPct}%"></div></div>
            </div>`;
        }

        // Off-day banner
        const isOffDay = sessions.length > 0 && sessions[0].is_off_day;
        if (isOffDay) {
            html += '<div class="off-day-banner">Lern-Ruhetag</div>';
        }

        html += '<div class="widget-section-title">Habits heute</div>';

        if (!sessions || sessions.length === 0) {
            html += '<div class="widget-empty">Keine Sessions heute</div>';
        } else {
            const now = new Date();
            const sessionTypeLabels = { input: 'Input', review: 'Review' };
            for (const s of sessions) {
                const startDt = new Date(s.start);
                const endDt = new Date(s.end);
                const sh = String(startDt.getHours()).padStart(2, '0');
                const sm = String(startDt.getMinutes()).padStart(2, '0');
                const eh = String(endDt.getHours()).padStart(2, '0');
                const em = String(endDt.getMinutes()).padStart(2, '0');
                const timeStr = `${sh}:${sm} – ${eh}:${em}`;
                const color = s.habit_color || '#4a9eff';
                const name = s.habit_name || 'Habit';
                const status = s.status;
                const sType = s.session_type || 'standard';
                const typeBadge = sType !== 'standard' ? ` <span class="session-type-badge type-${sType}">${sessionTypeLabels[sType] || sType}</span>` : '';

                let actionBtns = '';
                if (status === 'pending') {
                    actionBtns = `<div class="habit-session-actions">
                        <button class="btn-start-early" data-session="${s.id}" data-action="start_early" title="Jetzt starten">&#9654;</button>
                        <button class="btn-dismiss" data-session="${s.id}" data-action="dismissed" title="Verschieben">&#10007;</button>
                    </div>`;
                } else if (status === 'accepted') {
                    actionBtns = `<div class="habit-session-actions">
                        <button class="btn-cancel" data-session="${s.id}" data-action="cancelled" title="Abbrechen">&#9724;</button>
                    </div>`;
                } else if (status === 'completed') {
                    actionBtns = `<div class="habit-session-actions"><span class="habit-session-done" title="Erledigt">&#10003;</span></div>`;
                }

                html += `<div class="habit-today-item status-${status}" style="--habit-color:${color}">
                    <div class="habit-today-info">
                        <span class="widget-event-dot" style="background:${color}"></span>
                        <span class="habit-today-name">${escapeHtml(name)}${typeBadge}</span>
                        <span class="habit-today-time">${timeStr}</span>
                    </div>
                    ${actionBtns}
                </div>`;
            }
        }

        // Daily progress bar
        if (sessions && sessions.length > 0) {
            const completed = sessions.filter(s => s.status === 'completed').length;
            const total = sessions.length;
            const ratio = total > 0 ? completed / total : 0;
            const pct = Math.round(ratio * 100);
            const heatCls = ratio < 0.25 ? 'heatmap-cold' : ratio < 0.5 ? 'heatmap-warm' : ratio < 0.75 ? 'heatmap-hot' : 'heatmap-fire';
            html += `<div class="daily-progress">
                <div class="daily-progress-label"><span>Tagesfortschritt</span><span>${completed}/${total}</span></div>
                <div class="daily-progress-bar"><div class="daily-progress-fill ${heatCls}" style="width:${pct}%"></div></div>
            </div>`;
        }

        html += '</div>';
        container.innerHTML = html;

        // Wire up action buttons
        container.querySelectorAll('.habit-session-actions button').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const sessionId = btn.dataset.session;
                const action = btn.dataset.action;
                try {
                    const result = await apiJson(`/api/habits/sessions/${sessionId}/action`, {
                        method: 'POST',
                        body: JSON.stringify({ action }),
                    });
                    refreshCalendar();
                    loadFocusStrip();
                    loadHabitsWidget();
                    if (result && result.off_day_activated) {
                        showToast(result.message || 'Ruhetag aktiviert');
                    } else {
                        const toasts = { accepted: 'Session gestartet', dismissed: 'Session verschoben', cancelled: 'Session abgebrochen, Rest verschoben', start_early: 'Session frueher gestartet' };
                        showToast(toasts[action] || 'Erledigt');
                    }
                } catch (err) {
                    showToast(err.message, 'error');
                }
            });
        });
    } catch {
        container.innerHTML = '';
    }
}

// --- Habit Session Click ---
async function handleHabitSessionClick(fcEvent) {
    const dateStr = formatDate(fcEvent.start);
    const dayCell = document.querySelector(`.day-cell[data-date="${dateStr}"]`);
    if (dayCell) showDayDetail(dateStr, dayCell);
}

// --- Projects CRUD ---
async function loadProjects() {
    projects = await apiJson('/api/projects');
}

async function openProjectsList() {
    await loadProjects();
    renderProjectsList();
    openModal('projects-modal');
}

function renderProjectsList() {
    const container = document.getElementById('projects-list');
    if (projects.length === 0) {
        container.innerHTML = '<div class="contacts-empty">Noch keine Projekte vorhanden.</div>';
        return;
    }
    container.innerHTML = projects.map(p => {
        const statusLabels = { active: 'Aktiv', paused: 'Pausiert', completed: 'Fertig' };
        return `<div class="project-list-item" onclick="openEditProject('${p.id}')">
            <div class="project-list-info">
                <span class="project-color-dot" style="background:${p.color}"></span>
                <span class="project-list-name">${p.icon ? p.icon + ' ' : ''}${escapeHtml(p.name)}</span>
                <span class="project-list-status status-${p.status}">${statusLabels[p.status] || p.status}</span>
            </div>
            <div class="project-list-actions">
                <button class="btn-delete-project" onclick="event.stopPropagation(); deleteProject('${p.id}')" title="Loeschen">&#128465;</button>
            </div>
        </div>`;
    }).join('');
}

document.getElementById('btn-new-project').addEventListener('click', () => {
    closeModal('projects-modal');
    openNewProject();
});

function openNewProject() {
    document.getElementById('project-modal-title').textContent = 'Neues Projekt';
    document.getElementById('project-id').value = '';
    document.getElementById('project-name').value = '';
    document.getElementById('project-color').value = '#d0874a';
    document.getElementById('project-icon').value = '';
    document.getElementById('project-status').value = 'active';
    document.getElementById('btn-delete-project').classList.add('hidden');
    openModal('project-modal');
}

function openEditProject(id) {
    const p = projects.find(x => x.id === id);
    if (!p) return;
    closeModal('projects-modal');
    document.getElementById('project-modal-title').textContent = 'Projekt bearbeiten';
    document.getElementById('project-id').value = p.id;
    document.getElementById('project-name').value = p.name;
    document.getElementById('project-color').value = p.color;
    document.getElementById('project-icon').value = p.icon || '';
    document.getElementById('project-status').value = p.status;
    document.getElementById('btn-delete-project').classList.remove('hidden');
    openModal('project-modal');
}

document.getElementById('project-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('project-id').value;
    const payload = {
        name: document.getElementById('project-name').value,
        color: document.getElementById('project-color').value,
        icon: document.getElementById('project-icon').value || null,
        status: document.getElementById('project-status').value,
    };
    try {
        if (id) {
            await apiJson(`/api/projects/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
        } else {
            await apiJson('/api/projects', { method: 'POST', body: JSON.stringify(payload) });
        }
        closeModal('project-modal');
        await openProjectsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

document.getElementById('btn-delete-project').addEventListener('click', async () => {
    const id = document.getElementById('project-id').value;
    if (!id) return;
    if (!confirm('Projekt loeschen?')) return;
    try {
        await apiDelete(`/api/projects/${id}`);
        closeModal('project-modal');
        await openProjectsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

async function deleteProject(id) {
    if (!confirm('Projekt loeschen?')) return;
    try {
        await apiDelete(`/api/projects/${id}`);
        await loadProjects();
        renderProjectsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// --- Category and learning mode toggles ---
document.getElementById('habit-category').addEventListener('change', function() {
    const isLernen = this.value === 'lernen';
    document.getElementById('learning-category-fields').classList.toggle('hidden', !isLernen);
    // Show Pomodoro checkbox only for lernen
    const lmCheckbox = document.getElementById('habit-learning-mode');
    lmCheckbox.closest('.form-checkbox').classList.toggle('hidden', !isLernen);
    if (!isLernen) {
        lmCheckbox.checked = false;
        document.getElementById('learning-mode-fields').classList.add('hidden');
        document.getElementById('habit-target-label').textContent = 'Stunden / Woche';
    }
});

document.getElementById('habit-learning-mode').addEventListener('change', function() {
    document.getElementById('learning-mode-fields').classList.toggle('hidden', !this.checked);
    document.getElementById('habit-target-label').textContent = this.checked ? 'Fokus-Stunden / Woche' : 'Stunden / Woche';
});

document.getElementById('habit-wd-ratio').addEventListener('input', function() {
    document.getElementById('habit-wd-ratio-label').textContent = this.value + '%';
});

// --- Habits CRUD ---
document.getElementById('btn-habits').addEventListener('click', openHabitsList);
document.getElementById('btn-manage-projects').addEventListener('click', () => {
    closeModal('habits-list-modal');
    openProjectsList();
});

async function loadHabits() {
    habits = await apiJson('/api/habits');
}

async function openHabitsList() {
    await Promise.all([loadHabits(), loadProjects()]);
    renderHabitsList();
    openModal('habits-list-modal');
}

function renderHabitsList() {
    const container = document.getElementById('habits-list');
    if (habits.length === 0) {
        container.innerHTML = '<div class="contacts-empty">Noch keine Habits vorhanden.</div>';
        return;
    }
    const categoryLabels = { lernen: 'Lernen', lesen: 'Lesen', sonstige: '' };

    // Group habits by project
    const groups = new Map(); // project_id -> { project, habits[] }
    for (const h of habits) {
        const key = h.project_id || '__none__';
        if (!groups.has(key)) {
            const proj = h.project_id ? projects.find(p => p.id === h.project_id) : null;
            groups.set(key, { project: proj, habits: [] });
        }
        groups.get(key).habits.push(h);
    }

    // Sort: projects with name first, "Ohne Projekt" last
    const sorted = [...groups.entries()].sort((a, b) => {
        if (a[0] === '__none__') return 1;
        if (b[0] === '__none__') return -1;
        return (a[1].project?.name || '').localeCompare(b[1].project?.name || '');
    });

    let html = '';
    for (const [key, group] of sorted) {
        const proj = group.project;
        const groupName = proj ? `${proj.icon ? proj.icon + ' ' : ''}${escapeHtml(proj.name)}` : 'Ohne Projekt';
        const groupColor = proj ? proj.color : 'var(--text-secondary)';
        const clickAttr = proj ? ` onclick="openEditProject('${proj.id}')" style="cursor:pointer"` : '';
        html += `<div class="habit-group">
            <div class="habit-group-header"${clickAttr}>
                <span class="habit-group-dot" style="background:${groupColor}"></span>
                <span class="habit-group-name">${groupName}</span>
                <span class="habit-group-count">${group.habits.length}</span>
            </div>`;
        for (const h of group.habits) {
            const isActive = h.active !== false;
            const inactiveCls = isActive ? '' : ' habit-inactive';
            const pauseTag = isActive ? '' : ' <span class="project-badge" style="background:var(--danger);color:#fff;font-size:0.6rem">Pausiert</span>';
            const cat = h.category || 'sonstige';
            const catTag = cat !== 'sonstige' ? ` <span class="project-badge category-badge category-${cat}">${categoryLabels[cat]}</span>` : '';
            const learnTag = h.learning_mode ? ' <span class="project-badge learning-badge">Pomodoro</span>' : '';
            const targetLabel = h.learning_mode ? 'Fokus-Std/Woche' : 'Std/Woche';
            const checkedAttr = isActive ? ' checked' : '';
            html += `<div class="habit-list-item${inactiveCls}" onclick="openEditHabit('${h.id}')">
                <div class="habit-list-info">
                    <span class="habit-list-name">
                        <span class="habit-color-dot" style="background:${h.color}"></span>
                        ${escapeHtml(h.name)}${pauseTag}${catTag}${learnTag}
                    </span>
                    <span class="habit-list-detail">${h.target_hours_per_week} ${targetLabel}, mind. ${h.session_duration_minutes} Min</span>
                </div>
                <div class="habit-list-actions">
                    <label class="switch" onclick="event.stopPropagation()">
                        <input type="checkbox" class="habit-toggle" data-habit-id="${h.id}"${checkedAttr}>
                        <span class="switch-slider"></span>
                    </label>
                    <button class="btn-delete-habit" onclick="event.stopPropagation(); deleteHabit('${h.id}')" title="Loeschen">&#128465;</button>
                </div>
            </div>`;
        }
        html += '</div>';
    }
    container.innerHTML = html;

    // Wire toggle switches for activate/deactivate
    container.querySelectorAll('.habit-toggle').forEach(toggle => {
        toggle.addEventListener('change', async (e) => {
            const habitId = toggle.dataset.habitId;
            const newActive = toggle.checked;
            try {
                await apiJson(`/api/habits/${habitId}`, {
                    method: 'PUT',
                    body: JSON.stringify({ active: newActive }),
                });
                await loadHabits();
                renderHabitsList();
                refreshCalendar();
                loadFocusStrip();
                loadHabitsWidget();
                if (currentView === 'week') refreshWeekView();
            } catch (err) {
                showToast(err.message, 'error');
                toggle.checked = !newActive;
            }
        });
    });
}

document.getElementById('btn-new-habit').addEventListener('click', () => {
    closeModal('habits-list-modal');
    openNewHabit();
});

async function openNewHabit() {
    await loadProjects();
    populateProjectDropdown('');
    document.getElementById('habit-modal-title').textContent = 'Neuer Habit';
    document.getElementById('habit-id').value = '';
    document.getElementById('habit-name').value = '';
    document.getElementById('habit-color').value = '#4a9eff';
    document.getElementById('habit-project').value = '';
    document.getElementById('habit-category').value = 'sonstige';
    document.getElementById('habit-target').value = '6';
    document.getElementById('habit-duration').value = '90';
    document.getElementById('habit-wd-start').value = '17:00';
    document.getElementById('habit-wd-end').value = '20:00';
    document.getElementById('habit-we-start').value = '10:00';
    document.getElementById('habit-we-end').value = '18:00';
    document.getElementById('habit-learning-mode').checked = false;
    document.getElementById('habit-learning-mode').closest('.form-checkbox').classList.add('hidden');
    document.getElementById('learning-mode-fields').classList.add('hidden');
    document.getElementById('learning-category-fields').classList.add('hidden');
    document.getElementById('habit-target-label').textContent = 'Stunden / Woche';
    document.getElementById('habit-focus-min').value = '25';
    document.getElementById('habit-break-min').value = '5';
    document.getElementById('habit-wd-ratio').value = '30';
    document.getElementById('habit-wd-ratio-label').textContent = '30%';
    document.getElementById('habit-max-consecutive').value = '3';
    document.getElementById('habit-max-session').value = '120';
    document.getElementById('habit-active').checked = true;
    document.getElementById('habit-active-row').classList.add('hidden');
    document.getElementById('btn-delete-habit').classList.add('hidden');
    openModal('habit-modal');
}

async function openEditHabit(id) {
    const h = habits.find(x => x.id === id);
    if (!h) return;
    closeModal('habits-list-modal');
    await loadProjects();
    populateProjectDropdown(h.project_id || '');
    const category = h.category || 'sonstige';
    const isLernen = category === 'lernen';
    document.getElementById('habit-modal-title').textContent = 'Habit bearbeiten';
    document.getElementById('habit-id').value = h.id;
    document.getElementById('habit-name').value = h.name;
    document.getElementById('habit-color').value = h.color;
    document.getElementById('habit-project').value = h.project_id || '';
    document.getElementById('habit-category').value = category;
    document.getElementById('habit-target').value = h.target_hours_per_week;
    document.getElementById('habit-duration').value = h.session_duration_minutes;
    document.getElementById('habit-wd-start').value = h.weekday_start;
    document.getElementById('habit-wd-end').value = h.weekday_end;
    document.getElementById('habit-we-start').value = h.weekend_start;
    document.getElementById('habit-we-end').value = h.weekend_end;
    document.getElementById('habit-learning-mode').checked = !!h.learning_mode;
    document.getElementById('habit-learning-mode').closest('.form-checkbox').classList.toggle('hidden', !isLernen);
    document.getElementById('learning-mode-fields').classList.toggle('hidden', !h.learning_mode);
    document.getElementById('learning-category-fields').classList.toggle('hidden', !isLernen);
    document.getElementById('habit-target-label').textContent = h.learning_mode ? 'Fokus-Stunden / Woche' : 'Stunden / Woche';
    document.getElementById('habit-focus-min').value = h.focus_block_minutes || 25;
    document.getElementById('habit-break-min').value = h.break_minutes || 5;
    const wdRatio = Math.round((h.weekday_target_ratio || 0.3) * 100);
    document.getElementById('habit-wd-ratio').value = wdRatio;
    document.getElementById('habit-wd-ratio-label').textContent = wdRatio + '%';
    document.getElementById('habit-max-consecutive').value = h.max_consecutive_days || 3;
    document.getElementById('habit-max-session').value = h.max_session_minutes || 120;
    document.getElementById('habit-active').checked = h.active !== false;
    document.getElementById('habit-active-row').classList.remove('hidden');
    document.getElementById('btn-delete-habit').classList.remove('hidden');
    openModal('habit-modal');
}

function populateProjectDropdown(selectedId) {
    const sel = document.getElementById('habit-project');
    sel.innerHTML = '<option value="">-- Kein Projekt --</option>';
    for (const p of projects) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = (p.icon ? p.icon + ' ' : '') + p.name;
        if (p.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    }
}

document.getElementById('habit-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('habit-id').value;
    const category = document.getElementById('habit-category').value;
    const isLearning = document.getElementById('habit-learning-mode').checked;
    const payload = {
        name: document.getElementById('habit-name').value,
        color: document.getElementById('habit-color').value,
        project_id: document.getElementById('habit-project').value || null,
        target_hours_per_week: parseFloat(document.getElementById('habit-target').value),
        session_duration_minutes: parseInt(document.getElementById('habit-duration').value),
        weekday_start: document.getElementById('habit-wd-start').value,
        weekday_end: document.getElementById('habit-wd-end').value,
        weekend_start: document.getElementById('habit-we-start').value,
        weekend_end: document.getElementById('habit-we-end').value,
        learning_mode: isLearning,
        focus_block_minutes: parseInt(document.getElementById('habit-focus-min').value),
        break_minutes: parseInt(document.getElementById('habit-break-min').value),
        category: category,
        weekday_target_ratio: parseInt(document.getElementById('habit-wd-ratio').value) / 100,
        max_consecutive_days: parseInt(document.getElementById('habit-max-consecutive').value),
        max_session_minutes: parseInt(document.getElementById('habit-max-session').value),
        active: document.getElementById('habit-active').checked,
    };
    try {
        if (id) {
            await apiJson(`/api/habits/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
        } else {
            await apiJson('/api/habits', { method: 'POST', body: JSON.stringify(payload) });
        }
        await apiJson('/api/habits/schedule-ahead', { method: 'POST' }).catch(() => {});
        closeModal('habit-modal');
        refreshCalendar();
        loadFocusStrip();
        loadHabitsWidget();
        if (currentView === 'week') refreshWeekView();
        await openHabitsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

document.getElementById('btn-delete-habit').addEventListener('click', async () => {
    const id = document.getElementById('habit-id').value;
    if (!id) return;
    if (!confirm('Habit loeschen? Alle Sessions werden ebenfalls geloescht.')) return;
    try {
        await apiDelete(`/api/habits/${id}`);
        closeModal('habit-modal');
        refreshCalendar();
        loadFocusStrip();
        loadHabitsWidget();
        if (currentView === 'week') refreshWeekView();
        await openHabitsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
});

async function deleteHabit(id) {
    if (!confirm('Habit loeschen?')) return;
    try {
        await apiDelete(`/api/habits/${id}`);
        refreshCalendar();
        loadFocusStrip();
        loadHabitsWidget();
        if (currentView === 'week') refreshWeekView();
        await loadHabits();
        renderHabitsList();
    } catch (err) {
        showToast(err.message, 'error');
    }
}

// --- Month View (6x7 grid) ---
let monthViewDate = null; // erster Tag des angezeigten Monats

const MONTH_GRID_NAMES = ['Januar', 'Februar', 'März', 'April', 'Mai', 'Juni',
    'Juli', 'August', 'September', 'Oktober', 'November', 'Dezember'];

function navigateMonth(direction) {
    if (!monthViewDate) monthViewDate = new Date();
    monthViewDate = new Date(monthViewDate.getFullYear(), monthViewDate.getMonth() + direction, 1);
    refreshMonthView();
}

function monthGoToday() {
    const now = new Date();
    monthViewDate = new Date(now.getFullYear(), now.getMonth(), 1);
    refreshMonthView();
}

async function refreshMonthView() {
    if (!monthViewDate) {
        const now = new Date();
        monthViewDate = new Date(now.getFullYear(), now.getMonth(), 1);
    }
    if (!cachedEvents.length) await fetchCalendarEvents();
    renderMonthGrid();
}

function renderMonthGrid() {
    const grid = document.getElementById('month-grid');
    const label = document.getElementById('month-label');
    if (!grid) return;
    const year = monthViewDate.getFullYear();
    const month = monthViewDate.getMonth();
    label.textContent = `${MONTH_GRID_NAMES[month]} ${year}`;

    // Raster ab Montag der Woche, in der der 1. liegt.
    const first = new Date(year, month, 1);
    const offset = (first.getDay() + 6) % 7; // Mo=0
    const gridStart = new Date(year, month, 1 - offset);
    const todayStr = formatDate(new Date());

    const weekdayHead = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
        .map(d => `<div class="month-head">${d}</div>`).join('');

    let cells = '';
    for (let i = 0; i < 42; i++) {
        const d = new Date(gridStart.getTime() + i * 86400000);
        const dateStr = formatDate(d);
        const isOther = d.getMonth() !== month;
        const isToday = dateStr === todayStr;
        const isWeekend = d.getDay() === 0 || d.getDay() === 6;
        const dayEvents = eventsOnDay(d);
        const hasGoal = goalDates && goalDates.has(dateStr);

        const chips = dayEvents.slice(0, 3).map(ev => {
            const cls = ev.extendedProps?.is_daytype ? 'month-chip daytype' :
                        ev.extendedProps?.is_habit_session ? 'month-chip habit' :
                        ev.extendedProps?.is_todo ? 'month-chip todo' : 'month-chip';
            const color = ev.backgroundColor || '#3788d8';
            const time = ev.allDay ? '' : `<span class="month-chip-time">${pad2(ev.start.getHours())}:${pad2(ev.start.getMinutes())}</span> `;
            return `<div class="${cls}" style="--chip:${color}" data-event-id="${escapeHtml(String(ev.id))}" title="${escapeHtml(ev.title || '')}">${time}${escapeHtml(ev.title || '')}</div>`;
        }).join('');
        const more = dayEvents.length > 3 ? `<div class="month-more">+${dayEvents.length - 3} weitere</div>` : '';
        const goalDot = hasGoal ? '<span class="month-goaldot" title="Tagesziel"></span>' : '';

        cells += `<div class="month-cell ${isOther ? 'other-month' : ''} ${isToday ? 'today' : ''} ${isWeekend ? 'weekend' : ''}" data-date="${dateStr}">
            <div class="month-cell-head"><span class="month-daynum">${d.getDate()}</span>${goalDot}</div>
            <div class="month-chips">${chips}${more}</div>
        </div>`;
    }
    grid.innerHTML = `<div class="month-heads">${weekdayHead}</div><div class="month-cells">${cells}</div>`;

    // Klick auf Tag → reiches Tagesdetail (wiederverwendet).
    grid.querySelectorAll('.month-cell').forEach(cell => {
        cell.addEventListener('click', (e) => {
            const chip = e.target.closest('.month-chip');
            if (chip) {
                e.stopPropagation();
                const ev = getEventById(chip.dataset.eventId);
                if (ev && ev.extendedProps?.is_todo) { openTodoFromCalendar(ev.extendedProps.todo_id); return; }
                if (ev && !ev.extendedProps?.is_daytype && !ev.extendedProps?.is_habit_session) { openEditEvent(ev); return; }
            }
            showDayDetail(cell.dataset.date, cell);
        });
    });
}

function eventsOnDay(d) {
    const dayStart = new Date(d.getFullYear(), d.getMonth(), d.getDate());
    const dayEnd = new Date(dayStart.getTime() + 86400000);
    return cachedEvents
        .filter(ev => {
            if (ev.display === 'background') return false; // Tagestyp-Hintergrund nicht als Chip
            const s = ev.start;
            const e = ev.end || ev.start;
            return s < dayEnd && e > dayStart;
        })
        .sort((a, b) => (a.allDay === b.allDay) ? a.start - b.start : (a.allDay ? -1 : 1));
}

function pad2(n) { return String(n).padStart(2, '0'); }

document.getElementById('month-prev')?.addEventListener('click', () => navigateMonth(-1));
document.getElementById('month-next')?.addEventListener('click', () => navigateMonth(1));
document.getElementById('month-today')?.addEventListener('click', monthGoToday);

// --- View Switching ---
const VIEW_CYCLE = ['year', 'month', 'week'];
const VIEW_LABEL = { year: 'Jahresansicht', month: 'Monatsansicht', week: 'Wochenansicht' };

function switchView(view) {
    currentView = view;
    localStorage.setItem('kalender_view', view);
    applyViewState();
    setViewToggleLabel();
    if (view === 'week') refreshWeekView();
    if (view === 'month') refreshMonthView();
}

function applyViewState() {
    const yearView = document.getElementById('year-view');
    const weekView = document.getElementById('week-view');
    const monthView = document.getElementById('month-view');
    if (!yearView || !weekView) return;
    yearView.classList.toggle('hidden', currentView !== 'year');
    weekView.classList.toggle('hidden', currentView !== 'week');
    if (monthView) monthView.classList.toggle('hidden', currentView !== 'month');
}

function setViewToggleLabel() {
    const btn = document.getElementById('btn-view-toggle');
    if (!btn) return;
    // Button zeigt die NÄCHSTE Ansicht im Zyklus.
    const idx = VIEW_CYCLE.indexOf(currentView);
    const next = VIEW_CYCLE[(idx + 1) % VIEW_CYCLE.length];
    btn.textContent = VIEW_LABEL[next];
}

document.getElementById('btn-view-toggle')?.addEventListener('click', () => {
    const idx = VIEW_CYCLE.indexOf(currentView);
    switchView(VIEW_CYCLE[(idx + 1) % VIEW_CYCLE.length]);
});

// --- Week View ---
function getMonday(d) {
    const dt = new Date(d);
    const day = dt.getDay();
    const diff = (day === 0 ? -6 : 1) - day;
    dt.setDate(dt.getDate() + diff);
    dt.setHours(0, 0, 0, 0);
    return dt;
}

function initWeekView() {
    weekViewDate = getMonday(new Date());
    document.getElementById('week-prev')?.addEventListener('click', () => navigateWeek(-1));
    document.getElementById('week-next')?.addEventListener('click', () => navigateWeek(1));
    document.getElementById('week-today')?.addEventListener('click', weekGoToday);
}

function navigateWeek(direction) {
    weekViewDate = new Date(weekViewDate.getTime() + direction * 7 * 86400000);
    refreshWeekView();
}

function weekGoToday() {
    weekViewDate = getMonday(new Date());
    refreshWeekView();
}

async function refreshWeekView() {
    if (!weekViewDate) weekViewDate = getMonday(new Date());
    const weekStart = formatDate(weekViewDate);
    const weekEnd = formatDate(new Date(weekViewDate.getTime() + 6 * 86400000));

    // Update label
    const weekNum = getISOWeekNumber(weekViewDate);
    const startLabel = formatDateDE(weekViewDate);
    const endDate = new Date(weekViewDate.getTime() + 6 * 86400000);
    const endLabel = formatDateDE(endDate);
    const labelEl = document.getElementById('week-label');
    if (labelEl) labelEl.textContent = `KW ${weekNum} · ${startLabel} – ${endLabel}`;

    // Fetch data in parallel
    const startISO = weekViewDate.toISOString();
    const endISO = new Date(weekViewDate.getTime() + 7 * 86400000 - 1).toISOString();
    const params = new URLSearchParams({ start: startISO, end: endISO });

    try {
        const [events, sessions, dayTypes, todos] = await Promise.all([
            apiJson(`/api/events?${params}`),
            apiJson(`/api/habits/sessions/calendar?${params}`).catch(() => []),
            apiJson(`/api/day-type/week-schedule?week_start=${weekStart}`).catch(() => []),
            apiJson(`/api/todos?week_start=${weekStart}&include_completed=true`).catch(() => []),
        ]);
        weekDayTypes = dayTypes;
        weekTodos = todos;
        renderWeekView(events, sessions, dayTypes, todos);
    } catch (err) {
        console.error('Week view refresh failed:', err);
    }
}

function getISOWeekNumber(d) {
    const dt = new Date(Date.UTC(d.getFullYear(), d.getMonth(), d.getDate()));
    dt.setUTCDate(dt.getUTCDate() + 4 - (dt.getUTCDay() || 7));
    const yearStart = new Date(Date.UTC(dt.getUTCFullYear(), 0, 1));
    return Math.ceil(((dt - yearStart) / 86400000 + 1) / 7);
}

function formatDateDE(d) {
    const day = d.getDate();
    const months = ['Jan', 'Feb', 'Mar', 'Apr', 'Mai', 'Jun', 'Jul', 'Aug', 'Sep', 'Okt', 'Nov', 'Dez'];
    return `${day}. ${months[d.getMonth()]} ${d.getFullYear()}`;
}

// Module-level state for now-line updates
let weekHourStart = 6;
let weekHourPx = 48;
let weekHourEnd = 22;

function buildWeekTimedEventSegments(day) {
    const dayStart = new Date(day.date);
    dayStart.setHours(0, 0, 0, 0);
    const dayEnd = new Date(dayStart.getTime() + 86400000);

    const segments = day.events
        .filter(evt => !evt.all_day)
        .map(evt => {
            const segmentStart = evt._start > dayStart ? evt._start : dayStart;
            const segmentEnd = evt._end < dayEnd ? evt._end : dayEnd;
            if (segmentEnd <= segmentStart) return null;
            return {
                evt,
                segmentStart,
                segmentEnd,
                lane: 0,
                columns: 1,
            };
        })
        .filter(Boolean)
        .sort((a, b) => {
            const startDiff = a.segmentStart - b.segmentStart;
            if (startDiff !== 0) return startDiff;
            return a.segmentEnd - b.segmentEnd;
        });

    const groups = [];
    let currentGroup = [];
    let currentGroupEnd = null;

    for (const segment of segments) {
        if (!currentGroup.length || segment.segmentStart < currentGroupEnd) {
            currentGroup.push(segment);
            if (!currentGroupEnd || segment.segmentEnd > currentGroupEnd) {
                currentGroupEnd = segment.segmentEnd;
            }
            continue;
        }
        groups.push(currentGroup);
        currentGroup = [segment];
        currentGroupEnd = segment.segmentEnd;
    }

    if (currentGroup.length) {
        groups.push(currentGroup);
    }

    for (const group of groups) {
        const active = [];
        let columnCount = 0;

        for (const segment of group) {
            for (let i = active.length - 1; i >= 0; i -= 1) {
                if (active[i].segmentEnd <= segment.segmentStart) {
                    active.splice(i, 1);
                }
            }

            const usedLanes = new Set(active.map(item => item.lane));
            let lane = 0;
            while (usedLanes.has(lane)) {
                lane += 1;
            }

            segment.lane = lane;
            active.push(segment);
            columnCount = Math.max(columnCount, lane + 1);
        }

        for (const segment of group) {
            segment.columns = columnCount || 1;
        }
    }

    return segments;
}

function formatWeekSegmentTime(segmentStart, segmentEnd, dayStart, dayEnd) {
    const startLabel = segmentStart.getTime() === dayStart.getTime() ? '00:00' : fmtTime(segmentStart);
    const endLabel = segmentEnd.getTime() === dayEnd.getTime() ? '24:00' : fmtTime(segmentEnd);
    return `${startLabel} - ${endLabel}`;
}

function renderWeekView(events, sessions, dayTypes, todos) {
    const WD_NAMES = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So'];
    const WD_FULL = ['Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag', 'Sonntag'];
    const todayStr = formatDate(new Date());
    const HOUR_PX = 48;

    // Parse time string "HH:MM" to total minutes
    function parseTimeMin(t) {
        const [h, m] = t.split(':').map(Number);
        return h * 60 + m;
    }

    // Build day data with schedule info from week-schedule endpoint
    const days = [];
    for (let i = 0; i < 7; i++) {
        const d = new Date(weekViewDate.getTime() + i * 86400000);
        const dateStr = formatDate(d);
        const dt = dayTypes.find(dt => dt.date === dateStr);
        days.push({
            date: d,
            dateStr,
            dayName: WD_NAMES[i],
            dayFull: WD_FULL[i],
            dayType: dt ? dt.type : 'frei',
            wake: dt ? dt.wake : '08:00',
            bed: dt ? dt.bed : '00:00',
            buffer_start: dt ? dt.buffer_start : '22:00',
            block: dt ? dt.block : null,
            isToday: dateStr === todayStr,
            events: [],
            sessions: [],
            todos: [],
        });
    }

    // Compute dynamic HOUR_START / HOUR_END from wake/bed times
    let minWake = 24;
    let maxBed = 0;
    for (const day of days) {
        const wakeMin = parseTimeMin(day.wake);
        const wakeH = Math.floor(wakeMin / 60);
        if (wakeH < minWake) minWake = wakeH;

        const bedMin = parseTimeMin(day.bed);
        // Midnight (0:00) or past midnight -> treat as 24 / 25
        let bedH = bedMin === 0 ? 24 : (bedMin <= 720 ? Math.ceil(bedMin / 60) + 24 : Math.ceil(bedMin / 60));
        if (bedMin > 720) bedH = Math.ceil(bedMin / 60); // normal evening times
        if (bedH > maxBed) maxBed = bedH;
    }
    const HOUR_START = Math.max(0, minWake);
    const HOUR_END = Math.min(25, maxBed);

    // Store for now-line and external use
    weekHourStart = HOUR_START;
    weekHourPx = HOUR_PX;
    weekHourEnd = HOUR_END;

    // Assign events to days
    for (const evt of events) {
        if (DAYTYPE_CAL_IDS.has(evt.calendar_id)) continue;
        const cal = calendars.find(c => c.id === evt.calendar_id);
        const color = cal ? cal.color : '#3788d8';
        const evtStart = new Date(evt.start);
        const evtEnd = evt.end ? new Date(evt.end) : evtStart;
        for (const day of days) {
            const dayStart = new Date(day.date);
            const dayEnd = new Date(day.date.getTime() + 86400000);
            if (evtStart < dayEnd && evtEnd > dayStart) {
                day.events.push({ ...evt, color, _start: evtStart, _end: evtEnd });
            }
        }
    }

    // Assign habit sessions to days
    for (const s of sessions) {
        const sStart = new Date(s.start);
        const sDate = formatDate(sStart);
        const day = days.find(d => d.dateStr === sDate);
        if (day) day.sessions.push({ ...s, _start: sStart, _end: s.end ? new Date(s.end) : sStart });
    }

    // Assign todos to days
    for (const todo of todos) {
        if (!todo.due_date) continue;
        const day = days.find(d => d.dateStr === todo.due_date);
        if (day) day.todos.push(todo);
    }
    const noDueTodos = todos.filter(t => !t.due_date);
    const todayDay = days.find(d => d.isToday) || days[0];
    if (todayDay) todayDay.todos.push(...noDueTodos);

    // -- Render Todos Section --
    const todosSection = document.getElementById('week-todos-section');
    const hasTodos = days.some(d => d.todos.length > 0);
    if (!hasTodos) {
        if (todosSection) todosSection.innerHTML = '';
    } else {
        let todosHtml = '<div class="week-todos-grid">';
        todosHtml += '<div class="week-todos-gutter"></div>';
        for (const day of days) {
            const todayCls = day.isToday ? ' week-day-today' : '';
            todosHtml += `<div class="week-todo-col${todayCls}" data-date="${day.dateStr}">`;
            for (const todo of day.todos) {
                const prioCls = `todo-prio-${todo.priority}`;
                const checkedAttr = todo.completed ? ' checked' : '';
                const completedCls = todo.completed ? ' todo-done' : '';
                const recurIcon = todo.recurrence ? '<span class="todo-recur-icon" title="Wiederkehrend">&#8635;</span>' : '';
                const timeStr = todo.due_time ? `<span class="todo-time-hint">${todo.due_time}</span>` : '';
                todosHtml += `<div class="week-todo-item ${prioCls}${completedCls}" data-todo-id="${todo.id}">
                    <input type="checkbox" class="todo-check" data-todo-id="${todo.id}"${checkedAttr}>
                    <span class="todo-title">${escapeHtml(todo.title)}</span>
                    ${timeStr}${recurIcon}
                </div>`;
            }
            todosHtml += `<button class="week-todo-add" data-date="${day.dateStr}" title="To-Do hinzufuegen">+</button>`;
            todosHtml += '</div>';
        }
        todosHtml += '</div>';
        if (todosSection) todosSection.innerHTML = todosHtml;
    }

    // -- Render Week Grid --
    const grid = document.getElementById('week-grid');
    let gridHtml = '';

    // Header row with clickable daytype headers
    gridHtml += '<div class="week-header-row">';
    gridHtml += '<div class="week-time-gutter"></div>';
    for (const day of days) {
        const todayCls = day.isToday ? ' week-day-today' : '';
        const dtCls = day.dayType !== 'frei' && day.dayType !== 'wochenende' ? ` badge-${day.dayType}` : '';
        gridHtml += `<div class="week-day-header${todayCls}" data-date="${day.dateStr}" data-daytype="${day.dayType}">
            <span class="week-day-name">${day.dayName}</span>
            <span class="week-day-num${day.isToday ? ' today-circle' : ''}">${day.date.getDate()}</span>
            ${day.dayType !== 'frei' ? `<span class="week-daytype-badge${dtCls}">${day.dayType}</span>` : ''}
        </div>`;
    }
    gridHtml += '</div>';

    // Time grid body
    gridHtml += '<div class="week-body">';
    gridHtml += '<div class="week-time-gutter">';
    for (let h = HOUR_START; h < HOUR_END; h++) {
        const displayH = h % 24;
        const isMajor = displayH % 3 === 0;
        const majorCls = isMajor ? ' label-major' : '';
        gridHtml += `<div class="week-time-label${majorCls}" style="height:${HOUR_PX}px">${String(displayH).padStart(2, '0')}:00</div>`;
    }
    gridHtml += '</div>';

    for (const day of days) {
        const todayCls = day.isToday ? ' week-day-today' : '';
        gridHtml += `<div class="week-day-col${todayCls}" data-date="${day.dateStr}">`;
        const dayStart = new Date(day.date);
        dayStart.setHours(0, 0, 0, 0);
        const dayEnd = new Date(dayStart.getTime() + 86400000);

        // Hour lines
        for (let h = HOUR_START; h < HOUR_END; h++) {
            const displayH = h % 24;
            const isMajor = displayH % 3 === 0;
            const majorCls = isMajor ? ' hour-major' : '';
            gridHtml += `<div class="week-hour-line${majorCls}" data-hour="${displayH}" style="height:${HOUR_PX}px"></div>`;
        }

        // Daytype background block (Arbeit/Schule)
        if (day.block) {
            const blockStartMin = parseTimeMin(day.block.start);
            const blockEndMin = parseTimeMin(day.block.end);
            const top = Math.max(0, (blockStartMin - HOUR_START * 60)) * (HOUR_PX / 60);
            const height = Math.max(0, (blockEndMin - blockStartMin)) * (HOUR_PX / 60);
            const bgCls = day.dayType === 'arbeit' ? 'daytype-bg-arbeit' : 'daytype-bg-schule';
            const label = day.dayType === 'arbeit' ? 'Arbeit' : 'Schule';
            gridHtml += `<div class="week-daytype-block ${bgCls}" style="top:${top}px; height:${height}px">
                <span class="week-daytype-block-label">${label}</span>
            </div>`;
        }

        // Buffer zone
        const bufStartMin = parseTimeMin(day.buffer_start);
        let bedMin = parseTimeMin(day.bed);
        if (bedMin === 0) bedMin = 24 * 60;
        else if (bedMin <= 720) bedMin += 24 * 60;
        const bufTop = Math.max(0, (bufStartMin - HOUR_START * 60)) * (HOUR_PX / 60);
        const bufHeight = Math.max(0, (bedMin - bufStartMin)) * (HOUR_PX / 60);
        if (bufHeight > 0) {
            gridHtml += `<div class="week-buffer-zone" style="top:${bufTop}px; height:${bufHeight}px"></div>`;
        }

        // Event blocks
        for (const evt of day.events) {
            if (evt.all_day) {
                gridHtml += `<div class="week-allday-block" style="background:${evt.color}40; border-left: 3px solid ${evt.color}" data-event-id="${evt.id}">
                    <span>${escapeHtml(evt.title)}</span>
                </div>`;
            }
        }

        const timedSegments = buildWeekTimedEventSegments(day);
        for (const segment of timedSegments) {
            const segmentStartMin = segment.segmentStart.getHours() * 60 + segment.segmentStart.getMinutes();
            const segmentEndMin = segment.segmentEnd.getTime() === dayEnd.getTime()
                ? 24 * 60
                : segment.segmentEnd.getHours() * 60 + segment.segmentEnd.getMinutes();
            const top = Math.max(0, (segmentStartMin - HOUR_START * 60)) * (HOUR_PX / 60);
            const height = Math.max(HOUR_PX / 4, (segmentEndMin - segmentStartMin) * (HOUR_PX / 60));
            const laneWidth = 100 / segment.columns;
            const leftPct = segment.lane * laneWidth;
            const isPastEvt = segment.segmentEnd < new Date();
            const pastEvtCls = isPastEvt ? ' is-past' : '';
            const segmentTime = formatWeekSegmentTime(segment.segmentStart, segment.segmentEnd, dayStart, dayEnd);
            gridHtml += `<div class="week-event-block${pastEvtCls}" style="--week-event-left:${leftPct}%; --week-event-width:${laneWidth}%; top:${top}px; height:${height}px; background:${segment.evt.color}30; border-left:3px solid ${segment.evt.color}" data-event-id="${segment.evt.id}">
                <div class="week-event-title">${escapeHtml(segment.evt.title)}</div>
                <div class="week-event-time">${segmentTime}</div>
            </div>`;
        }

        // Habit session blocks
        for (const s of day.sessions) {
            const startMin = s._start.getHours() * 60 + s._start.getMinutes();
            const endMin = s._end.getHours() * 60 + s._end.getMinutes();
            const top = Math.max(0, (startMin - HOUR_START * 60)) * (HOUR_PX / 60);
            const height = Math.max(HOUR_PX / 4, (endMin - startMin) * (HOUR_PX / 60));
            const color = s.backgroundColor || '#4a9eff';
            const statusCls = s.extendedProps?.status || 'pending';
            const isPastSes = s._end < new Date();
            const pastSesCls = isPastSes ? ' is-past' : '';
            gridHtml += `<div class="week-session-block status-${statusCls}${pastSesCls}" style="top:${top}px; height:${height}px; border-color:${color}" data-session-date="${day.dateStr}">
                <div class="week-session-title">${escapeHtml(s.title || '')}</div>
                <div class="week-session-time">${fmtTime(s._start)} - ${fmtTime(s._end)}</div>
            </div>`;
        }

        gridHtml += '</div>';
    }

    gridHtml += '</div>';
    if (grid) grid.innerHTML = gridHtml;

    // Wire event interactions
    wireWeekViewEvents(days);
    wireWeekDaytypeDropdowns(days);
    updateNowLine();

    // Scroll to current time
    const wrapper = document.querySelector('.week-grid-wrapper');
    if (wrapper) {
        const now = new Date();
        const nowMin = now.getHours() * 60 + now.getMinutes();
        const scrollTop = Math.max(0, (nowMin - HOUR_START * 60 - 60) * (HOUR_PX / 60));
        wrapper.scrollTop = scrollTop;
    }

    // Start now-line updates
    if (weekNowLineInterval) clearInterval(weekNowLineInterval);
    weekNowLineInterval = setInterval(() => updateNowLine(), 60000);
}

function fmtTime(d) {
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

// --- Daytype Dropdown in Week Header ---
function wireWeekDaytypeDropdowns(days) {
    document.querySelectorAll('.week-day-header[data-date]').forEach(header => {
        header.style.cursor = 'pointer';
        header.addEventListener('click', (e) => {
            e.stopPropagation();
            // Close any existing dropdown
            document.querySelectorAll('.week-daytype-dropdown').forEach(d => d.remove());

            const dateStr = header.dataset.date;
            const currentType = header.dataset.daytype || 'frei';

            const dropdown = document.createElement('div');
            dropdown.className = 'week-daytype-dropdown';

            const types = [
                { key: 'arbeit', label: 'Arbeit', cls: 'daytype-arbeit' },
                { key: 'schule', label: 'Schule', cls: 'daytype-schule' },
                { key: 'urlaub', label: 'Urlaub', cls: 'daytype-urlaub' },
                { key: 'krank', label: 'Krank', cls: 'daytype-krank' },
            ];

            for (const t of types) {
                const btn = document.createElement('button');
                btn.className = `wdt-btn ${t.cls}`;
                btn.textContent = t.label;
                if (currentType === t.key) btn.classList.add('active');
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    dropdown.remove();
                    try {
                        await apiJson(`/api/day-type/set?date=${dateStr}&type=${t.key}`, { method: 'POST' });
                        refreshWeekView();
                        refreshCalendar();
                        loadFocusStrip();
                        loadDashboardWidget();
                    } catch (err) {
                        showToast(err.message, 'error');
                    }
                });
                dropdown.appendChild(btn);
            }

            header.style.position = 'relative';
            header.appendChild(dropdown);

            // Close on outside click
            const closeHandler = (e) => {
                if (!dropdown.contains(e.target) && e.target !== header) {
                    dropdown.remove();
                    document.removeEventListener('click', closeHandler);
                }
            };
            setTimeout(() => document.addEventListener('click', closeHandler), 0);
        });
    });
}

function updateNowLine() {
    // Remove existing
    document.querySelectorAll('.week-now-line').forEach(el => el.remove());
    const todayStr = formatDate(new Date());
    const todayCol = document.querySelector(`.week-day-col[data-date="${todayStr}"]`);
    if (!todayCol) return;
    const now = new Date();
    const nowMin = now.getHours() * 60 + now.getMinutes();
    const top = (nowMin - weekHourStart * 60) * (weekHourPx / 60);
    if (top < 0 || top > (weekHourEnd - weekHourStart) * weekHourPx) return;
    const line = document.createElement('div');
    line.className = 'week-now-line';
    line.style.top = top + 'px';
    todayCol.appendChild(line);
}

function wireWeekViewEvents(days) {
    // Todo checkboxes
    document.querySelectorAll('.todo-check').forEach(cb => {
        cb.addEventListener('change', async (e) => {
            e.stopPropagation();
            const todoId = cb.dataset.todoId;
            try {
                if (cb.checked) {
                    await apiJson(`/api/todos/${todoId}/complete`, { method: 'POST' });
                } else {
                    await apiJson(`/api/todos/${todoId}/uncomplete`, { method: 'POST' });
                }
                refreshCalendar();
                loadFocusStrip();
                refreshWeekView();
                loadTodosWidget();
            } catch (err) {
                showToast(err.message, 'error');
                cb.checked = !cb.checked;
            }
        });
    });

    // Todo item click -> edit
    document.querySelectorAll('.week-todo-item').forEach(item => {
        item.addEventListener('click', (e) => {
            if (e.target.classList.contains('todo-check')) return;
            const todoId = item.dataset.todoId;
            const todo = weekTodos.find(t => t.id === todoId);
            if (todo) openEditTodo(todo);
        });
    });

    // Todo add button
    document.querySelectorAll('.week-todo-add').forEach(btn => {
        btn.addEventListener('click', () => {
            openNewTodo(btn.dataset.date);
        });
    });

    // Event block click
    document.querySelectorAll('.week-event-block, .week-allday-block').forEach(block => {
        block.addEventListener('click', () => {
            const evtId = block.dataset.eventId;
            const evt = cachedEvents.find(e => String(e.id) === String(evtId));
            if (evt && !evt.extendedProps?.is_daytype) openEditEvent(evt);
        });
    });

    // Session block click -> day detail
    document.querySelectorAll('.week-session-block').forEach(block => {
        block.addEventListener('click', () => {
            const dateStr = block.dataset.sessionDate;
            const dayCell = document.querySelector(`.day-cell[data-date="${dateStr}"]`);
            if (dayCell) showDayDetail(dateStr, dayCell);
        });
    });

    // Empty hour click -> create event
    document.querySelectorAll('.week-hour-line').forEach(line => {
        line.addEventListener('click', (e) => {
            if (e.target !== line) return;
            const col = line.closest('.week-day-col');
            if (!col) return;
            const dateStr = col.dataset.date;
            const hour = parseInt(line.dataset.hour);
            const startTime = `${String(hour).padStart(2, '0')}:00`;
            const endTime = `${String(hour + 1).padStart(2, '0')}:00`;
            openNewEventWithTime(dateStr, startTime, endTime);
        });
    });
}

function openNewEventWithTime(dateStr, startTime, endTime) {
    document.getElementById('event-modal-title').textContent = 'Neues Event';
    document.getElementById('evt-id').value = '';
    document.getElementById('evt-title').value = '';
    document.getElementById('evt-description').value = '';
    setzeTerminOrt(null, null, null);
    document.getElementById('evt-allday').checked = false;
    document.getElementById('evt-start').value = dateStr;
    document.getElementById('evt-end').value = dateStr;
    document.getElementById('evt-start-time').value = startTime;
    document.getElementById('evt-end-time').value = endTime;
    setRecurrenceSelect('');
    document.getElementById('evt-reminder').value = '';
    eventModalContext = { seriesId: null, occDate: null, isInstance: false };
    toggleTimeFields(false);
    document.getElementById('btn-delete-event').classList.add('hidden');
    openModal('event-modal');
}

// --- Todo CRUD UI ---
let allTodos = [];

document.getElementById('btn-todos')?.addEventListener('click', openTodosList);

async function openTodosList() {
    await loadProjects();
    const showCompleted = document.getElementById('todos-show-completed')?.checked || false;
    allTodos = await apiJson(`/api/todos?include_completed=${showCompleted}`);
    renderTodosList();
    openModal('todos-modal');
}

document.getElementById('todos-show-completed')?.addEventListener('change', async () => {
    const showCompleted = document.getElementById('todos-show-completed').checked;
    allTodos = await apiJson(`/api/todos?include_completed=${showCompleted}`);
    renderTodosList();
});

function renderTodosList() {
    const container = document.getElementById('todos-list');
    if (allTodos.length === 0) {
        container.innerHTML = '<div class="contacts-empty">Keine To-Dos vorhanden.</div>';
        return;
    }
    const prioOrder = { dringend: 0, hoch: 1, mittel: 2, niedrig: 3 };
    const sorted = [...allTodos].sort((a, b) => {
        const pa = prioOrder[a.priority] ?? 2;
        const pb = prioOrder[b.priority] ?? 2;
        if (pa !== pb) return pa - pb;
        if (a.due_date && b.due_date) return a.due_date.localeCompare(b.due_date);
        if (a.due_date) return -1;
        if (b.due_date) return 1;
        return 0;
    });
    const prioLabels = { dringend: 'Dringend', hoch: 'Hoch', mittel: 'Mittel', niedrig: 'Niedrig' };
    container.innerHTML = sorted.map(t => {
        const prioCls = `todo-prio-${t.priority}`;
        const completedCls = t.completed ? ' todo-list-done' : '';
        const recurIcon = t.recurrence ? ' &#8635;' : '';
        const reminderHint = t.recurrence ? ' · Erinnerung im Kalender' : '';
        const dueStr = t.due_date ? new Date(t.due_date + 'T00:00:00').toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) : '';
        const timeStr = t.due_time ? ` ${t.due_time}` : '';
        return `<div class="todo-list-item ${prioCls}${completedCls}" data-todo-id="${t.id}">
            <div class="todo-list-info">
                <span class="todo-list-title">${escapeHtml(t.title)}${recurIcon}</span>
                <span class="todo-list-detail">${prioLabels[t.priority]}${dueStr ? ' · ' + dueStr + timeStr : ''}${reminderHint}</span>
            </div>
            <div class="todo-list-actions">
                <button class="btn-delete-todo-list" data-todo-id="${t.id}" title="Loeschen">&#128465;</button>
            </div>
        </div>`;
    }).join('');

    // Wire click to edit
    container.querySelectorAll('.todo-list-item').forEach(item => {
        item.addEventListener('click', (e) => {
            if (e.target.closest('.btn-delete-todo-list')) return;
            const todo = allTodos.find(t => t.id === item.dataset.todoId);
            if (todo) {
                closeModal('todos-modal');
                openEditTodo(todo);
            }
        });
    });
    container.querySelectorAll('.btn-delete-todo-list').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            e.stopPropagation();
            if (!confirm('To-Do loeschen?')) return;
            try {
                await apiDelete(`/api/todos/${btn.dataset.todoId}`);
                allTodos = allTodos.filter(t => t.id !== btn.dataset.todoId);
                renderTodosList();
                refreshCalendar();
                loadFocusStrip();
                loadTodosWidget();
                if (currentView === 'week') refreshWeekView();
            } catch (err) {
                showToast(err.message, 'error');
            }
        });
    });
}

document.getElementById('btn-new-todo')?.addEventListener('click', () => {
    closeModal('todos-modal');
    openNewTodo();
});

document.querySelectorAll('.todo-template-btn').forEach(btn => {
    btn.addEventListener('click', () => {
        document.getElementById('todo-title').value = btn.dataset.title || '';
        document.getElementById('todo-recurrence').value = btn.dataset.recurrence || '';
        document.getElementById('todo-priority').value = btn.dataset.priority || 'mittel';
        if (!document.getElementById('todo-due-date').value) {
            document.getElementById('todo-due-date').value = formatDate(new Date());
        }
    });
});

function _todayStr() {
    const t = new Date();
    return `${t.getFullYear()}-${String(t.getMonth() + 1).padStart(2, '0')}-${String(t.getDate()).padStart(2, '0')}`;
}

async function openNewTodo(prefillDate) {
    await loadProjects();
    populateTodoProjectDropdown('');
    document.getElementById('todo-modal-title').textContent = 'Neues To-Do';
    document.getElementById('todo-id').value = '';
    document.getElementById('todo-title').value = '';
    document.getElementById('todo-description').value = '';
    document.getElementById('todo-priority').value = 'mittel';
    document.getElementById('todo-recurrence').value = '';
    document.getElementById('todo-due-date').value = prefillDate || '';
    document.getElementById('todo-due-time').value = '';
    document.getElementById('todo-project').value = '';
    populateTodoGoalDropdown(prefillDate || _todayStr(), '');
    document.getElementById('btn-delete-todo').classList.add('hidden');
    openModal('todo-modal');
}

function openEditTodo(todo) {
    loadProjects().then(() => {
        populateTodoProjectDropdown(todo.project_id || '');
    });
    document.getElementById('todo-modal-title').textContent = 'To-Do bearbeiten';
    document.getElementById('todo-id').value = todo.id;
    document.getElementById('todo-title').value = todo.title;
    document.getElementById('todo-description').value = todo.description || '';
    document.getElementById('todo-priority').value = todo.priority;
    document.getElementById('todo-recurrence').value = todo.recurrence || '';
    document.getElementById('todo-due-date').value = todo.due_date || '';
    document.getElementById('todo-due-time').value = todo.due_time || '';
    document.getElementById('todo-project').value = todo.project_id || '';
    populateTodoGoalDropdown(todo.due_date || todo.planned_date || _todayStr(), todo.goal_id || '');
    document.getElementById('btn-delete-todo').classList.remove('hidden');
    openModal('todo-modal');
}

async function populateTodoGoalDropdown(dateStr, selectedId) {
    const sel = document.getElementById('todo-goal');
    if (!sel) return;
    sel.innerHTML = '<option value="">-- Kein Ziel --</option>';
    let goals = [];
    try { goals = await apiJson(`/api/goals?date=${dateStr}`); } catch { goals = []; }
    // bereits verknuepftes Ziel mit anderem Datum trotzdem anbieten
    if (selectedId && !goals.some(g => g.id === selectedId)) {
        try {
            const g = await apiJson(`/api/goals/${selectedId}`);
            if (g && g.id) goals.unshift(g);
        } catch {}
    }
    for (const g of goals) {
        const opt = document.createElement('option');
        opt.value = g.id;
        opt.textContent = `[${g.priority}] ${g.title}`;
        if (g.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    }
}

function populateTodoProjectDropdown(selectedId) {
    const sel = document.getElementById('todo-project');
    sel.innerHTML = '<option value="">-- Kein Projekt --</option>';
    for (const p of projects) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = (p.icon ? p.icon + ' ' : '') + p.name;
        if (p.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    }
}

document.getElementById('todo-form')?.addEventListener('submit', async (e) => {
    e.preventDefault();
    const id = document.getElementById('todo-id').value;
    const payload = {
        title: document.getElementById('todo-title').value,
        description: document.getElementById('todo-description').value || null,
        priority: document.getElementById('todo-priority').value,
        recurrence: document.getElementById('todo-recurrence').value || null,
        due_date: document.getElementById('todo-due-date').value || null,
        due_time: document.getElementById('todo-due-time').value || null,
        project_id: document.getElementById('todo-project').value || null,
        goal_id: document.getElementById('todo-goal').value || null,
    };
    try {
        if (id) {
            await apiJson(`/api/todos/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
        } else {
            await apiJson('/api/todos', { method: 'POST', body: JSON.stringify(payload) });
        }
        closeModal('todo-modal');
        refreshCalendar();
        loadFocusStrip();
        loadTodosWidget();
        if (currentView === 'week') refreshWeekView();
        showToast(id ? 'To-Do aktualisiert' : 'To-Do erstellt');
    } catch (err) {
        showToast(err.message, 'error');
    }
});

document.getElementById('btn-delete-todo')?.addEventListener('click', async () => {
    const id = document.getElementById('todo-id').value;
    if (!id) return;
    if (!confirm('To-Do loeschen?')) return;
    try {
        await apiDelete(`/api/todos/${id}`);
        closeModal('todo-modal');
        refreshCalendar();
        loadFocusStrip();
        loadTodosWidget();
        if (currentView === 'week') refreshWeekView();
        showToast('To-Do geloescht');
    } catch (err) {
        showToast(err.message, 'error');
    }
});

// --- Browser Notifications ---
function initNotifications() {
    if (!('Notification' in window)) return;
    if (Notification.permission === 'default') {
        Notification.requestPermission();
    }
}

function startNotificationPolling() {
    setInterval(async () => {
        if (Notification.permission !== 'granted') return;
        try {
            const sessions = await apiJson('/api/habits/sessions/upcoming');
            for (const s of sessions) {
                if (notifiedSessionIds.has(s.id)) continue;
                notifiedSessionIds.add(s.id);
                showSessionNotification(s);
            }
        } catch {}
    }, 60 * 1000);
}

function showSessionNotification(session) {
    const startTime = new Date(session.start);
    const timeStr = `${String(startTime.getHours()).padStart(2,'0')}:${String(startTime.getMinutes()).padStart(2,'0')}`;
    new Notification(session.habit_name || 'Habit Session', {
        body: `Session um ${timeStr} steht an`,
        icon: '/css/style.css',
        tag: `habit-session-${session.id}`,
    });
}

// --- Startup ---
(async () => {
    try {
        const status = await apiJson('/api/auth/status');
        if (status.authenticated) {
            await initApp();
            return;
        }
    } catch {}
    showLogin();
})();
