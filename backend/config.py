import os
import secrets


class Settings:
    KALENDER_PASSWORD: str = os.environ.get("KALENDER_PASSWORD", "")
    # SECRET_KEY: aus env, sonst leer -> beim Start persistent aus der DB geladen/
    # erzeugt (_ensure_secret_key), damit JWTs einen Container-Neustart überleben.
    SECRET_KEY: str = os.environ.get("SECRET_KEY", "")
    # Session-Cookie nur über HTTPS ausliefern. Default False, damit der direkte
    # HTTP-LAN-Zugang (host:8085) nicht bricht; hinter HTTPS-Edge auf 1 setzen.
    COOKIE_SECURE: bool = os.environ.get("COOKIE_SECURE", "0") not in ("0", "false", "False", "")
    FEED_TOKEN: str = os.environ.get("FEED_TOKEN", "")
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///data/kalender.db")
    NTFY_URL: str = os.environ.get("NTFY_URL", "").rstrip("/")
    NTFY_TOPIC: str = os.environ.get("NTFY_TOPIC", "")
    NTFY_TOKEN: str = os.environ.get("NTFY_TOKEN", "")
    SESSION_NOTIFY_LEAD_MINUTES_START: int = int(
        os.environ.get("SESSION_NOTIFY_LEAD_MINUTES_START", "10")
    )
    SESSION_NOTIFY_LEAD_MINUTES_END: int = int(
        os.environ.get("SESSION_NOTIFY_LEAD_MINUTES_END", "5")
    )
    # Multi-Tenant: better-auth-sub, dem alle Bestandsdaten gehoeren und auf den
    # headerlose Aufrufe zurueckfallen (natives Frontend/JWT, Feed-Token, iCal,
    # KG-internal, mobile, Notification-Runner). Der Saganta-Weg stempelt pro
    # Request X-Saganta-Sub (kalender-bff/app-proxy).
    #
    # Bis zum 2026-09-06 stand hier die Kennung eines konkreten Menschen als
    # Vorbelegung. Sie gehoert nicht in ein Repo, das veroeffentlicht werden
    # soll, und ein Selbsthoster hat ohnehin eine andere. Seither Konfiguration,
    # einzutragen mit saganta/scripts/owner-kennung-eintragen.sh.
    #
    # ⚠️ Leer heisst hier mehr als bei lager/mealprep/fitness. Am headerlosen
    # Pfad haengt die CORE-Kette: Home Assistant fragt /api/day-type/today ab,
    # daran haengt das 05:35-Weckfenster. Leer waere fail-closed, also ein
    # Tagestyp ohne Datengrundlage. Deshalb leitet backend/mandant_ableiten.py
    # beim Start ab, solange die Daten genau einen Mandanten tragen, und warnt.
    DEFAULT_OWNER_SUB: str = os.environ.get("DEFAULT_OWNER_SUB", "")
    # Echtheitsprüfung des X-Saganta-Sub-Headers (siehe backend/tenant_auth.py).
    # Leer = wie bisher (Header wird ungeprüft übernommen, keine Warnungen).
    # Gesetzt + ENFORCE=0 = Beobachtungsphase (akzeptieren, protokollieren).
    # Gesetzt + ENFORCE=1 = 401 für unsignierte Header.
    # ⚠️ Eigenes Geheimnis vergeben, NICHT SECRET_KEY wiederverwenden, sonst
    # wandert der JWT-Signierschlüssel in jeden Absender.
    KALENDER_TENANT_SECRET: str = os.environ.get("KALENDER_TENANT_SECRET", "")
    TENANT_HEADER_ENFORCE: bool = os.environ.get("TENANT_HEADER_ENFORCE", "0") not in (
        "0", "false", "False", "",
    )
    # Unbekannte Query-Parameter an den geschuetzten Routen (siehe
    # backend/parameter_wache.py). Default 0 = protokollieren und ignorieren
    # (Beobachtungsphase), 1 = mit 400 ablehnen. Erst scharf schalten, wenn die
    # Protokolle ueber mehrere Tage still bleiben, sonst bricht ein Konsument,
    # der sich seit Monaten unbemerkt vertippt.
    STRICT_QUERY_PARAMS: bool = os.environ.get("STRICT_QUERY_PARAMS", "0") not in (
        "0", "false", "False", "",
    )
    # Eingang für gemessene Zeit (backend/zeit_ingest.py), vorgesehen für eine
    # Smartwatch über einen Adapter. Ein Gerät kann kein JWT halten, es bliebe also
    # der headerlose Weg mit Rückfall auf DEFAULT_OWNER_SUB.
    #
    # ⚠️ Leer heißt hier: Eingang AUS, jeder Aufruf 403. Bewusst fail-closed und
    # damit anders als die übrigen optionalen Werte. Ein Schreibpfad, der ohne
    # Nachweis erreichbar ist, nimmt Bewegungsprofile entgegen; genau diese Bauart
    # (headerloser Pfad, geschützt allein durch die Bindung an 127.0.0.1) hätte in
    # der Fitness-App am 2026-09-14 mit dem ersten LAN-Vhost Körperdaten
    # offengelegt. Eigenes Geheimnis vergeben, keines der anderen wiederverwenden.
    ZEIT_INGEST_TOKEN: str = os.environ.get("ZEIT_INGEST_TOKEN", "")
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRATION_HOURS: int = 72
    JWT_REFRESH_GRACE_DAYS: int = int(
        os.environ.get("JWT_REFRESH_GRACE_DAYS", "30")
    )
    # Quick-Capture LLM-Fallback (optional, lokaler node2-Gemma über LLM-Gateway).
    # Default AUS, das regelbasierte Parsing deckt den Alltag ab. Wenn aktiviert,
    # geht der Text an den lokalen Gemma (Homelab), nie an eine Cloud.
    CAPTURE_LLM_ENABLED: bool = os.environ.get("CAPTURE_LLM_ENABLED", "0") not in ("0", "false", "False", "")
    LLM_GATEWAY_URL: str = os.environ.get("LLM_GATEWAY_URL", "")
    LLM_API_KEY: str = os.environ.get("LLM_API_KEY", "")
    LLM_MODEL: str = os.environ.get("LLM_MODEL", "gemma")
    LLM_TIMEOUT: int = int(os.environ.get("LLM_TIMEOUT", "60"))

    # --- Adaptiver Sekretär: Cross-App-Konnektoren (fail-soft, read-only) ---
    # Der Kalender fragt diese lokalen Apps ab, um Vorschläge zu machen
    # (Sport aus Fitness, Einkauf aus Lager/MealPrep). Headerlose Aufrufe fallen
    # bei allen dreien auf DEFAULT_OWNER_SUB zurück. Leerer Wert = Konnektor aus.
    # Adressierung über das geteilte Docker-Netz `cc-apps` per Container-Name; alle
    # drei lauschen intern auf :8000 (vgl. mealprep → http://lager:8000). NICHT die
    # Host-Ports 8094/95/96 (127.0.0.1 wäre aus dem Container der Container selbst).
    FITNESS_URL: str = os.environ.get("FITNESS_URL", "http://fitness:8000").rstrip("/")
    MEALPREP_URL: str = os.environ.get("MEALPREP_URL", "http://mealprep:8000").rstrip("/")
    LAGER_URL: str = os.environ.get("LAGER_URL", "http://lager:8000").rstrip("/")
    CROSS_APP_ENABLED: bool = os.environ.get("CROSS_APP_ENABLED", "1") not in ("0", "false", "False", "")
    CROSS_APP_TIMEOUT: float = float(os.environ.get("CROSS_APP_TIMEOUT", "3.0"))

    # --- Orte, Karte, Wegzeit (alle drei optional) -------------------------
    # Der Kalender laeuft ohne jeden dieser Werte vollstaendig: Adressen bleiben
    # dann Text, es gibt keine Karte und keine Wegzeiten. Das ist Absicht und
    # kein Notbetrieb, denn die Dienste dahinter sind Homelab-Infrastruktur, und
    # wer den Kalender selbst betreibt, hat sie nicht. Jede Funktion prueft ihren
    # eigenen Schalter, damit „Karte ja, Wegzeit nein" moeglich bleibt.
    #
    # ADRESS_URL: dreistufige Adresssuche (Ort, Strasse, Hausnummer).
    #   Erwartet /orte?q=, /strassen?q=&ort=, /nummern?strasse=&q=.
    #   Im Homelab: http://192.0.2.10:8144/adressen
    ADRESS_URL: str = os.environ.get("ADRESS_URL", "").rstrip("/")
    # ROUTING_URL: Wegzeit-Anbieter. ROUTING_ART waehlt das Protokoll.
    #   valhalla = Fuss/Rad/Auto offline (Homelab: http://192.0.2.10:8002)
    #   otp      = zusaetzlich OePNV, braucht Fahrplandaten (noch nicht gebaut)
    ROUTING_URL: str = os.environ.get("ROUTING_URL", "").rstrip("/")
    ROUTING_ART: str = os.environ.get("ROUTING_ART", "valhalla")
    # Vorgabe-Verkehrsmittel, wenn ein Termin keines nennt.
    # pedestrian | bicycle | auto | transit (transit nur mit ROUTING_ART=otp)
    WEG_STANDARD_MITTEL: str = os.environ.get("WEG_STANDARD_MITTEL", "pedestrian")
    # Wie viel Vorlauf vor dem Termin einkalkuliert wird (Jacke, Tuer, Puffer).
    WEG_PUFFER_MINUTEN: int = int(os.environ.get("WEG_PUFFER_MINUTEN", "5"))
    # Wie lange man vom Aufstehen bis zum Losgehen braucht. Bewusst eine grobe,
    # sichtbare Annahme statt einer gelernten Groesse: was jemand morgens
    # braucht, weiss der Kalender nicht, und erfundene Praezision waere hier
    # schlimmer als eine Zahl, die man selbst einstellen kann.
    WEG_VORLAUF_MINUTEN: int = int(os.environ.get("WEG_VORLAUF_MINUTEN", "45"))
    ROUTING_TIMEOUT: float = float(os.environ.get("ROUTING_TIMEOUT", "8.0"))
    # KARTEN_URL: Basis des Kartenportals fuer die kleine Karte im Kontaktbuch.
    #   Wird NUR an den Browser weitergereicht, der Server ruft sie nie auf.
    #   ⚠️ Muss zum Schema der Seite passen: eine http-Karte in einer
    #   https-Seite blockiert der Browser als Mixed Content, ohne dass am
    #   Server etwas auffaellt. Im Homelab: https://karten.home.arpa
    KARTEN_URL: str = os.environ.get("KARTEN_URL", "").rstrip("/")
    CROSS_APP_CACHE_SECONDS: int = int(os.environ.get("CROSS_APP_CACHE_SECONDS", "900"))
    # Proaktive Push-Nudges (Check-in-Prompt morgens, Aktivitäts-Nudge nach Feierabend).
    ASSISTANT_NUDGES_ENABLED: bool = os.environ.get("ASSISTANT_NUDGES_ENABLED", "1") not in ("0", "false", "False", "")
    # Ab welcher Stunde nach Arbeit/Schule der Aktivitäts-Nudge feuern darf.
    ASSISTANT_ACTIVITY_NUDGE_HOUR: int = int(os.environ.get("ASSISTANT_ACTIVITY_NUDGE_HOUR", "16"))
    # Backlog-Verfall: ab so vielen Aufschüben gilt eine Aufgabe als „staut sich" und
    # wird proaktiv eskaliert („X× verschoben: heute oder streichen?").
    BACKLOG_ESCALATE_THRESHOLD: int = int(os.environ.get("BACKLOG_ESCALATE_THRESHOLD", "3"))
    # Slow-lane LLM-Auswertung von Aktivitäts-Randnotizen (Zusammenfassung/Sentiment/Follow-ups).
    # Wiederverwendet die LLM_*-Gateway-Config oben. Default aus → nichts verlässt das Netz.
    FEEDBACK_LLM_ENABLED: bool = os.environ.get("FEEDBACK_LLM_ENABLED", "0") not in ("0", "false", "False", "")
    # Aus LLM-Follow-ups zusätzlich Pool-Todos anlegen (separat, damit der Backlog nicht flutet).
    FEEDBACK_LLM_FOLLOWUP_TODOS: bool = os.environ.get("FEEDBACK_LLM_FOLLOWUP_TODOS", "0") not in ("0", "false", "False", "")


settings = Settings()
