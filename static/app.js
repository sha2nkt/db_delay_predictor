// Each (mode, language) pair has its own URL so Google can index them separately,
// which makes the path — not localStorage — the authority on both.
//   /              /entschaedigung        German
//   /en/           /en/compensation       English
const URL_LANG = /^\/en(\/|$)/.test(location.pathname) ? "en" : "de";
const PAST_PAGE = /^(\/entschaedigung|\/en\/compensation)\/?$/.test(location.pathname);
const PATHS = {
  de: { future: "/", past: "/entschaedigung" },
  en: { future: "/en/", past: "/en/compensation" },
};
const pagePath = (mode, lang = URL_LANG) => PATHS[lang][mode];

const state = {
  from: null,   // {id, name}
  to: null,
  viaOut1: null,  // stopover stations, {id, name} like from/to; Out = outbound
  viaOut2: null,  // leg of the trip, Ret = return leg (round trips only)
  viaRet1: null,
  viaRet2: null,
  journeys: [],
  sort: "departure",
  windowUsed: 7,  // stats window that produced the current results
  dticketUsed: "off",  // D-Ticket mode of the current results: "off" | "all" | "only"
  travellersUsed: [{ count: 1, age: "adult", discount: "none" }],  // party the current results' prices were fetched for
  departure: null,  // departure ISO of the current search (reused for paging)
  earlierRef: null,  // paging tokens from the API
  laterRef: null,
  lang: URL_LANG,
  chart: null,  // which hero chart is expanded: null (collapsed) | "scatter" | "violin"
  status: null,  // {key, params} of the current status message, re-rendered on lang switch
  mode: PAST_PAGE ? "past" : "future",  // "future" = delay forecast, "past" = compensation check for a past journey
  coverage: null,  // {minDay, maxDay, liveMaxDay} for the date picker, fetched on demand
  liveDay: false,  // searched day is past the local data and answered live from IRIS
  claimJourney: null,  // journey shown in the claim-steps modal
  reportJourney: null,  // journey shown in the report-subscription modal
  returnTrip: false,  // a return journey was added in the search card
  leg: "outbound",  // step of the round trip: "outbound" | "return" | "summary"
  returnDeparture: null,  // departure ISO of the return search
  outbound: null,  // journey picked on the outbound step
  outboundResults: null,  // cached outbound list, so going back doesn't refetch
  returnJourney: null,  // journey picked on the return step
  returnResults: null,  // cached return list, so going back doesn't refetch
  returnPrefetch: null,  // {departure, data} proven answerable by the search's preflight
  products: null,  // {outbound, return}: Sets of enabled produktgattungen, initialized below
};

// DB digital compensation flow lives in the customer account's past-trips list
const CLAIM_URL = "https://www.bahn.de/buchung/reiseuebersicht/vergangene";
// fallback for tickets not bought via a bahn.de account
const CLAIM_FORM_URL = "https://www.bahn.de/fahrgastrechte";

// no-op when the Umami script is blocked or unavailable
const track = (name, data) => window.umami?.track(name, data);

// kill switch for all donate/Ko-fi asks (footer link + post-result nudge); set true to bring them back
const DONATE_ENABLED = false;

// --- i18n ---

const I18N = {
  de: {
    pageTitle: "DelayBahn – DB Verbindungssuche mit Verspätungsstatistik",
    pageTitlePast: "Bahn-Entschädigung prüfen – Verspätungs-Check für vergangene Reisen | DelayBahn",
    headerTitle: "Verbindungssuche",
    headerSubtitle: "mit Verspätungsstatistik",
    tagline: "Den Zug buchen, nicht die Verspätung",
    from: "Von",
    to: "Nach",
    fromPlaceholder: "z.B. Berlin Hbf",
    toPlaceholder: "z.B. München Hbf",
    swapTitle: "Richtung tauschen",
    clearInput: "Eingabe löschen",
    date: "Datum",
    time: "Uhrzeit",
    returnAdd: "+ Rückfahrt hinzufügen",
    returnDate: "Rückfahrt",
    returnTime: "Uhrzeit",
    returnRemoveTitle: "Rückfahrt entfernen",
    returnIncomplete: "Bitte Datum und Uhrzeit der Rückfahrt angeben.",
    returnBeforeOutbound: "Die Rückfahrt kann nicht vor der Hinfahrt liegen.",
    stopoverAdd: "+ Zwischenhalt hinzufügen",
    stopover1: "Zwischenhalt 1",
    stopover2: "Zwischenhalt 2",
    stopoverPlaceholder: "z.B. Frankfurt(Main)Hbf",
    stayLabel: "Aufenthalt",
    stayHours: "Stunden",
    stayMins: "Minuten",
    stayLess: "Aufenthalt verkürzen",
    stayMore: "Aufenthalt verlängern",
    stopoverRemoveTitle: "Zwischenhalt entfernen",
    stopoverUnresolved: "Bitte wähle den Zwischenhalt aus der Vorschlagsliste.",
    stepOutbound: "Hinfahrt",
    stepReturn: "Rückfahrt",
    stepSummary: "Übersicht",
    stepSummaryHint: "Prüfen und buchen",
    stepBack: (n, label) => `Zurück zu Schritt ${n}: ${label}`,
    continueBtn: "Weiter",
    outboundPicked: "Hinfahrt gewählt:",
    changeOutbound: "Ändern",
    summaryTitle: "Deine Reise",
    summaryTotal: "Gesamtpreis",
    bookBoth: "Beide Fahrten auf bahn.de buchen",
    window: "Statistik-Zeitraum",
    days7: "7 Tage",
    days15: "15 Tage",
    days30: "30 Tage",
    transferTime: "Umstiegszeit",
    transferNormal: "Normal",
    search: "Suchen",
    sortLabel: "Sortieren:",
    sortDeparture: "Abfahrtszeit",
    sortDelay: "Wenigste Verspätung",
    sortPrice: "Günstigster Preis",
    sortRisk: "Geringstes Anschlussrisiko",
    sortTransfers: "Wenigste Umstiege",
    earlier: "Frühere Verbindungen",
    later: "Spätere Verbindungen",
    heroHeadline: "Setz auf den pünktlicheren Zug.",
    heroClaimLate: "Verspätete Züge bleiben verspätet.",
    heroClaimPunctual: "Pünktliche Züge bleiben pünktlich.",
    heroSubScope: "39.143 Züge im Juni und Juli 2026 verglichen:",
    heroSubFinding: "Wer im Juni zu spät kam, kam auch im Juli zu spät.",
    chartSwitchLabel: "Daten ansehen",
    chartAlt: "Verspätete Züge bleiben verspätet: Züge, die im Juni verspätet waren, waren es auch im Juli.",
    violinAlt: "Pünktlich bleibt pünktlich, verspätet bleibt verspätet: Züge, gruppiert nach ihrer Juni-Verspätung, zeigen im Juli dieselbe Rangfolge.",
    chartScatter: "Punktwolke",
    chartViolin: "Verteilung",
    favAdd: "Zu Favoriten hinzufügen",
    favRemove: "Aus Favoriten entfernen",
    pickStations: "Bitte Start und Ziel aus der Vorschlagsliste wählen.",
    searching: "Suche Verbindungen…",
    noResults: "Keine Verbindungen gefunden.",
    error: (msg) => `Fehler: ${msg}`,
    overloadRetryIn: (s) => `DB-Server überlastet – neuer Versuch in ${s} s…`,
    overloadRetryAgainIn: (n, s) => `DB-Server weiterhin überlastet – ${n}. Versuch in ${s} s…`,
    overloadFail: "Die DB-Server sind gerade überlastet. Bitte versuch es in einer Minute noch einmal.",
    staleNotice: (min) => `DB-Server überlastet – Ergebnisse vom Stand vor ${min} Min.`,
    returnChecking: "Prüfe Verbindungen für die Rückfahrt…",
    returnRetryIn: (s) => `Rückfahrt noch nicht abrufbar – neuer Versuch in ${s} s…`,
    returnRetryAgainIn: (n, s) => `Rückfahrt weiterhin nicht abrufbar – ${n}. Versuch in ${s} s…`,
    returnUnavailable: "Die Rückfahrt lässt sich gerade nicht abrufen – die DB-Server sind überlastet. Bitte versuch es in einer Minute noch einmal.",
    noData: "keine Daten",
    notTracked: "nicht erfasst",
    notTrackedTooltip: "Für U-Bahn, Tram, Bus und Fähre werden keine Verspätungsdaten erhoben",
    liveTimeTooltip: "Voraussichtliche Zeit inkl. aktueller Verspätung",
    badgeDays: (matched, total) => `(${matched}/${total} Tage)`,
    badgeTooltip: (win, max) => `Mittlere Ankunftsverspätung (Median) der letzten ${win} Tage (max. +${max} min)`,
    badgeClickHint: "Verspätung pro Tag anzeigen",
    chartDayCaption: (win) => `Ankunftsverspätung pro Tag – letzte ${win} Tage`,
    chartCanceled: "ausgefallen",
    direct: "direkt",
    transfers: (n) => `${n} Umstieg${n > 1 ? "e" : ""}`,
    walk: "Fußweg",
    walkMinutes: (n) => `${n} min`,
    train: "Zug",
    priceFrom: (price) => `ab ${price.toFixed(2).replace(".", ",")} €`,
    priceNa: "Preis auf bahn.de",
    priceFree: "kostenfrei",
    priceFreeTooltip: "Kinder von 0-5 Jahren reisen kostenfrei und ohne eigene Fahrkarte",
    dticket: "Nur D-Ticket",
    dticketTooltip: "Nur Verbindungen anzeigen, die mit dem Deutschland-Ticket nutzbar sind",
    dticketAll: "D-Ticket + alle Züge",
    dticketAllTooltip: "Alle Verbindungen anzeigen – Nahverkehr ist mit dem Deutschland-Ticket inklusive, Preise gelten nur für die übrigen Züge",
    dticketIncluded: "D-Ticket",
    dticketIncludedTooltip: "Mit dem Deutschland-Ticket nutzbar – kein Ticketkauf nötig",
    dticketPartialTooltip: "Preis nur für die Züge, die das Deutschland-Ticket nicht abdeckt",
    advancedOptions: "Erweiterte Optionen",
    age: "Alter",
    ageAdult: "Person (27-64 Jahre)",
    ageSenior: "Person (ab 65 Jahre)",
    ageYoung: "Person (15-26 Jahre)",
    ageChild: "Kind (6-14 Jahre)",
    ageToddler: "Kind (0-5 Jahre)",
    travellerCount: "Anzahl Reisende",
    travellerAdd: "+ Reisende hinzufügen",
    travellerRemoveTitle: "Reisende entfernen",
    travellerMaxTooltip: (max) => `Für mehr als ${max} Reisende bietet bahn.de eine eigene Gruppenbuchung an`,
    discount: "Ermäßigung",
    discountNone: "keine Ermäßigung",
    discountBc25Second: "BahnCard 25, 2. Klasse",
    discountBc25First: "BahnCard 25, 1. Klasse",
    discountBc50Second: "BahnCard 50, 2. Klasse",
    discountBc50First: "BahnCard 50, 1. Klasse",
    discountBc100Second: "BahnCard 100, 2. Klasse",
    discountBc100First: "BahnCard 100, 1. Klasse",
    discountNoneToddlerTooltip: "Kinder von 0-5 Jahren reisen kostenfrei – eine Ermäßigung gibt es dafür nicht",
    transportTitle: "Verkehrsmittel",
    transportTooltip: "Nach Verkehrsmitteln filtern",
    transportSection: "Streckenabschnitt",
    transportEntire: "Gesamte Reise",
    transportOutbound: "Hinfahrt",
    transportReturn: "Rückfahrt",
    transportAll: "Alle",
    transportLocal: "Nur Nahverkehr",
    transportLong: "Nur Fernverkehr",
    tmIce: "Hochgeschwindigkeitszüge",
    tmIc: "Intercity- und Eurocityzüge",
    tmIr: "Interregio- und Schnellzüge",
    tmRegional: "Nahverkehr und sonstige Züge",
    tmSbahn: "S-Bahn",
    tmBus: "Busse",
    tmBoat: "Schiffe",
    tmUbahn: "U-Bahn",
    tmTram: "Straßenbahn",
    tmOnCall: "Anrufpflichtige Fahrten",
    transportNone: "Mindestens ein Verkehrsmittel auswählen.",
    transportReset: "Zurücksetzen",
    transportAccept: "Übernehmen",
    book: "Auf bahn.de buchen",
    cancelNote: (win, n) => `⚠ In den letzten ${win} Tagen ${n}× (teil-)ausgefallen`,
    tightTitle: "Knapper Umstieg:",
    unlikelyTitle: "Unwahrscheinlicher Umstieg:",
    unlikelyBadge: "⛔ Anschlussrisiko",
    unlikelyBadgeTooltip: (stations) => `Die typische Verspätung übersteigt die Umstiegszeit deutlich (${stations})`,
    tightBadge: "⚠ Knapper Umstieg",
    tightBadgeTooltip: (stations) => `Die typische Verspätung lässt wenig Umstiegszeit (${stations})`,
    tightDetail: (transfer, delay) => `${transfer} min Umstiegszeit – dieser Zug kommt typischerweise +${delay} min verspätet an`,
    ifMissedBtn: (time) => `Falls verpasst: Ankunft ${time}`,
    ifMissedLead: "↳ Nächste realistische Verbindung:",
    footerStories: "Delay Geschichten",
    footerOpenSource: "Open Source – Quellcode auf GitHub",
    footerData: "Verspätungsdaten:",
    footerDonate: "☕ Spendier mir einen Kaffee",
    donateNudgeLead: "Hat dir das geholfen?",
    donateNudgeLink: "☕ Spendier mir einen Kaffee",
    feedbackAsk: "Hat dir das geholfen?",
    feedbackYes: "Ja, hilfreich",
    feedbackNo: "Nein, nicht hilfreich",
    feedbackDismiss: "Ausblenden",
    feedbackFollowUp: "Danke! Was können wir besser machen?",
    feedbackPlaceholder: "Was war hilfreich, was hat gefehlt oder gestört? Je konkreter, desto besser. Screenshots kannst du direkt hier einfügen.",
    feedbackSend: "Senden",
    feedbackAttach: "📎 Screenshot anhängen",
    feedbackShotRemove: "Screenshot entfernen",
    feedbackShotError: "Screenshot konnte nicht angehängt werden.",
    feedbackThanks: "Danke für dein Feedback.",
    footerLegal: "Impressum & Datenschutz",
    footerContact: "Kontakt",
    // English on the German page by choice, not an untranslated string
    followFeatures: "New features:",
    followInstagram: "DelayBahn auf Instagram",
    followLinkedIn: "DelayBahn auf LinkedIn",
    followX: "DelayBahn auf X",
    footerDisclaimer: "DelayBahn ist ein unabhängiges Projekt und steht in keiner Verbindung zur Deutsche Bahn AG. „DB“ und „Deutsche Bahn“ sind Marken der Deutsche Bahn AG.",
    navRefund: "Entschädigung beantragen",
    refundCtaTitle: "Über 1 Stunde Verspätung gehabt?",
    refundCtaLead: "Sieh die Reise, die du tatsächlich hattest – mit Verspätungen und verpassten Anschlüssen.",
    refundCtaSub: "Hol dir dein Geld von der DB zurück – in 3 einfachen Klicks",
    storiesCtaNew: "Neu",
    storiesCtaLead: "Gestrandet, verspätet, überlebt? Echte Geschichten von deutschen Bahnhöfen – von Reisenden wie dir.",
    storiesCtaGo: "Lesen, abstimmen oder selbst erzählen →",
    storiesBannerTitle: "Schlechte Fahrt gehabt? Erzähl sie hier.",
    storiesBannerLead: "Wir sammeln Probleme mit deutschen Zügen.",
    pastTitle: "Verspätungs-Check für vergangene Reisen",
    pastLead: "Gib deine Reise ein, um zu sehen, wie sie tatsächlich verlief – mit Verspätungen, verpassten Anschlüssen und deinem Entschädigungsanspruch.",
    pastCoverageLabel: "Daten verfügbar:",
    pastExit: "← Zur Verbindungssuche",
    searchPast: "Entschädigung prüfen",
    dateOutOfRange: (a, b) => `Verspätungsdaten sind nur für Reisen vom ${a} bis ${b} verfügbar.`,
    dateNotYet: (d) => `Verspätungsdaten für dieses Datum sind noch nicht verfügbar. Neue Daten kommen jeden Morgen dazu – schau ab dem ${d} wieder vorbei.`,
    dateNotYetLag: "Verspätungsdaten für dieses Datum sind noch nicht verfügbar – die Daten hängen gerade etwas hinterher. Schau in den nächsten Tagen wieder vorbei.",
    notYetBadge: "noch offen",
    notYetTooltip: "Für diesen Halt liegt noch keine Ist-Meldung vor – sie kommt spätestens am nächsten Morgen dazu.",
    claimPending: "Ankunft noch nicht bestätigt – morgen früh prüfen",
    thatDayTooltip: "Tatsächliche Ankunftsverspätung an diesem Tag",
    claimPct: (pct) => `${pct} % zurückholen →`,
    claimNone: "Keine Entschädigung (unter 60 min)",
    claimCanceled: "Ausgefallen – Anspruch prüfen →",
    claimMissed: "Anschluss verpasst – Anspruch prüfen →",
    claimAltPre: "Ticket nicht im DB-Konto?",
    claimAltLink: "Zum Fahrgastrechte-Formular",
    claimModalTitle: "So holst du dir dein Geld zurück",
    claimModalTitlePct: (pct) => `So holst du dir ${pct} % zurück`,
    claimModalLead: "Gleich öffnet sich deine Reiseübersicht auf bahn.de. Melde dich dort an – dann sind es nur diese Schritte:",
    claimModalStepFind: "Finde diese Reise unter „Vergangene Reisen“:",
    claimModalStepDetails: "Öffne die Reisedetails:",
    claimModalStepRequest: "Starte den Entschädigungsantrag:",
    claimModalStepSubmit: "Prüfe die Angaben und sende den Antrag ab:",
    bahnBtnDetails: "Reisedetails",
    bahnBtnRequest: "Entschädigung beantragen",
    bahnBtnSubmit: "Antrag jetzt senden",
    claimModalGo: "Weiter zu bahn.de →",
    claimModalClose: "Schließen",
    missedBadge: "⛔ Anschluss verpasst",
    missedLegBadge: "verpasst",
    simContinuation: "↳ Tatsächliche Weiterfahrt mit der nächsten möglichen Verbindung:",
    simBadgeTooltip: "Simulierte Verspätung am Ziel – verpasste Anschlüsse und tatsächliche Weiterfahrt berücksichtigt",
    simIncomplete: "Keine Ersatzverbindung in den Daten gefunden – tatsächliche Ankunft unbekannt",
    pastDisclaimer: "Entschädigung nach EU-Fahrgastrechten: 25 % des Ticketpreises ab 60 min, 50 % ab 120 min Verspätung am Ziel. Auszahlung ab 4 €. Angezeigte Verspätungen basieren auf unseren aufgezeichneten Daten – maßgeblich ist die tatsächliche Ankunft.",
    installTitle: "DelayBahn als App installieren",
    installLead: "Schneller Zugriff vom Startbildschirm.",
    installLeadDesktop: "Öffne delaybahn.com im Browser deines Handys und tippe auf „Installieren“.",
    installBtn: "Installieren",
    iosSheetTitle: "Zum Home-Bildschirm hinzufügen",
    iosSheetLead: "In Safari, in 3 Schritten:",
    iosSheetLeadChrome: "In Chrome, in 3 Schritten:",
    iosStep1Chrome: "Auf das Teilen-Symbol neben dem Website-Namen in der Adressleiste tippen",
    iosStep2Chrome: "Nach unten scrollen zu „Zum Home-Bildschirm“",
    iosSheetLeadOther: "Über das Teilen-Menü deines Browsers, in 3 Schritten:",
    iosStep1: "Auf „Teilen“ tippen",
    iosStep2: "„Zum Home-Bildschirm“ wählen",
    iosStep3: "„Hinzufügen“ tippen",
    iosSheetDone: "Verstanden",
    iosSheetClose: "Schließen",
    installDismiss: "Schließen",
    reportBellTitle: "Verspätungs-Report nach der Fahrt erhalten",
    reportBellOnTitle: "Report bestellt – zum Abbestellen klicken",
    reportModalTitle: "Statistik vs. Realität – dein Verspätungs-Report",
    reportModalLead: "Nach deiner Fahrt vergleichen wir die heute angezeigte Verspätung aus der Vergangenheit mit dem, was tatsächlich passiert ist – der Report kommt etwa zwei Tage später per E-Mail.",
    reportTeaserSeen: "Heute angezeigte Verspätung",
    reportTeaserUpTo: (station) => `bis ${station}`,
    reportTeaserActual: "Tatsächliche Verspätung",
    reportTeaserAfter: "wird nach deiner Fahrt ermittelt",
    reportLoginLead: "Der Report geht an die E-Mail-Adresse deines DelayBahn-Kontos.",
    reportLoginBtn: "Anmelden & Report bestellen",
    reportBusy: "Einen Moment …",
    reportDoneMsg: (email) => `Bestellt! Der Report geht an ${email}.`,
    reportStoredNote: "Gespeichert: diese Verbindung und deine Konto-E-Mail. Abbestellen jederzeit hier oder per Link in der E-Mail.",
    reportPrivacyLink: "Datenschutzhinweise",
    reportCancelBtn: "Report abbestellen",
    reportCancelledMsg: "Abbestellt – für diese Fahrt kommt kein Report.",
    reportErrInvalid: "Für diese Fahrt ist kein Report möglich.",
    reportErrThrottle: "Zu viele Anfragen – bitte versuch es später noch einmal.",
    reportErrGeneric: "Das hat gerade nicht geklappt – bitte versuch es später noch einmal.",
    reportModalClose: "Schließen",
  },
  en: {
    pageTitle: "DelayBahn – DB Connection Search with Delay Statistics",
    pageTitlePast: "Check DB delay compensation – delay check for past journeys | DelayBahn",
    headerTitle: "Connection Search",
    headerSubtitle: "with delay statistics",
    tagline: "Book the train, not the delay",
    from: "From",
    to: "To",
    fromPlaceholder: "e.g. Berlin Hbf",
    toPlaceholder: "e.g. München Hbf",
    swapTitle: "Swap direction",
    clearInput: "Clear input",
    date: "Date",
    time: "Time",
    returnAdd: "+ Add return journey",
    returnDate: "Return",
    returnTime: "Time",
    returnRemoveTitle: "Remove return journey",
    returnIncomplete: "Please enter a date and time for the return journey.",
    returnBeforeOutbound: "The return journey can't start before the outbound one.",
    stopoverAdd: "+ Add stopover",
    stopover1: "Stopover 1",
    stopover2: "Stopover 2",
    stopoverPlaceholder: "e.g. Frankfurt(Main)Hbf",
    stayLabel: "Stay",
    stayHours: "Hours",
    stayMins: "Minutes",
    stayLess: "Shorten stay",
    stayMore: "Extend stay",
    stopoverRemoveTitle: "Remove stopover",
    stopoverUnresolved: "Please pick the stopover from the suggestion list.",
    stepOutbound: "Outbound",
    stepReturn: "Return",
    stepSummary: "Summary",
    stepSummaryHint: "Review and book",
    stepBack: (n, label) => `Back to step ${n}: ${label}`,
    continueBtn: "Continue",
    outboundPicked: "Outbound selected:",
    changeOutbound: "Change",
    summaryTitle: "Your trip",
    summaryTotal: "Total",
    bookBoth: "Book both trips on bahn.de",
    window: "Tracking period",
    days7: "7 days",
    days15: "15 days",
    days30: "30 days",
    transferTime: "Transfer time",
    transferNormal: "Normal",
    search: "Search",
    sortLabel: "Sort:",
    sortDeparture: "Departure time",
    sortDelay: "Least delay",
    sortPrice: "Cheapest price",
    sortRisk: "Lowest connection risk",
    sortTransfers: "Fewest transfers",
    earlier: "Earlier connections",
    later: "Later connections",
    heroHeadline: "Choose the train with the better track record.",
    heroClaimLate: "Late trains stay late.",
    heroClaimPunctual: "Punctual trains stay punctual.",
    heroSubScope: "39,143 trains compared across June and July 2026",
    heroSubFinding: "the ones that ran late in June ran late again in July.",
    chartSwitchLabel: "See the data",
    chartAlt: "Delayed trains stay delayed: trains that ran late in June also ran late in July.",
    violinAlt: "Punctual stays punctual, late stays late: trains grouped by their June delay show the same ranking in July.",
    chartScatter: "Scatter",
    chartViolin: "Distribution",
    favAdd: "Add to favourites",
    favRemove: "Remove from favourites",
    pickStations: "Please pick origin and destination from the suggestion list.",
    searching: "Searching for connections…",
    noResults: "No connections found.",
    error: (msg) => `Error: ${msg}`,
    overloadRetryIn: (s) => `DB's servers are busy – retrying in ${s} s…`,
    overloadRetryAgainIn: (n, s) => `DB's servers are still busy – retry ${n} in ${s} s…`,
    overloadFail: "DB's servers are overloaded right now. Please try again in a minute.",
    staleNotice: (min) => `DB servers busy – results as of ${min} min ago.`,
    returnChecking: "Checking connections for the return journey…",
    returnRetryIn: (s) => `Return journey not available yet – retrying in ${s} s…`,
    returnRetryAgainIn: (n, s) => `Return journey still not available – retry ${n} in ${s} s…`,
    returnUnavailable: "The return journey can't be loaded right now – DB's servers are overloaded. Please try again in a minute.",
    noData: "no data",
    notTracked: "not tracked",
    notTrackedTooltip: "Delay data isn't collected for metro, tram, bus and ferry services",
    liveTimeTooltip: "Expected time including the current delay",
    badgeDays: (matched, total) => `(${matched}/${total} days)`,
    badgeTooltip: (win, max) => `Median arrival delay over the last ${win} days (max. +${max} min)`,
    badgeClickHint: "Show per-day delays",
    chartDayCaption: (win) => `Arrival delay per day – last ${win} days`,
    chartCanceled: "cancelled",
    direct: "direct",
    transfers: (n) => `${n} transfer${n > 1 ? "s" : ""}`,
    walk: "Walk",
    walkMinutes: (n) => `${n} min`,
    train: "Train",
    priceFrom: (price) => `from ${price.toFixed(2).replace(".", ",")} €`,
    priceNa: "Price on bahn.de",
    priceFree: "free",
    priceFreeTooltip: "Children aged 0-5 travel free of charge, no ticket needed",
    dticket: "D-Ticket only",
    dticketTooltip: "Only show connections valid with the Deutschland-Ticket",
    dticketAll: "D-Ticket + all trains",
    dticketAllTooltip: "Show all connections – regional legs are covered by the Deutschland-Ticket, prices are for the remaining trains only",
    dticketIncluded: "D-Ticket",
    dticketIncludedTooltip: "Valid with the Deutschland-Ticket – no extra ticket needed",
    dticketPartialTooltip: "Price covers only the trains the Deutschland-Ticket does not include",
    advancedOptions: "Advanced options",
    age: "Age",
    ageAdult: "Person (aged 27-64)",
    ageSenior: "Person (aged 65 or over)",
    ageYoung: "Person (aged 15-26)",
    ageChild: "Child (aged 6-14)",
    ageToddler: "Child (aged 0-5)",
    travellerCount: "Number of travellers",
    travellerAdd: "+ Add travellers",
    travellerRemoveTitle: "Remove travellers",
    travellerMaxTooltip: (max) => `For parties of more than ${max}, bahn.de has a group booking of its own`,
    discount: "Discount",
    discountNone: "no discount",
    discountBc25Second: "BahnCard 25, 2nd class",
    discountBc25First: "BahnCard 25, 1st class",
    discountBc50Second: "BahnCard 50, 2nd class",
    discountBc50First: "BahnCard 50, 1st class",
    discountBc100Second: "BahnCard 100, 2nd class",
    discountBc100First: "BahnCard 100, 1st class",
    discountNoneToddlerTooltip: "Children aged 0-5 travel free of charge, so there is no discount to pick",
    transportTitle: "Mode of transport",
    transportTooltip: "Filter by mode of transport",
    transportSection: "Route section",
    transportEntire: "Entire journey",
    transportOutbound: "Outbound journey",
    transportReturn: "Return journey",
    transportAll: "All",
    transportLocal: "Local transport only",
    transportLong: "Long-distance travel only",
    tmIce: "High-speed trains",
    tmIc: "Intercity and Eurocity trains",
    tmIr: "Interregio and fast trains",
    tmRegional: "Regional and other trains",
    tmSbahn: "S-Bahn",
    tmBus: "Buses",
    tmBoat: "Boats",
    tmUbahn: "Underground",
    tmTram: "Tram",
    tmOnCall: "Services requiring tel. registration",
    transportNone: "Select at least one mode of transport.",
    transportReset: "Reset",
    transportAccept: "Accept",
    book: "Book on bahn.de",
    cancelNote: (win, n) => `⚠ (Partially) cancelled ${n}× in the last ${win} days`,
    tightTitle: "Tight transfer:",
    unlikelyTitle: "Unlikely transfer:",
    unlikelyBadge: "⛔ Connection risk",
    unlikelyBadgeTooltip: (stations) => `Typical delay far exceeds the transfer time (${stations})`,
    tightBadge: "⚠ Tight transfer",
    tightBadgeTooltip: (stations) => `Typical delay leaves little time to change trains (${stations})`,
    tightDetail: (transfer, delay) => `${transfer} min to change trains – this train typically arrives +${delay} min late`,
    ifMissedBtn: (time) => `If missed: arrival ${time}`,
    ifMissedLead: "↳ Next realistic connection:",
    footerStories: "Delay Stories",
    footerOpenSource: "Open source – view the code on GitHub",
    footerData: "Delay data:",
    footerDonate: "☕ Buy me a coffee",
    donateNudgeLead: "Found this useful?",
    donateNudgeLink: "☕ Buy me a coffee",
    feedbackAsk: "Was this helpful?",
    feedbackYes: "Yes, helpful",
    feedbackNo: "No, not helpful",
    feedbackDismiss: "Dismiss",
    feedbackFollowUp: "Thanks! What could be better?",
    feedbackPlaceholder: "What helped, what was missing or in the way? The more specific, the better. You can paste a screenshot right here.",
    feedbackSend: "Send",
    feedbackAttach: "📎 Attach a screenshot",
    feedbackShotRemove: "Remove screenshot",
    feedbackShotError: "Couldn't attach that screenshot.",
    feedbackThanks: "Thanks for your feedback.",
    footerLegal: "Legal notice & privacy",
    footerContact: "Contact us",
    followFeatures: "New features:",
    followInstagram: "DelayBahn on Instagram",
    followLinkedIn: "DelayBahn on LinkedIn",
    followX: "DelayBahn on X",
    footerDisclaimer: "DelayBahn is an independent project and is not affiliated with Deutsche Bahn AG. “DB” and “Deutsche Bahn” are trademarks of Deutsche Bahn AG.",
    navRefund: "Apply for delay compensation",
    refundCtaTitle: "Hit by over 1 hour of delay?",
    refundCtaLead: "See the journey you actually took, including delays and missed connections.",
    refundCtaSub: "Get your money back from DB in 3 easy clicks",
    storiesCtaNew: "New",
    storiesCtaLead: "Stranded, delayed, survived? Real stories from German train stations – from travellers like you.",
    storiesCtaGo: "Read, vote or tell your own →",
    storiesBannerTitle: "Had a less than ideal train experience? Report it here.",
    storiesBannerLead: "We are crowdsourcing issues with German trains.",
    pastTitle: "Delay check for past journeys",
    pastLead: "Enter your journey to see the trip you actually took – including delays, missed connections and what you can claim back.",
    pastCoverageLabel: "Data available:",
    pastExit: "← Back to connection search",
    searchPast: "Check compensation",
    dateOutOfRange: (a, b) => `Delay data is only available for journeys from ${a} to ${b}.`,
    dateNotYet: (d) => `Delay data for this date isn't available yet. New data arrives every morning – check back on ${d}.`,
    dateNotYetLag: "Delay data for this date isn't available yet – the data is currently running a bit behind. Please check back in the next few days.",
    notYetBadge: "pending",
    notYetTooltip: "No actual time reported for this stop yet – it lands by tomorrow morning at the latest.",
    claimPending: "Arrival not confirmed yet – check tomorrow morning",
    thatDayTooltip: "Actual arrival delay on this day",
    claimPct: (pct) => `Get ${pct}% back →`,
    claimNone: "No compensation (under 60 min)",
    claimCanceled: "Cancelled – check your claim →",
    claimMissed: "Missed connection – check your claim →",
    claimAltPre: "Ticket not in your DB account?",
    claimAltLink: "Use the passenger rights form",
    claimModalTitle: "How to get your money back",
    claimModalTitlePct: (pct) => `How to get your ${pct}% back`,
    claimModalLead: "You're about to open your trip overview on bahn.de. Log in there – then it's just these steps:",
    claimModalStepFind: "Find this journey under “Past trips”:",
    claimModalStepDetails: "Open the trip details:",
    claimModalStepRequest: "Start the compensation request:",
    claimModalStepSubmit: "Check the details and submit:",
    bahnBtnDetails: "Trip details",
    bahnBtnRequest: "Submit compensation request",
    bahnBtnSubmit: "Submit request now",
    claimModalGo: "Continue to bahn.de →",
    claimModalClose: "Close",
    missedBadge: "⛔ Missed connection",
    missedLegBadge: "missed",
    simContinuation: "↳ Actual onward journey with the next possible connection:",
    simBadgeTooltip: "Simulated delay at destination – missed connections and the actual onward journey taken into account",
    simIncomplete: "No replacement connection found in the data – actual arrival unknown",
    pastDisclaimer: "Compensation under EU passenger rights: 25% of the ticket price from 60 min, 50% from 120 min delay at your destination. Paid out from €4. Shown delays are based on our recorded data – the actual arrival is authoritative.",
    installTitle: "Install DelayBahn as an app",
    installLead: "Quick access from your home screen.",
    installLeadDesktop: "Open delaybahn.com in your phone's browser and tap Install.",
    installBtn: "Install",
    iosSheetTitle: "Add to your home screen",
    iosSheetLead: "In Safari, in 3 steps:",
    iosSheetLeadChrome: "In Chrome, in 3 steps:",
    iosStep1Chrome: "Tap the share icon next to the website name in the address bar",
    iosStep2Chrome: "Scroll down to “Add to Home Screen”",
    iosSheetLeadOther: "From your browser’s share menu, in 3 steps:",
    iosStep1: "Tap “Share”",
    iosStep2: "Choose “Add to Home Screen”",
    iosStep3: "Tap “Add”",
    iosSheetDone: "Got it",
    iosSheetClose: "Close",
    installDismiss: "Dismiss",
    reportBellTitle: "Get a delay report after your journey",
    reportBellOnTitle: "Report ordered – click to cancel",
    reportModalTitle: "History vs. reality – your delay report",
    reportModalLead: "After your journey we compare the past delay shown today with what actually happened – the report arrives by email about two days later.",
    reportTeaserSeen: "Delay shown today",
    reportTeaserUpTo: (station) => `up to ${station}`,
    reportTeaserActual: "Actual delay",
    reportTeaserAfter: "revealed after your journey",
    reportLoginLead: "The report goes to your DelayBahn account's email address.",
    reportLoginBtn: "Log in & order the report",
    reportBusy: "One moment …",
    reportDoneMsg: (email) => `Ordered! The report goes to ${email}.`,
    reportStoredNote: "Stored: this connection and your account email. Cancel any time here or via the link in the email.",
    reportPrivacyLink: "Privacy notice",
    reportCancelBtn: "Cancel report",
    reportCancelledMsg: "Cancelled – no report will be sent for this journey.",
    reportErrInvalid: "No report is possible for this journey.",
    reportErrThrottle: "Too many requests – please try again later.",
    reportErrGeneric: "That didn't work right now – please try again later.",
    reportModalClose: "Close",
  },
};

function t(key, ...args) {
  const entry = I18N[state.lang][key];
  return typeof entry === "function" ? entry(...args) : entry;
}

// IRIS delay-cause codes (<m t="d" c="…"/>), official German texts; codes 70-98
// are quality messages that never appear as delay causes and are omitted
const DELAY_REASONS = {
  de: {
    1: "Nähere Informationen in Kürze",
    2: "Polizeieinsatz",
    3: "Feuerwehreinsatz auf der Strecke",
    4: "Kurzfristiger Personalausfall",
    5: "Ärztliche Versorgung eines Fahrgastes",
    6: "Betätigen der Notbremse",
    7: "Unbefugte Personen auf der Strecke",
    8: "Notarzteinsatz auf der Strecke",
    9: "Streikauswirkungen",
    10: "Tiere auf der Strecke",
    11: "Unwetter",
    12: "Warten auf ein verspätetes Schiff",
    13: "Pass- und Zollkontrolle",
    14: "Defekt am Bahnhof",
    15: "Beeinträchtigung durch Vandalismus",
    16: "Entschärfung einer Fliegerbombe",
    17: "Beschädigung einer Brücke",
    18: "Umgestürzter Baum auf der Strecke",
    19: "Unfall an einem Bahnübergang",
    20: "Tiere im Gleis",
    21: "Warten auf Anschlussreisende",
    22: "Witterungsbedingte Beeinträchtigungen",
    23: "Betriebsstabilisierung",
    24: "Verspätung im Ausland",
    25: "Bereitstellung weiterer Wagen",
    26: "Abhängen von Wagen",
    27: "Technische Störung am Bus",
    28: "Gegenstände auf der Strecke",
    29: "Ersatzverkehr mit Bus ist eingerichtet",
    30: "Personalausfall im Stellwerk",
    31: "Bauarbeiten",
    32: "Längere Haltezeit am Bahnhof",
    33: "Defekt an der Oberleitung",
    34: "Defekt an einem Signal",
    35: "Streckensperrung",
    36: "Technische Störung am Zug",
    37: "Kurzfristiger Fahrzeugausfall",
    38: "Defekt an der Strecke",
    39: "Stau / Hohes Verkehrsaufkommen",
    40: "Defektes Stellwerk",
    41: "Defekt an einem Bahnübergang",
    42: "Außerplanmäßige Geschwindigkeitsbeschränkung",
    43: "Verspätung eines vorausfahrenden Zuges",
    44: "Warten auf einen entgegenkommenden Zug",
    45: "Vorfahrt eines anderen Zuges",
    46: "Vorfahrt eines anderen Zuges",
    47: "Verspätete Bereitstellung",
    48: "Verspätung aus vorheriger Fahrt",
    49: "Kurzfristiger Personalausfall",
    50: "Kurzfristige Erkrankung von Personal",
    51: "Verspätetes Personal aus vorheriger Fahrt",
    52: "Streik",
    53: "Unwetterauswirkungen",
    54: "Verfügbarkeit der Gleise derzeit eingeschränkt",
    55: "Technischer Defekt an einem anderen Zug",
    56: "Laden der Antriebsbatterie",
    57: "Zusätzlicher Halt",
    58: "Umleitung",
    59: "Schnee und Eis",
    60: "Witterungsbedingt verminderte Geschwindigkeit",
    61: "Defekte Tür",
    62: "Behobener Defekt am Zug",
    63: "Technische Untersuchung am Zug",
    64: "Defekt an einer Weiche",
    65: "Erdrutsch",
    66: "Hochwasser",
    67: "Behördliche Maßnahme",
    68: "Hohes Fahrgastaufkommen",
    69: "Zug verkehrt mit verminderter Geschwindigkeit",
    99: "Verzögerungen im Betriebsablauf",
  },
  en: {
    1: "More information shortly",
    2: "Police operation",
    3: "Fire brigade operation on the line",
    4: "Short-notice staff shortage",
    5: "Medical assistance for a passenger",
    6: "Emergency brake activated",
    7: "Unauthorised people on the line",
    8: "Emergency medical services on the line",
    9: "Strike impact",
    10: "Animals on the line",
    11: "Severe weather",
    12: "Waiting for a delayed ship",
    13: "Passport and customs checks",
    14: "Fault at the station",
    15: "Vandalism",
    16: "Defusing of an unexploded bomb",
    17: "Damage to a bridge",
    18: "Fallen tree on the line",
    19: "Accident at a level crossing",
    20: "Animals on the track",
    21: "Waiting for connecting passengers",
    22: "Weather-related disruption",
    23: "Operational stabilisation",
    24: "Delay abroad",
    25: "Attaching additional carriages",
    26: "Detaching carriages",
    27: "Technical fault on the bus",
    28: "Objects on the line",
    29: "Replacement bus service in place",
    30: "Staff shortage at the signal box",
    31: "Construction work",
    32: "Extended stop at the station",
    33: "Overhead wire fault",
    34: "Signal fault",
    35: "Line closure",
    36: "Technical fault on the train",
    37: "Short-notice vehicle failure",
    38: "Fault on the line",
    39: "Congestion / high traffic volume",
    40: "Signal box failure",
    41: "Fault at a level crossing",
    42: "Unscheduled speed restriction",
    43: "Delay of a preceding train",
    44: "Waiting for an oncoming train",
    45: "Another train given priority",
    46: "Another train given priority",
    47: "Delayed provision of the train",
    48: "Delay from previous journey",
    49: "Short-notice staff shortage",
    50: "Short-notice staff illness",
    51: "Delayed staff from previous journey",
    52: "Strike",
    53: "Effects of severe weather",
    54: "Track availability currently restricted",
    55: "Technical fault on another train",
    56: "Charging the traction battery",
    57: "Additional stop",
    58: "Diversion",
    59: "Snow and ice",
    60: "Weather-related speed reduction",
    61: "Door fault",
    62: "Technical fault on the train resolved",
    63: "Technical inspection of the train",
    64: "Points failure",
    65: "Landslide",
    66: "Flooding",
    67: "Measure by authorities",
    68: "High passenger volume",
    69: "Train running at reduced speed",
    99: "Delays in operations",
  },
};

function reasonText(code) {
  return code != null ? DELAY_REASONS[state.lang][code] || null : null;
}

const chartSrcs = {
  scatter: { de: "/delay-correlation.svg?v=5", en: "/delay-correlation-en.svg?v=5", alt: "chartAlt" },
  violin: { de: "/delay-violin.svg?v=4", en: "/delay-violin-en.svg?v=4", alt: "violinAlt" },
};

function updateChartImg() {
  // the chart itself only loads once a toggle button expands it
  document.getElementById("hero-chart").classList.toggle("chart-open", !!state.chart);
  if (!state.chart) return;
  const img = document.getElementById("chart-img");
  const c = chartSrcs[state.chart];
  img.src = c[state.lang];
  img.alt = t(c.alt);
}

function setStatus(key, ...params) {
  state.status = key ? { key, params } : null;
  statusEl.textContent = key ? t(key, ...params) : "";
}

// markup cloned from a <template> after load misses the page-wide pass in
// applyLang, so translating takes a root: the document there, the fresh row here
function translateSubtree(root) {
  root.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  root.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });
  root.querySelectorAll("[data-i18n-aria]").forEach((el) => { el.ariaLabel = t(el.dataset.i18nAria); });
}

function applyLang(lang) {
  state.lang = lang;
  localStorage.setItem("lang", lang);
  document.documentElement.lang = lang;
  document.title = t(PAST_PAGE ? "pageTitlePast" : "pageTitle");

  document.querySelectorAll(".lang-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.lang === lang));

  translateSubtree(document);

  updateChartImg();
  updateTransportBtn();  // its value is set from JS, so data-i18n cannot retranslate it
  syncTravellerUI();     // same for the tooltips the traveller rows set themselves

  if (state.status) statusEl.textContent = t(state.status.key, ...state.status.params);
  if (state.staleSeconds) setStaleNotice(state.staleSeconds);
  if (claimModal.open) populateClaimModal();
  if (reportModal.open) populateReportModal();
  renderTripSteps();
  render();
}

// --- recent stations ---

const RECENTS_KEY = "recentStations";
const RECENTS_MAX = 6;

function getRecents() {
  try {
    return (JSON.parse(localStorage.getItem(RECENTS_KEY)) || []).filter((s) => s?.id && s?.name);
  } catch { return []; }
}

function saveRecent(station) {
  const list = [{ id: station.id, name: station.name },
    ...getRecents().filter((s) => s.id !== station.id)].slice(0, RECENTS_MAX);
  localStorage.setItem(RECENTS_KEY, JSON.stringify(list));
}

// --- favourite stations ---

const FAVORITES_KEY = "favoriteStations";

function getFavorites() {
  try {
    return (JSON.parse(localStorage.getItem(FAVORITES_KEY)) || []).filter((s) => s?.id && s?.name);
  } catch { return []; }
}

function isFavorite(id) {
  return getFavorites().some((s) => s.id === id);
}

// returns the station's new state, so the caller can repaint its star
function toggleFavorite(station) {
  const list = getFavorites();
  const rest = list.filter((s) => s.id !== station.id);
  const added = rest.length === list.length;
  localStorage.setItem(FAVORITES_KEY,
    JSON.stringify(added ? [...list, { id: station.id, name: station.name }] : rest));
  return added;
}

function paintStar(button, on) {
  button.textContent = on ? "★" : "☆";
  button.classList.toggle("on", on);
  button.setAttribute("aria-pressed", String(on));
  button.title = t(on ? "favRemove" : "favAdd");
  button.setAttribute("aria-label", button.title);
}

// --- autocomplete ---

function setupAutocomplete(inputId, dropdownId, key, onChange) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  let timer = null;

  function makeRow(item) {
    const row = document.createElement("div");
    row.className = "dropdown-item";

    const name = document.createElement("span");
    name.className = "dropdown-name";
    name.textContent = item.name;
    row.appendChild(name);

    const star = document.createElement("button");
    star.type = "button";
    star.className = "fav-star";
    paintStar(star, isFavorite(item.id));
    // mousedown, not click: the input's blur would tear the dropdown down first
    star.addEventListener("mousedown", (e) => {
      e.preventDefault();   // keeps the focus — and the open dropdown — on the input
      e.stopPropagation();  // starring a station must not also pick it
      const nowFavorite = toggleFavorite(item);
      paintStar(star, nowFavorite);
      // in the empty-input view the row's rank in the list just changed
      if (input.value.trim() === "") showSaved();
    });
    row.appendChild(star);

    row.addEventListener("mousedown", () => {
      state[key] = item;
      input.value = item.name;
      dropdown.classList.remove("open");
      onChange?.();
    });
    return row;
  }

  function showItems(items) {
    dropdown.innerHTML = "";
    items.forEach((item) => dropdown.appendChild(makeRow(item)));
    dropdown.classList.toggle("open", items.length > 0);
  }

  // one list: favourites on top, then the recents that aren't already favourites
  function showSaved() {
    if (input.value.trim() !== "") return;
    const favorites = getFavorites();
    showItems([...favorites,
      ...getRecents().filter((s) => !favorites.some((f) => f.id === s.id))]);
  }

  input.addEventListener("focus", showSaved);

  input.addEventListener("input", () => {
    state[key] = null;
    onChange?.();
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      dropdown.classList.remove("open");
      if (q === "") showSaved();
      return;
    }
    timer = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/locations?query=${encodeURIComponent(q)}`);
        if (!resp.ok) return;
        const items = await resp.json();
        showItems(items);
      } catch { /* network hiccup: ignore */ }
    }, 250);
  });

  input.addEventListener("blur", () => setTimeout(() => dropdown.classList.remove("open"), 150));

  // mousedown, not click: keeps the focus — and the open dropdown — on the input
  input.parentElement.querySelector(".input-clear").addEventListener("mousedown", (e) => {
    e.preventDefault();
    state[key] = null;
    input.value = "";
    input.focus();
    showSaved();
    onChange?.();
  });
}

setupAutocomplete("from", "from-dropdown", "from");
setupAutocomplete("to", "to-dropdown", "to");
setupAutocomplete("viaOut1", "viaOut1-dropdown", "viaOut1", syncStopoverUI);
setupAutocomplete("viaOut2", "viaOut2-dropdown", "viaOut2", syncStopoverUI);
setupAutocomplete("viaRet1", "viaRet1-dropdown", "viaRet1", syncStopoverUI);
setupAutocomplete("viaRet2", "viaRet2-dropdown", "viaRet2", syncStopoverUI);

document.getElementById("swap").addEventListener("click", () => {
  const fromInput = document.getElementById("from");
  const toInput = document.getElementById("to");
  [state.from, state.to] = [state.to, state.from];
  [fromInput.value, toInput.value] = [toInput.value, fromInput.value];
});

// --- defaults ---

const now = new Date();
document.getElementById("date").value = now.toISOString().slice(0, 10);
document.getElementById("time").value = now.toTimeString().slice(0, 5);

// --- search ---

const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const staleNoticeEl = document.getElementById("stale-notice");

// results served from the server's stale fallback carry their age; disclose it
function setStaleNotice(seconds) {
  state.staleSeconds = seconds || 0;
  staleNoticeEl.classList.toggle("hidden", !state.staleSeconds);
  staleNoticeEl.textContent = state.staleSeconds
    ? t("staleNotice", Math.max(1, Math.round(state.staleSeconds / 60))) : "";
}
const controlsEl = document.getElementById("controls");
const searchBtn = document.getElementById("search");
if (PAST_PAGE) searchBtn.dataset.i18n = "searchPast";
const earlierBtn = document.getElementById("earlier");
const laterBtn = document.getElementById("later");

// on phones the outcome of a tapped search — the list, the "no connections"
// note or an error — lands below the fold, and a visitor who sees nothing
// move re-taps "search"; bring it into view unless it already is
function scrollResultsIntoView() {
  const anchor = !tripStepsEl.classList.contains("hidden") ? tripStepsEl
    : !controlsEl.classList.contains("hidden") ? controlsEl : statusEl;
  if (anchor.getBoundingClientRect().top < window.innerHeight * 0.5) return;
  anchor.scrollIntoView({ behavior: "smooth", block: "start" });
}

searchBtn.addEventListener("click", async () => {
  await search();
  scrollResultsIntoView();
});
earlierBtn.addEventListener("click", () => loadPage("earlier"));
laterBtn.addEventListener("click", () => loadPage("later"));

function refetchCurrentLeg() {
  if (!state.journeys.length || !state.from || !state.to) return;
  // the picked return may not survive the new filter, so step 3 cannot stand
  if (state.leg === "summary") state.leg = "return";
  // the cached lists were fetched with the settings that just changed
  state.returnPrefetch = null;
  if (state.leg === "return") {
    state.outboundResults = null;
    state.returnJourney = null;
    state.returnResults = null;
  }
  runSearch();
}

document.getElementById("window").addEventListener("change", () => {
  // window is aggregated server-side: refetch, but only if results are showing
  refetchCurrentLeg();
});

// bahn.de's minimum transfer time (Umstiegszeit); "0" is the station's normal
// interchange time. Hidden in past mode, where a leftover value must not filter.
function transferMinutes() {
  if (state.mode === "past") return "0";
  return document.getElementById("transfer").value;
}

document.getElementById("transfer").addEventListener("change", () => {
  // the filter is applied by bahn.de: refetch, but only if results are showing
  refetchCurrentLeg();
});

// "only" filters to D-Ticket-valid connections; "all" keeps every connection but
// prices it for a D-Ticket holder. Both toggles are hidden in past mode, where a
// leftover checked state must not filter.
function dticketMode() {
  if (state.mode === "past") return "off";
  return document.getElementById("dticket").checked ? "only"
    : document.getElementById("dticket-all").checked ? "all" : "off";
}

// the two D-Ticket toggles are mutually exclusive ("only" implies the ticket);
// both are handled server-side by bahn.de: refetch, but only if results are showing
for (const [id, other] of [["dticket", "dticket-all"], ["dticket-all", "dticket"]]) {
  document.getElementById(id).addEventListener("change", (e) => {
    if (e.target.checked) document.getElementById(other).checked = false;
    refetchCurrentLeg();
  });
}

// the age bracket, the minimum transfer time, the transport filter and the
// stopovers are occasional settings: they fold away behind the "advanced
// options" toggle. Collapsing only hides them — whatever is set keeps applying
// to searches. The D-Ticket switches stay out in the main row.
const advancedToggle = document.getElementById("advanced-toggle");
const advancedPanel = document.getElementById("advanced-panel");
function setAdvancedOpen(open) {
  advancedPanel.classList.toggle("hidden", !open);
  advancedToggle.setAttribute("aria-expanded", String(open));
}
advancedToggle.addEventListener("click", () => {
  setAdvancedOpen(advancedPanel.classList.contains("hidden"));
});

// --- travellers (the bahn.de search mask's count / age / discount row) ---

// bahn.de's own cap: from six travellers on, its mask hands over to a group
// booking that prices differently, so the search mask stops there and so do we
const MAX_TRAVELLERS = 5;
// 0-5-year-olds ride free, so bahn.de leaves them no discount to pick
const NO_DISCOUNT_AGES = new Set(["toddler"]);

const travellerFields = document.getElementById("traveller-fields");
const travellerAddBtn = document.getElementById("traveller-add");
const travellerTemplate = document.getElementById("traveller-row-template");

// the option lists live in the template, so the markup stays their one source
const TRAVELLER_AGES = [...travellerTemplate.content.querySelectorAll(".traveller-age option")].map((o) => o.value);
const TRAVELLER_DISCOUNTS = [...travellerTemplate.content.querySelectorAll(".traveller-discount option")].map((o) => o.value);

function travellerRows() {
  return [...travellerFields.querySelectorAll(".traveller-row")];
}

// a "travellers" param off a shared link; null when it carries nothing usable,
// which leaves the default party in place rather than half-restoring one
function parseTravellers(raw) {
  if (!raw) return null;
  const list = raw.split(",").map((entry) => {
    const [age, count, discount] = entry.split(":");
    const n = Number(count);
    return TRAVELLER_AGES.includes(age) && TRAVELLER_DISCOUNTS.includes(discount)
      && Number.isInteger(n) && n >= 1 && n <= MAX_TRAVELLERS ? { count: n, age, discount } : null;
  });
  const total = list.reduce((sum, tr) => sum + (tr?.count || 0), 0);
  return list.every(Boolean) && total <= MAX_TRAVELLERS ? list : null;
}

// the picked party, read off the rows the way the selects beside them are read.
// Pinned to one adult in past mode, where no prices are shown and a leftover
// selection must not fragment the lookup.
function travellersUsed() {
  if (state.mode === "past") return [{ count: 1, age: "adult", discount: "none" }];
  return travellerRows().map((row) => ({
    count: Number(row.querySelector(".traveller-count").value),
    age: row.querySelector(".traveller-age").value,
    discount: row.querySelector(".traveller-discount").value,
  }));
}

// the compact form the API and share links carry: one "<age>:<count>:<discount>"
// per row. Null for the lone adult without a discount, which both ends default to.
function travellersParam() {
  const list = travellersUsed();
  const [first] = list;
  if (list.length === 1 && first.count === 1 && first.age === "adult" && first.discount === "none") return null;
  return list.map((tr) => `${tr.age}:${tr.count}:${tr.discount}`).join(",");
}

// keeps the rows consistent with each other: what is left of the cap bounds every
// count, a lone row cannot be removed, and a 0-5-year-old carries no discount
function syncTravellerUI() {
  const rows = travellerRows();
  const total = travellersUsed().reduce((sum, tr) => sum + tr.count, 0);
  for (const row of rows) {
    const countSel = row.querySelector(".traveller-count");
    const own = Number(countSel.value);
    for (const opt of countSel.options) {
      opt.disabled = total - own + Number(opt.value) > MAX_TRAVELLERS;
    }
    const discountSel = row.querySelector(".traveller-discount");
    const free = NO_DISCOUNT_AGES.has(row.querySelector(".traveller-age").value);
    if (free) discountSel.value = "none";
    discountSel.disabled = free;
    // the tooltip goes on the wrapper: a disabled control shows none of its own
    discountSel.closest(".field").title = free ? t("discountNoneToddlerTooltip") : "";
    row.querySelector(".traveller-remove").classList.toggle("hidden", rows.length === 1);
  }
  // a second traveller turns the line into a list, and the add button drops below it
  travellerFields.classList.toggle("multi", rows.length > 1);
  travellerAddBtn.classList.toggle("hidden", total >= MAX_TRAVELLERS);
  travellerAddBtn.title = total >= MAX_TRAVELLERS ? t("travellerMaxTooltip", MAX_TRAVELLERS) : "";
}

function addTravellerRow(traveller = { count: 1, age: "adult", discount: "none" }) {
  const row = travellerTemplate.content.firstElementChild.cloneNode(true);
  const countSel = row.querySelector(".traveller-count");
  for (let n = 1; n <= MAX_TRAVELLERS; n++) countSel.add(new Option(String(n), String(n)));
  countSel.value = String(traveller.count);
  row.querySelector(".traveller-age").value = traveller.age;
  row.querySelector(".traveller-discount").value = traveller.discount;
  translateSubtree(row);
  travellerFields.insertBefore(row, travellerAddBtn);
  return row;
}

addTravellerRow();  // the default party: one adult, no discount
syncTravellerUI();

travellerAddBtn.addEventListener("click", () => {
  addTravellerRow();
  syncTravellerUI();
  track("traveller-add");
  // the row starts as another plain adult, which prices the same as raising the
  // count on an existing one - nothing to refetch until it is actually changed
});

travellerFields.addEventListener("change", (e) => {
  if (!e.target.closest(".traveller-row")) return;
  syncTravellerUI();
  // pricing is server-side: refetch, but only if results are showing
  refetchCurrentLeg();
});

travellerFields.addEventListener("click", (e) => {
  const btn = e.target.closest(".traveller-remove");
  if (!btn) return;
  btn.closest(".traveller-row").remove();
  syncTravellerUI();
  refetchCurrentLeg();
});

// --- transport-mode filter (bahn.de "Verkehrsmittel") ---

// order matches app/bahn_api.py ALL_PRODUCTS and the bahn.de vm= URL codes
const PRODUCTS = ["ICE", "EC_IC", "IR", "REGIONAL", "SBAHN", "BUS", "SCHIFF", "UBAHN", "TRAM", "ANRUFPFLICHTIG"];
const LONG_DISTANCE = ["ICE", "EC_IC", "IR"];
const LOCAL_TRANSPORT = PRODUCTS.filter((p) => !LONG_DISTANCE.includes(p));
// per-leg sets so a round trip can filter its two searches differently
state.products = { outbound: new Set(PRODUCTS), return: new Set(PRODUCTS) };

const transportModal = document.getElementById("transport-modal");
const tmSectionSel = document.getElementById("tm-section");
const tmAcceptBtn = document.getElementById("tm-accept");
const tmInputs = [...transportModal.querySelectorAll(".tm-row input")];
const tmQuickBtns = [...transportModal.querySelectorAll(".tm-quick-btn")];

let tmDraft = null;  // edited copy of state.products; only Accept commits it

function productsParam(dir) {
  // the picker is hidden in past mode, where a leftover selection must not narrow
  // the compensation check away from the train that was actually taken
  if (state.mode === "past") return null;
  const set = state.products[dir];
  return set.size === PRODUCTS.length ? null : PRODUCTS.filter((p) => set.has(p)).join(",");
}

// a removed return must not keep a diverged filter around for a later re-add
function syncReturnProducts() {
  state.products.return = new Set(state.products.outbound);
}

// the button carries its own value the way the selects beside it do: "Alle"
// until something is switched off, then the counts (two, when the legs diverge)
function updateTransportBtn() {
  const parts = [state.products.outbound];
  if (state.returnTrip && productsParam("return") !== productsParam("outbound")) parts.push(state.products.return);
  const valueEl = document.getElementById("transport-value");
  const filtered = parts.some((s) => s.size < PRODUCTS.length);
  valueEl.textContent = filtered
    ? parts.map((s) => `${s.size}/${PRODUCTS.length}`).join(" · ") : t("transportAll");
  valueEl.classList.toggle("transport-filtered", filtered);
}

// the sets the dialog is editing; "entire journey" writes through to both legs
function tmActiveSets() {
  const section = state.returnTrip ? tmSectionSel.value : "entire";
  return section === "outbound" ? [tmDraft.outbound]
    : section === "return" ? [tmDraft.return]
    : [tmDraft.outbound, tmDraft.return];
}

function tmQuickTarget(btn) {
  return btn.dataset.quick === "all" ? PRODUCTS
    : btn.dataset.quick === "local" ? LOCAL_TRANSPORT : LONG_DISTANCE;
}

function renderTransportModal() {
  const sets = tmActiveSets();
  // a product shows as on only when every edited leg allows it
  for (const input of tmInputs) input.checked = sets.every((s) => s.has(input.dataset.product));
  for (const btn of tmQuickBtns) {
    const target = tmQuickTarget(btn);
    const match = sets.every((s) => s.size === target.length && target.every((p) => s.has(p)));
    btn.setAttribute("aria-pressed", String(match));
  }
  const empty = !tmDraft.outbound.size || (state.returnTrip && !tmDraft.return.size);
  tmAcceptBtn.disabled = empty;
  document.getElementById("tm-none").classList.toggle("hidden", !empty);
}

function openTransportModal() {
  tmDraft = { outbound: new Set(state.products.outbound), return: new Set(state.products.return) };
  document.getElementById("tm-section-wrap").classList.toggle("hidden", !state.returnTrip);
  tmSectionSel.value = "entire";
  // the section picker names the two legs like bahn.de does
  const ogOut = document.getElementById("tm-og-outbound");
  const ogRet = document.getElementById("tm-og-return");
  ogOut.label = t("transportOutbound");
  ogRet.label = t("transportReturn");
  const named = state.from?.name && state.to?.name;
  ogOut.firstElementChild.textContent = named ? `${state.from.name} – ${state.to.name}` : t("transportOutbound");
  ogRet.firstElementChild.textContent = named ? `${state.to.name} – ${state.from.name}` : t("transportReturn");
  renderTransportModal();
  transportModal.showModal();
  track("transport-modal");
}

document.getElementById("transport-btn").addEventListener("click", openTransportModal);
document.getElementById("transport-close").addEventListener("click", () => transportModal.close());
// a click on the backdrop lands on the dialog element itself (the inner wrapper covers the rest)
transportModal.addEventListener("click", (e) => { if (e.target === transportModal) transportModal.close(); });
// X, Esc and backdrop all discard; Accept has already nulled the draft by the time it closes
transportModal.addEventListener("close", () => { tmDraft = null; });

tmSectionSel.addEventListener("change", renderTransportModal);

for (const input of tmInputs) {
  input.addEventListener("change", () => {
    for (const s of tmActiveSets()) input.checked ? s.add(input.dataset.product) : s.delete(input.dataset.product);
    renderTransportModal();
  });
}

for (const btn of tmQuickBtns) {
  btn.addEventListener("click", () => {
    const target = tmQuickTarget(btn);
    for (const s of tmActiveSets()) { s.clear(); for (const p of target) s.add(p); }
    renderTransportModal();
  });
}

document.getElementById("tm-reset").addEventListener("click", () => {
  tmDraft = { outbound: new Set(PRODUCTS), return: new Set(PRODUCTS) };
  renderTransportModal();
});

tmAcceptBtn.addEventListener("click", () => {
  const before = [productsParam("outbound"), productsParam("return")].join("|");
  state.products = tmDraft;
  tmDraft = null;
  const after = [productsParam("outbound"), productsParam("return")].join("|");
  transportModal.close();
  updateTransportBtn();
  // filtering is server-side like the window and D-Ticket: refetch, but only if
  // results are showing
  if (after !== before) {
    track("transport-filter", {
      outbound: productsParam("outbound") || "all",
      return: productsParam("return") || "all",
    });
    refetchCurrentLeg();
  }
});

// --- return journey ---

const dateEl = document.getElementById("date");
const returnAddBtn = document.getElementById("return-add");
const returnFieldsEl = document.getElementById("return-fields");
const returnDateEl = document.getElementById("return-date");
const returnTimeEl = document.getElementById("return-time");
const tripStepsEl = document.getElementById("trip-steps");

function returnDepartureIso() {
  return `${returnDateEl.value}T${returnTimeEl.value}:00`;
}

function setReturnTrip(on) {
  // the compensation check looks at one journey that already happened
  state.returnTrip = on && state.mode !== "past";
  if (!state.returnTrip) syncReturnProducts();
  updateTransportBtn();
  returnAddBtn.classList.toggle("hidden", state.returnTrip);
  returnFieldsEl.classList.toggle("hidden", !state.returnTrip);
  syncStopoverUI();  // the return direction's stopover group follows the trip type
  if (!state.returnTrip) return;
  returnDateEl.min = dateEl.value;
  if (!returnDateEl.value || returnDateEl.value < dateEl.value) returnDateEl.value = dateEl.value;
  if (!returnTimeEl.value) returnTimeEl.value = "17:00";
}

returnAddBtn.addEventListener("click", () => {
  setReturnTrip(true);
  returnDateEl.focus();
  track("return-add");
  // results already on screen keep their place; only their CTA changes to "continue"
  renderTripSteps();
  render();
});

document.getElementById("return-remove").addEventListener("click", () => {
  setReturnTrip(false);
  if (state.leg !== "outbound") backToOutbound();
  else { renderTripSteps(); render(); }
});

dateEl.addEventListener("change", () => {
  returnDateEl.min = dateEl.value;
  if (returnDateEl.value && returnDateEl.value < dateEl.value) returnDateEl.value = dateEl.value;
});

// --- stopovers (the last row of the advanced-options panel) ---

const VIA_KEYS = { outbound: ["viaOut1", "viaOut2"], return: ["viaRet1", "viaRet2"] };
const ALL_VIA_KEYS = [...VIA_KEYS.outbound, ...VIA_KEYS.return];

const viaRow = (key) => document.getElementById(`${key}-row`);
const viaStayEls = (key) => [
  document.getElementById(`${key}StayH`),
  document.getElementById(`${key}StayM`),
];
// the phone-only type=time twin of the two segments (see .stay-time in style.css)
const viaStayTimeEl = (key) => document.getElementById(`${key}StayT`);

const STAY_MAX = 1439;
const STAY_STEP = 15;
const clampStay = (minutes) => Math.max(0, Math.min(STAY_MAX, Math.round(minutes)));

// a shared or older link carries the stay as bare minutes ("90"); hh:mm is read
// too, so a hand-edited "1:30" works; anything unreadable counts as no stay
function parseStay(raw) {
  const text = String(raw ?? "").trim();
  if (!text) return 0;
  const hhmm = text.match(/^(\d{1,2})\s*[:hH]\s*(\d{1,2})?$/);
  const minutes = hhmm ? Number(hhmm[1]) * 60 + Number(hhmm[2] || 0) : Number(text);
  return Number.isFinite(minutes) ? clampStay(minutes) : 0;
}

// the two segments are one duration, so minutes above 59 carry into the hours:
// a typed 0h/90min settles as 1h/30min instead of being rejected
function stayMinutes(key) {
  const [h, m] = viaStayEls(key);
  return clampStay((Number(h.value) || 0) * 60 + (Number(m.value) || 0));
}

function setStay(key, minutes) {
  const total = clampStay(minutes);
  const [h, m] = viaStayEls(key);
  h.value = String(Math.floor(total / 60)).padStart(2, "0");
  m.value = String(total % 60).padStart(2, "0");
  viaStayTimeEl(key).value = `${h.value}:${m.value}`;
  syncStayButtons(key);
}

// an exhausted direction shouldn't offer a button that does nothing
function syncStayButtons(key) {
  const total = stayMinutes(key);
  document.querySelectorAll(`.stay-stepper[data-via="${key}"] .stay-step`).forEach((btn) => {
    btn.disabled = Number(btn.dataset.step) > 0 ? total >= STAY_MAX : total <= 0;
  });
}

// − and + move to the neighbouring quarter hour, so a typed 0:07 snaps onto the
// grid rather than drifting along it
function stepStay(key, direction) {
  const total = stayMinutes(key);
  setStay(key, direction > 0
    ? (Math.floor(total / STAY_STEP) + 1) * STAY_STEP
    : (Math.ceil(total / STAY_STEP) - 1) * STAY_STEP);
}

// the picked stopovers of one direction, in row order
function viasFor(direction) {
  if (state.mode === "past") return [];
  return VIA_KEYS[direction].filter((k) => state[k]).map((k) => ({
    station: state[k],
    stay: stayMinutes(k),
  }));
}

function clearVia(key) {
  state[key] = null;
  document.getElementById(key).value = "";
  setStay(key, 0);
}

// keeps the stopover controls consistent with the state: which groups and rows
// show, and whether each add button still has a row left to reveal
function syncStopoverUI() {
  for (const [direction, keys] of Object.entries(VIA_KEYS)) {
    const returnGroup = direction === "return";
    // dropping the return trip drops its stopovers with it
    if (returnGroup && !state.returnTrip) {
      keys.forEach((k) => { clearVia(k); viaRow(k).classList.add("hidden"); });
    }
    document.getElementById(`stopover-group-${direction}`)
      .classList.toggle("hidden", returnGroup && !state.returnTrip);
    const anyHidden = keys.some((k) => viaRow(k).classList.contains("hidden"));
    document.getElementById(`stopover-add-${direction}`).classList.toggle("hidden", !anyHidden);
  }
  // the direction labels only mean something once there are two directions
  document.querySelectorAll(".stopover-group-label").forEach((el) =>
    el.classList.toggle("hidden", !state.returnTrip));
}

function revealVia(direction) {
  const key = VIA_KEYS[direction].find((k) => viaRow(k).classList.contains("hidden"));
  if (!key) return;
  viaRow(key).classList.remove("hidden");
  syncStopoverUI();
  document.getElementById(key).focus();
}

for (const direction of ["outbound", "return"]) {
  document.getElementById(`stopover-add-${direction}`).addEventListener("click", () => {
    revealVia(direction);
    track("stopover-add");
  });
}

document.querySelectorAll(".stay-stepper").forEach((stepper) => {
  const key = stepper.dataset.via;
  stepper.querySelectorAll(".stay-step").forEach((btn) => {
    btn.addEventListener("click", () => stepStay(key, Number(btn.dataset.step)));
  });
  // the native picker hands back HH:MM, or nothing when it was cleared
  viaStayTimeEl(key).addEventListener("change", () =>
    setStay(key, parseStay(viaStayTimeEl(key).value)));
  stepper.querySelectorAll(".stay-part").forEach((el) => {
    // focusing selects, so a segment is overwritten by typing rather than edited
    // around, and two digits in the hours hand the caret to the minutes
    el.addEventListener("focus", () => el.select());
    el.addEventListener("input", () => {
      el.value = el.value.replace(/\D/g, "");
      if (el.classList.contains("stay-h") && el.value.length === 2) viaStayEls(key)[1].select();
      syncStayButtons(key);
    });
    // what was typed only has to be readable, not well formed: it settles into
    // padded h/min on the way out
    el.addEventListener("change", () => setStay(key, stayMinutes(key)));
    el.addEventListener("keydown", (e) => {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      stepStay(key, e.key === "ArrowUp" ? 1 : -1);
    });
  });
  syncStayButtons(key);
});

document.querySelectorAll(".stopover-remove").forEach((btn) => {
  btn.addEventListener("click", () => {
    clearVia(btn.dataset.via);
    viaRow(btn.dataset.via).classList.add("hidden");
    syncStopoverUI();
  });
});

// --- past mode (compensation check) ---

function fmtDateFull(iso) {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}.${iso.slice(0, 4)}`;
}

// last day past mode can answer: the local data, extended to today where live
// IRIS lookups are available
function latestPastDay() {
  const c = state.coverage || {};
  return c.liveMaxDay && c.liveMaxDay > c.maxDay ? c.liveMaxDay : c.maxDay;
}

async function ensureCoverage() {
  if (state.coverage) return;
  try {
    const resp = await fetch("/api/coverage");
    if (resp.ok) state.coverage = await resp.json();
  } catch { /* no coverage info: skip client-side date bounds */ }
}

// one donate ask per view: the post-result nudge and the footer link never show together
function setDonateNudge(show) {
  show = show && DONATE_ENABLED;
  document.getElementById("donate-nudge").classList.toggle("hidden", !show);
  document.body.classList.toggle("nudge-on", show);
}

// --- feedback nudge ---

const feedbackEl = document.getElementById("feedback-nudge");
const feedbackLead = document.getElementById("feedback-lead");
const feedbackForm = document.getElementById("feedback-form");
const feedbackInput = document.getElementById("feedback-text");
const shotInput = document.getElementById("feedback-shot-input");
const shotAttach = document.getElementById("feedback-attach");
const shotChip = document.getElementById("feedback-shot-chip");
const shotThumb = document.getElementById("feedback-shot-thumb");
const shotError = document.getElementById("feedback-shot-error");

// optional screenshot riding along with the comment, as a base64 data URL
let feedbackShot = null;

// one id per prompt, so the vote and the comment that may follow become one row
let feedbackSid = null;

// randomUUID is missing outside a secure context, which local LAN dev hits
const newSid = () =>
  crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;

// every fresh result list gets a fresh ask: answering only closes the prompt
// for the list it was answered on, never for the visit or the browser
function resetFeedback() {
  feedbackSid = newSid();
  delete feedbackEl.dataset.vote;
  feedbackEl.classList.remove("feedback-voted", "feedback-done");
  feedbackForm.classList.add("hidden");
  feedbackInput.value = "";
  setShot(null);
  shotError.classList.add("hidden");
  feedbackLead.dataset.i18n = "feedbackAsk";
  feedbackLead.textContent = t("feedbackAsk");
}

// --- feedback screenshot ---

// the server refuses anything bigger, so the client shrinks until it fits
const SHOT_MAX_BYTES = 500 * 1024;
const SHOT_MAX_EDGE = 1600;

function setShot(url) {
  feedbackShot = url;
  shotThumb.src = url || "";
  shotChip.classList.toggle("hidden", !url);
  shotAttach.classList.toggle("hidden", !!url);
}

// via an <img> rather than createImageBitmap for the sake of older Safari
function loadShotImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => { URL.revokeObjectURL(url); resolve(img); };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("not a decodable image")); };
    img.src = url;
  });
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(file);
  });
}

// decoded size of a data URL's payload: base64 spends 4 chars per 3 bytes
const dataUrlBytes = (url) => ((url.length - url.indexOf(",") - 1) * 3) / 4;

async function shrinkShot(file) {
  const img = await loadShotImage(file); // rejects files that only claim to be images
  // small enough already: keep the original, PNG screenshots stay crisp that way
  if (file.size <= SHOT_MAX_BYTES && ["image/png", "image/jpeg", "image/webp"].includes(file.type)) {
    return readAsDataURL(file);
  }
  // shrink dimensions and JPEG quality together until it fits
  let scale = Math.min(1, SHOT_MAX_EDGE / Math.max(img.naturalWidth, img.naturalHeight));
  for (const quality of [0.85, 0.7, 0.55]) {
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(img.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(img.naturalHeight * scale));
    canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
    const url = canvas.toDataURL("image/jpeg", quality);
    if (dataUrlBytes(url) <= SHOT_MAX_BYTES) return url;
    scale *= 0.75;
  }
  throw new Error("still too large after shrinking");
}

async function attachShot(file) {
  if (!file) return;
  shotError.classList.add("hidden");
  try {
    setShot(await shrinkShot(file));
    track("feedback-shot");
  } catch {
    shotError.classList.remove("hidden");
  }
}

shotAttach.addEventListener("click", () => shotInput.click());

shotInput.addEventListener("change", () => {
  const file = shotInput.files[0];
  shotInput.value = ""; // so the same file can be picked again after a remove
  attachShot(file);
});

// an image pasted into the comment box counts as an attachment, not as text
feedbackInput.addEventListener("paste", (e) => {
  const item = [...e.clipboardData.items].find((i) => i.type.startsWith("image/"));
  if (!item) return;
  e.preventDefault();
  attachShot(item.getAsFile()); // getAsFile must happen before the event expires
});

document.getElementById("feedback-shot-remove").addEventListener("click", () => setShot(null));

// The follow row rides in the ask's own line, inserted before the dismiss button so
// ✕ stays the trailing affordance. Cloned from the footer rather than repeated in
// the markup, so the brand marks exist exactly once. Mounted once: the vote buttons
// hide themselves after a vote but this row stays for every state of the ask.
// The footer row is bare icons; next to "Hat dir das geholfen?" they need a word to
// say what clicking them does, so the label is added here rather than cloned.
(function mountFollowRow() {
  const row = document.querySelector(".footer-social").cloneNode(true);
  row.classList.remove("footer-social");
  row.classList.add("feedback-follow");
  const lead = document.createElement("span");
  lead.dataset.i18n = "followFeatures";
  lead.textContent = t("followFeatures");
  row.prepend(lead);
  feedbackEl.querySelector(".feedback-row").insertBefore(row, document.getElementById("feedback-skip"));
})();

// results have landed and the visitor isn't already looking at a donate ask
function setFeedbackNudge(show) {
  show = show && !DONATE_ENABLED;
  feedbackEl.classList.toggle("hidden", !show);
  if (show) resetFeedback();
}

// the visitor is doing us a favour: a failed send must never surface as an error
async function sendFeedback(vote, text, shot) {
  try {
    await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sid: feedbackSid, vote, text, shot: shot || "",
        lang: state.lang,
        context: PAST_PAGE ? "past" : "future",
      }),
    });
  } catch { /* swallowed by design */ }
}

function thankFeedback() {
  // swapping data-i18n keeps the line correct if the language is switched afterwards
  feedbackLead.dataset.i18n = "feedbackThanks";
  feedbackLead.textContent = t("feedbackThanks");
  feedbackForm.classList.add("hidden");
  feedbackEl.classList.add("feedback-done");
}

feedbackEl.querySelectorAll(".feedback-vote").forEach((btn) => {
  btn.addEventListener("click", () => {
    const vote = btn.dataset.vote;
    feedbackEl.dataset.vote = vote;
    // one event name per outcome: Umami's event list then shows the up/down split
    // directly, without depending on its per-event properties view
    track(`feedback-${vote}`);
    sendFeedback(vote, "");
    feedbackLead.dataset.i18n = "feedbackFollowUp";
    feedbackLead.textContent = t("feedbackFollowUp");
    feedbackEl.classList.add("feedback-voted");
    feedbackForm.classList.remove("hidden");
    feedbackInput.focus();
  });
});

// Enter now inserts a newline, so keep the keyboard path open the usual way
feedbackInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) feedbackForm.requestSubmit();
});

feedbackForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = feedbackInput.value.trim();
  if (text || feedbackShot) {
    track("feedback-text", { vote: feedbackEl.dataset.vote });
    sendFeedback(feedbackEl.dataset.vote, text, feedbackShot);
  }
  thankFeedback();
});

document.getElementById("feedback-skip").addEventListener("click", () => {
  track("feedback-dismiss");
  setFeedbackNudge(false);
});

// --- contact ---

// the mailto lives in the markup; JS only records the click
document.getElementById("contact-link").addEventListener("click", () => track("contact"));

// --- follow links ---

// delegated, so the row cloned into the feedback ask is covered too
document.addEventListener("click", (e) => {
  const link = e.target.closest(".social-link");
  if (link) track(`follow-${link.dataset.net}`);
});

// the sub-page's body class, banner and search-button label are baked into the
// served HTML; only the coverage-dependent date bounds need JS on load
async function initPastPage() {
  await ensureCoverage();
  if (state.coverage?.minDay) {
    // live IRIS lookups extend the pickable range to today
    const dateEl = document.getElementById("date");
    const maxDay = latestPastDay();
    dateEl.min = state.coverage.minDay;
    dateEl.max = maxDay;
    if (dateEl.value < dateEl.min || dateEl.value > dateEl.max) dateEl.value = maxDay;
    document.getElementById("past-coverage").textContent =
      `${fmtDateFull(state.coverage.minDay)} – ${fmtDateFull(maxDay)}`;
  }
}

document.getElementById("refund-cta").addEventListener("click", () => track("refund-cta"));
document.getElementById("stories-cta").addEventListener("click", () => track("stories-cta"));
document.getElementById("stories-banner").addEventListener("click", () => track("stories-banner"));
document.getElementById("refund-nav").addEventListener("click", () => track("refund-nav"));
document.getElementById("donate-footer-item").hidden = !DONATE_ENABLED;
document.getElementById("donate-footer").addEventListener("click", () =>
  track("donate", { placement: "footer" }));
document.querySelector("#donate-nudge a").addEventListener("click", () =>
  track("donate", { placement: "nudge" }));

// the result list shows one leg at a time; the return leg runs the search backwards
function searchLeg() {
  return state.leg === "outbound"
    ? { from: state.from, to: state.to, departure: state.departure, vias: viasFor("outbound") }
    : { from: state.to, to: state.from, departure: state.returnDeparture, vias: viasFor("return") };
}

// bahn.de rate-limits the session all our searches share, and says how long it
// wants us to wait (Retry-After, ~45 s, passed through on our 503). Waiting that
// out beats failing: the search then succeeds a little late instead of telling
// the user to come back. A fixed short retry used to land inside the same window
// and fail for certain.
const RETRYABLE = new Set([429, 502, 503, 504]);
// no or unparsable Retry-After: long enough not to pile straight back on
const RETRY_FALLBACK_S = 8;
// never sit on one wait longer than this, however long the header asks for
const RETRY_MAX_WAIT_S = 60;
// total time a search may spend waiting before giving up on it
const RETRY_MAX_TOTAL_S = 75;
// The return preflight gets a longer budget than a plain search: its result
// gates a list the user is already waiting for, and giving up means throwing
// away the outbound answer we just paid an upstream call for.
const PREFLIGHT_MAX_TOTAL_S = 150;

// Bumped by every new search intent, so a retry still counting down for an
// abandoned search neither renders into the new one nor keeps the button locked.
let searchGen = 0;

function retryAfterSeconds(resp) {
  const raw = (resp.headers.get("Retry-After") || "").trim();
  if (!raw) return RETRY_FALLBACK_S;
  const secs = Number(raw);
  if (Number.isFinite(secs) && secs >= 0) return secs;
  const when = Date.parse(raw);  // the HTTP-date form
  if (!Number.isNaN(when)) return Math.max(0, Math.round((when - Date.now()) / 1000));
  return RETRY_FALLBACK_S;
}

// Counts the wait down in the status line: a visible "retrying in 41 s" reads as
// progress, where a frozen spinner for the same 45 s reads as a hang.
// Resolves false when a newer search superseded this one.
async function waitBeforeRetry(seconds, gen, statusKey, ...params) {
  for (let left = Math.ceil(seconds); left > 0; left--) {
    if (gen !== searchGen) return false;
    statusEl.classList.remove("error");
    setStatus(statusKey, ...params, left);
    await new Promise((r) => setTimeout(r, 1000));
  }
  return gen === searchGen;
}

// opts: {leg} to fetch a leg other than the one on screen (the return preflight),
// {maxTotal, retryStatusKey, retryAgainStatusKey} to give that fetch its own
// retry budget and wording.
async function fetchJourneys(pagingRef, opts = {}) {
  const maxTotal = opts.maxTotal ?? RETRY_MAX_TOTAL_S;
  const retryStatusKey = opts.retryStatusKey ?? "overloadRetryIn";
  const retryAgainStatusKey = opts.retryAgainStatusKey ?? "overloadRetryAgainIn";
  const win = document.getElementById("window").value;
  const dticket = dticketMode();
  const travellers = travellersUsed();
  const leg = opts.leg ?? searchLeg();
  // opts.dir names the trip half being fetched when it is not the one on screen
  const dir = opts.dir ?? (state.leg === "return" ? "return" : "outbound");
  const params = new URLSearchParams({
    from: leg.from.id, to: leg.to.id, departure: leg.departure, window: win,
  });
  if (state.mode === "past") params.set("mode", "past");
  if (dticket !== "off") params.set("dticket", dticket);
  const rs = travellersParam();
  if (rs) params.set("travellers", rs);
  if (transferMinutes() !== "0") params.set("transfer", transferMinutes());
  const tm = productsParam(dir);
  if (tm) params.set("products", tm);
  if (pagingRef) params.set("pagingRef", pagingRef);
  (leg.vias || []).forEach((v, i) => {
    params.set(`via${i + 1}`, v.station.id);
    if (v.stay) params.set(`via${i + 1}Stay`, String(v.stay));
  });

  const gen = searchGen;
  let waited = 0;
  let attempt = 0;
  for (;;) {
    const resp = await fetch(`/api/journeys?${params}`);
    if (resp.ok) {
      state.windowUsed = Number(win);
      state.dticketUsed = dticket;
      state.travellersUsed = travellers;
      return resp.json();
    }
    // wait out the server's own cooldown, as often as its budget allows; the
    // floor keeps a Retry-After of 0 from spinning the loop against the budget
    const wait = Math.max(1, Math.min(retryAfterSeconds(resp), RETRY_MAX_WAIT_S));
    if (!RETRYABLE.has(resp.status) || waited + wait > maxTotal) {
      const body = await resp.json().catch(() => ({}));
      const err = new Error(body.detail || `HTTP ${resp.status}`);
      err.overloaded = RETRYABLE.has(resp.status);
      throw err;
    }
    waited += wait;
    attempt++;
    // from the second round on, a numbered "still busy" message keeps a fresh
    // countdown from reading as the first one looping
    const ok = attempt === 1
      ? await waitBeforeRetry(wait, gen, retryStatusKey)
      : await waitBeforeRetry(wait, gen, retryAgainStatusKey, attempt);
    if (!ok) {
      // a newer search took over: end this one quietly, it owns no UI any more
      throw Object.assign(new Error("superseded"), { superseded: true });
    }
  }
}

function lastArrival(journey) {
  const legs = journey?.legs || [];
  return (legs[legs.length - 1]?.plannedArrival || "").slice(0, 19);
}

// earliest departure a return may have: you cannot leave before the outbound lands
function returnFloor() {
  if (state.leg !== "return" || !state.outbound) return null;
  return lastArrival(state.outbound) || null;
}

// bahn.de answers a departure query with a window that reaches a little before
// the requested time, so the return list can start before the outbound lands
function usableJourneys(list) {
  const floor = returnFloor();
  if (!floor) return list;
  return list.filter((j) => ((j.legs || [])[0]?.plannedDeparture || "").slice(0, 19) >= floor);
}

function updatePageButtons() {
  let canPageEarlier = state.journeys.length > 0 && !!state.earlierRef;
  // the return list starts at the requested time, which can be hours after the
  // outbound lands — page back, but only down to the arrival
  const floor = returnFloor();
  if (canPageEarlier && floor) {
    canPageEarlier = ((state.journeys[0].legs || [])[0]?.plannedDeparture || "").slice(0, 19) > floor;
  }
  earlierBtn.classList.toggle("hidden", !canPageEarlier);
  laterBtn.classList.toggle("hidden", !state.laterRef);
}

async function resolveTyped(key) {
  // Typed but not picked from the dropdown: accept an exact name match.
  if (state[key]) return;
  const input = document.getElementById(key);
  const q = input.value.trim();
  if (q.length < 2) return;
  try {
    const resp = await fetch(`/api/locations?query=${encodeURIComponent(q)}`);
    if (!resp.ok) return;
    const items = await resp.json();
    const match = items.find((it) => it.name.toLowerCase() === q.toLowerCase());
    if (match) {
      state[key] = match;
      input.value = match.name;
    }
  } catch { /* network hiccup: leave unresolved */ }
}

function syncUrl() {
  // keep the search in the URL so refresh/bookmark/share restores the results
  const params = new URLSearchParams({
    fromId: state.from.id, from: state.from.name,
    toId: state.to.id, to: state.to.name,
    date: document.getElementById("date").value,
    time: document.getElementById("time").value,
    window: document.getElementById("window").value,
  });
  if (dticketMode() !== "off") params.set("dticket", dticketMode());
  if (travellersParam()) params.set("travellers", travellersParam());
  if (transferMinutes() !== "0") params.set("transfer", transferMinutes());
  const tm = productsParam("outbound");
  if (tm) params.set("tm", tm);
  // "all" marks a return deliberately left unfiltered next to a filtered outbound
  const tmr = productsParam("return");
  if (state.returnTrip && tmr !== tm) params.set("tmr", tmr || "all");
  if (state.returnTrip && returnDateEl.value) {
    params.set("rdate", returnDateEl.value);
    params.set("rtime", returnTimeEl.value);
  }
  const VIA_URL_PARAMS = { viaOut1: "vo1", viaOut2: "vo2", viaRet1: "vr1", viaRet2: "vr2" };
  for (const [direction, keys] of Object.entries(VIA_KEYS)) {
    for (const [i, via] of viasFor(direction).entries()) {
      // re-numbered on write, so a lone second-row pick restores as row one
      const p = VIA_URL_PARAMS[keys[i]];
      params.set(`${p}Id`, via.station.id);
      params.set(p, via.station.name);
      if (via.stay) params.set(`${p}Stay`, String(via.stay));
    }
  }
  history.replaceState(null, "", `?${params}`);
}

async function search() {
  // stopover fields count like from/to, but only where their direction is live
  const viaKeys = state.mode === "past" ? []
    : [...VIA_KEYS.outbound, ...(state.returnTrip ? VIA_KEYS.return : [])]
      .filter((k) => !viaRow(k).classList.contains("hidden"));
  await Promise.all([resolveTyped("from"), resolveTyped("to"),
    ...viaKeys.map((k) => resolveTyped(k))]);
  if (!state.from || !state.to) {
    setStatus("pickStations");
    statusEl.classList.add("error");
    return;
  }
  // typed but unresolved stopover text must not silently search without it
  if (viaKeys.some((k) => !state[k] && document.getElementById(k).value.trim())) {
    setAdvancedOpen(true);  // the field the error is about must be visible
    setStatus("stopoverUnresolved");
    statusEl.classList.add("error");
    return;
  }
  syncStopoverUI();  // resolveTyped may have just turned text into a pick
  saveRecent(state.from);
  saveRecent(state.to);
  viaKeys.forEach((k) => { if (state[k]) saveRecent(state[k]); });
  if (state.mode === "past") {
    await ensureCoverage();
    const day = document.getElementById("date").value;
    const latest = latestPastDay();
    if (state.coverage?.minDay && day < state.coverage.minDay) {
      setStatus("dateOutOfRange", fmtDateFull(state.coverage.minDay), fmtDateFull(latest));
      statusEl.classList.add("error");
      return;
    }
    // days past the local data but within the live range are answered from IRIS
    state.liveDay = !!(state.coverage?.maxDay && day > state.coverage.maxDay);
    if (latest && day > latest) {
      // data for a day normally lands the next morning; if that morning has
      // already passed, the pipeline is running behind
      const next = new Date(`${day}T12:00:00`);
      next.setDate(next.getDate() + 1);
      const nextIso = next.toISOString().slice(0, 10);
      const todayIso = new Date().toLocaleDateString("sv-SE");  // local YYYY-MM-DD
      if (nextIso > todayIso) setStatus("dateNotYet", fmtDateFull(nextIso));
      else setStatus("dateNotYetLag");
      statusEl.classList.add("error");
      return;
    }
  }
  state.departure = `${dateEl.value}T${document.getElementById("time").value}:00`;
  if (state.returnTrip) {
    if (!returnDateEl.value || !returnTimeEl.value) {
      setStatus("returnIncomplete");
      statusEl.classList.add("error");
      return;
    }
    if (returnDepartureIso() < state.departure) {
      setStatus("returnBeforeOutbound");
      statusEl.classList.add("error");
      return;
    }
    state.returnDeparture = returnDepartureIso();
  } else {
    state.returnDeparture = null;
  }
  // a fresh search always restarts at the outbound leg
  state.leg = "outbound";
  state.outbound = null;
  state.outboundResults = null;
  state.returnJourney = null;
  state.returnResults = null;
  state.returnPrefetch = null;
  syncUrl();
  const party = travellersUsed();
  track("search", {
    from: state.from.name,
    to: state.to.name,
    window: Number(document.getElementById("window").value),
    mode: state.mode,
    dticket: dticketMode(),
    age: party[0].age,
    travellers: party.reduce((sum, tr) => sum + tr.count, 0),
    discount: party.find((tr) => tr.discount !== "none")?.discount || "none",
    transfer: Number(transferMinutes()),
    returnTrip: state.returnTrip,
    stopovers: viasFor("outbound").length + viasFor("return").length,
    transport: productsParam("outbound") || "all",
  });
  await runSearch();
}

// fetches and renders the leg the flow is currently on; search() sets that up,
// the window/D-Ticket toggles reuse it to refetch without leaving the leg
async function runSearch() {
  const gen = ++searchGen;  // supersedes any retry still counting down
  statusEl.classList.remove("error");
  setStatus("searching");
  setStaleNotice(0);
  resultsEl.innerHTML = "";
  controlsEl.classList.add("hidden");
  earlierBtn.classList.add("hidden");
  laterBtn.classList.add("hidden");
  document.getElementById("hero-chart").classList.add("hidden");
  document.getElementById("refund-cta").classList.add("hidden");
  document.getElementById("stories-cta").classList.add("hidden");
  setDonateNudge(false);
  setFeedbackNudge(false);
  searchBtn.disabled = true;
  renderTripSteps();

  try {
    const data = await fetchJourneys(null);
    // an outbound list the user can act on implies a return list behind it
    if (state.returnTrip && state.leg === "outbound" && (data.journeys || []).length) {
      await preflightReturn();
    }
    state.journeys = usableJourneys(data.journeys || []);
    state.earlierRef = data.earlierRef || null;
    state.laterRef = data.laterRef || null;
    setStaleNotice(data.staleSeconds || 0);
    if (state.journeys.length) setStatus(null);
    else setStatus("noResults");
    controlsEl.classList.toggle("hidden", state.journeys.length === 0);
    document.getElementById("past-disclaimer").classList.toggle(
      "hidden", !(state.mode === "past" && state.journeys.length));
    setDonateNudge(state.journeys.length > 0);
    setFeedbackNudge(state.journeys.length > 0);
    updatePageButtons();
    render();
  } catch (e) {
    if (e.superseded) return;  // a newer search owns the UI now
    // the outbound may well have come back fine: say which half is missing
    if (e.returnLeg && e.overloaded) setStatus("returnUnavailable");
    else if (e.overloaded) setStatus("overloadFail");
    else setStatus("error", e.message);
    statusEl.classList.add("error");
  } finally {
    // a superseded search must not re-enable the button under the new one
    if (gen === searchGen) {
      searchBtn.disabled = false;
      // the steps behind this one only become pressable now the leg has landed
      renderTripSteps();
    }
  }
}

// --- two-step round trip: outbound list, then return list ---

// bahn.de throttles us hard, and step 2 only runs once the user has picked an
// outbound — so a return leg that cannot be fetched used to surface as a dead
// end two steps in, or (worse) as a cached answer for some other time of day.
// Prove the return answerable at the requested time before the outbound list is
// shown at all, and hand that proven answer to step 2 rather than asking twice.
async function preflightReturn() {
  setStatus("returnChecking");
  const leg = { from: state.to, to: state.from, departure: state.returnDeparture, vias: viasFor("return") };
  try {
    const data = await fetchJourneys(null, {
      leg,
      dir: "return",
      maxTotal: PREFLIGHT_MAX_TOTAL_S,
      retryStatusKey: "returnRetryIn",
      retryAgainStatusKey: "returnRetryAgainIn",
    });
    state.returnPrefetch = { departure: leg.departure, data };
  } catch (e) {
    e.returnLeg = true;
    throw e;
  }
}

function fmtTripDay(iso) {
  return new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString(
    state.lang === "de" ? "de-DE" : "en-GB", { weekday: "short", day: "numeric", month: "short" });
}

// the round trip's steps in order; `state.leg` is always one of these
const STEPS = ["outbound", "return", "summary"];

// stepping back while a leg is still loading would land on the list the
// in-flight fetch is about to replace
function goToStep(back) {
  if (searchBtn.disabled) return;
  back();
}

function renderTripSteps() {
  tripStepsEl.innerHTML = "";
  const show = state.returnTrip && state.mode !== "past" && state.from && state.to;
  tripStepsEl.classList.toggle("hidden", !show);
  if (!show) return;

  const stepper = document.createElement("ol");
  stepper.className = "trip-stepper";
  [
    { leg: "outbound", label: "stepOutbound", from: state.from, to: state.to, when: state.departure },
    { leg: "return", label: "stepReturn", from: state.to, to: state.from,
      when: state.returnDeparture || (returnDateEl.value ? returnDepartureIso() : null) },
    { leg: "summary", label: "stepSummary" },
  ].forEach((step, i) => {
    const li = document.createElement("li");
    li.className = `trip-step${state.leg === step.leg ? " active" : ""}`;
    const label = Object.assign(document.createElement("strong"), {
      textContent: `${i + 1}. ${t(step.label)}`,
    });
    const detail = Object.assign(document.createElement("span"), {
      textContent: step.from
        ? `${step.from.name} → ${step.to.name}` + (step.when ? ` · ${fmtTripDay(step.when)}` : "")
        : t("stepSummaryHint"),
    });
    // only steps already behind the flow are reachable: going forward needs a
    // pick, and leaving a step drops the pick that was made on it. While a leg
    // is loading nothing is pressable — the list it would return to is about to
    // be replaced, and a dead button reads worse than a plain one
    const back = !searchBtn.disabled && i < STEPS.indexOf(state.leg)
      ? [backToOutbound, backToReturn][i]
      : null;
    if (back) {
      li.classList.add("trip-step-done");
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "trip-step-btn";
      btn.setAttribute("aria-label", t("stepBack", i + 1, t(step.label)));
      btn.append(label, detail);
      btn.addEventListener("click", () => goToStep(back));
      li.appendChild(btn);
    } else {
      li.append(label, detail);
    }
    stepper.appendChild(li);
  });
  tripStepsEl.appendChild(stepper);

  // step 3 shows both journeys in full, so the one-line recap only earns its
  // space while the return list is still being picked from
  if (state.leg !== "return" || !state.outbound) return;
  const legs = state.outbound.legs || [];
  const first = legs[0], last = legs[legs.length - 1];
  const trains = legs.filter((l) => !l.walking).map((l) => l.line?.name).filter(Boolean).join(", ");
  const picked = document.createElement("div");
  picked.className = "trip-picked";
  const change = document.createElement("button");
  change.type = "button";
  change.className = "trip-change";
  change.textContent = t("changeOutbound");
  change.disabled = searchBtn.disabled;
  change.addEventListener("click", () => goToStep(backToOutbound));
  picked.append(
    Object.assign(document.createElement("span"), {
      textContent: `${t("outboundPicked")} ${fmtTripDay(first.plannedDeparture)}, ` +
        `${fmtTime(first.plannedDeparture)} → ${fmtTime(last.plannedArrival)}` +
        (trains ? ` · ${trains}` : ""),
    }),
    change);
  tripStepsEl.appendChild(picked);
}

function selectOutbound(journey) {
  state.outbound = journey;
  // keep the list so "change" can go back without a refetch
  state.outboundResults = {
    journeys: state.journeys, earlierRef: state.earlierRef, laterRef: state.laterRef,
  };
  const arrival = lastArrival(journey);
  const wanted = returnDepartureIso();
  // the return can't leave before the outbound lands
  state.returnDeparture = arrival && wanted < arrival ? arrival : wanted;
  state.leg = "return";
  track("return-continue", { from: state.from?.name, to: state.to?.name });
  // the preflight already answered this exact query; only a return clamped up to
  // the outbound arrival, or return fields edited since, needs asking again
  const pre = state.returnPrefetch;
  if (pre && pre.departure === state.returnDeparture) {
    state.returnPrefetch = null;
    setStaleNotice(pre.data.staleSeconds || 0);
    restoreList({
      journeys: usableJourneys(pre.data.journeys || []),
      earlierRef: pre.data.earlierRef || null,
      laterRef: pre.data.laterRef || null,
    });
    return;
  }
  runSearch();
}

function selectReturn(journey) {
  state.returnJourney = journey;
  // keep the list so "change" can go back without a refetch
  state.returnResults = {
    journeys: state.journeys, earlierRef: state.earlierRef, laterRef: state.laterRef,
  };
  state.leg = "summary";
  track("summary-open", {
    from: state.from?.name, to: state.to?.name, price: tripTotal() ?? "na",
  });
  // step 3 has no list of its own: nothing to fetch, sort or page
  statusEl.classList.remove("error");
  setStatus(null);
  controlsEl.classList.add("hidden");
  earlierBtn.classList.add("hidden");
  laterBtn.classList.add("hidden");
  renderTripSteps();
  render();
  tripStepsEl.scrollIntoView({ behavior: "smooth", block: "start" });
}

// both legs carry the "ab" price of a one-way; a total only means something
// once bahn.de has priced both
function tripTotal() {
  // a D-Ticket-covered leg costs nothing on top of the ticket the user holds
  const price = (j) => j?.dticketCovered ? 0 : j?.price;
  const out = price(state.outbound), ret = price(state.returnJourney);
  return out != null && ret != null ? out + ret : null;
}

// how the trip total reads: fully covered only if both legs are, partial as soon
// as the D-Ticket paid for any part of the trip
function tripPriceKind() {
  const out = state.outbound, ret = state.returnJourney;
  const covered = (j) => !!j?.dticketCovered;
  return {
    dticketCovered: covered(out) && covered(ret),
    pricePartial: covered(out) || covered(ret) || out?.pricePartial || ret?.pricePartial,
  };
}

function restoreList(cached) {
  state.journeys = cached.journeys;
  state.earlierRef = cached.earlierRef;
  state.laterRef = cached.laterRef;
  statusEl.classList.remove("error");
  setStatus(state.journeys.length ? null : "noResults");
  controlsEl.classList.toggle("hidden", state.journeys.length === 0);
  updatePageButtons();
  renderTripSteps();
  render();
}

function backToOutbound() {
  state.leg = "outbound";
  state.outbound = null;
  // the return was picked to follow the outbound now being dropped
  state.returnJourney = null;
  state.returnResults = null;
  const cached = state.outboundResults;
  if (!cached) {
    // dropped because the stats window or D-Ticket filter changed meanwhile
    renderTripSteps();
    runSearch();
    return;
  }
  restoreList(cached);
}

function backToReturn() {
  state.leg = "return";
  state.returnJourney = null;
  const cached = state.returnResults;
  if (!cached) {
    renderTripSteps();
    runSearch();
    return;
  }
  restoreList(cached);
}

function journeyKey(j) {
  const legs = j.legs || [];
  const trains = legs.filter((l) => !l.walking).map((l) => l.line?.name).join("|");
  return `${legs[0]?.plannedDeparture}|${legs[legs.length - 1]?.plannedArrival}|${trains}`;
}

async function loadPage(dir) {
  const ref = dir === "earlier" ? state.earlierRef : state.laterRef;
  if (!ref) return;
  const btn = dir === "earlier" ? earlierBtn : laterBtn;
  // counted on intent, not on success: a page that fails upstream is still a
  // user who wanted one. How often this fires decides whether prefetching the
  // adjacent pages is worth the extra bahn.de calls.
  track("page", { dir, leg: state.leg });
  const gen = ++searchGen;  // supersedes any retry still counting down
  btn.disabled = true;
  statusEl.classList.remove("error");

  try {
    const data = await fetchJourneys(ref);
    const raw = data.journeys || [];
    const usable = usableJourneys(raw);
    const seen = new Set(state.journeys.map(journeyKey));
    const fresh = usable.filter((j) => !seen.has(journeyKey(j)));
    if (dir === "earlier") {
      state.journeys = [...fresh, ...state.journeys];
      // this page already reached past the outbound arrival: nothing usable is left behind it
      state.earlierRef = raw.length > usable.length ? null : (data.earlierRef || null);
    } else {
      state.journeys = [...state.journeys, ...fresh];
      state.laterRef = data.laterRef || null;
    }
    // a retry may have left its waiting message behind; results are here now
    setStatus(null);
    if (data.staleSeconds) setStaleNotice(data.staleSeconds);
    updatePageButtons();
    render();
  } catch (e) {
    if (e.superseded) return;  // a newer search owns the UI now
    if (e.overloaded) setStatus("overloadFail");
    else setStatus("error", e.message);
    statusEl.classList.add("error");
  } finally {
    if (gen === searchGen) btn.disabled = false;
  }
}

// --- language toggle ---

// The buttons are links to the other language's URL, so switching is a navigation,
// not an in-place text swap. The stored choice only decides where a returning
// visitor who lands on a German URL gets sent (see the redirect in index.html).
document.querySelectorAll(".lang-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.lang === state.lang) return;
    track("lang", { lang: btn.dataset.lang });
    try { localStorage.setItem("lang", btn.dataset.lang); } catch (e) {}
  });
});

document.querySelectorAll(".chart-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    const open = state.chart !== btn.dataset.chart;  // clicking the open chart collapses it again
    document.querySelectorAll(".chart-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.toggle("active", open);
    state.chart = open ? btn.dataset.chart : null;
    updateChartImg();
    if (open) track("hero-chart", { chart: state.chart });
  });
});

// --- sorting ---

document.querySelectorAll(".sort-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".sort-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.sort = btn.dataset.sort;
    track("sort", { mode: state.sort });
    render();
  });
});

function sortedJourneys() {
  const js = [...state.journeys];
  if (state.sort === "delay") {
    const past = state.mode === "past";
    const score = (j) => (past ? j.arrivalDelay : j.delayScore);
    // past mode: simulated arrivalDelay already reflects missed connections;
    // journeys whose outcome stayed unknown have a null score and sort last
    const unlikely = (j) => (past ? false : (j.tightTransfers || []).some((tt) => tt.unlikely));
    js.sort((a, b) => {
      const aMissing = score(a) == null, bMissing = score(b) == null;
      if (aMissing !== bMissing) return aMissing ? 1 : -1;  // missing data last
      if (aMissing && bMissing) return 0;
      const aUnlikely = unlikely(a), bUnlikely = unlikely(b);
      if (aUnlikely !== bUnlikely) return aUnlikely ? 1 : -1;  // likely-missed connections after reliable ones
      if (score(a) !== score(b)) return score(a) - score(b);
      return (a.maxLegMedianDelay ?? 0) - (b.maxLegMedianDelay ?? 0);
    });
  } else if (state.sort === "price") {
    // a D-Ticket-covered journey carries no price but costs nothing on top of the
    // ticket, so it is the cheapest option, not an unpriced one
    const price = (j) => (j.dticketCovered ? 0 : j.price);
    js.sort((a, b) => {
      const aMissing = price(a) == null, bMissing = price(b) == null;
      if (aMissing !== bMissing) return aMissing ? 1 : -1;  // no price last
      if (aMissing && bMissing) return 0;
      return price(a) - price(b);  // stable sort keeps departure order on ties
    });
  } else if (state.sort === "transfers") {
    const transferCount = (j) =>
      j.transfers ?? Math.max(0, (j.legs || []).filter((l) => !l.walking).length - 1);
    js.sort((a, b) => transferCount(a) - transferCount(b));  // stable sort keeps departure order on ties
  } else if (state.sort === "risk") {
    const past = state.mode === "past";
    // tiers mirror the header pills: no risk < yellow tight transfer < red connection risk;
    // past mode ranks by what actually happened - journeys with missed connections last
    const tier = (j) => {
      if (past) return (j.missedTransfers || []).length ? 2 : 0;
      const tts = j.tightTransfers || [];
      return tts.some((tt) => tt.unlikely) ? 2 : tts.length ? 1 : 0;
    };
    // within a tier, rank by the riskiest transfer's slack (transfer time minus the
    // arriving leg's median delay): direct journeys first, unknown delay data last
    const margin = (j) => {
      const trainLegs = (j.legs || []).filter((l) => !l.walking);
      if ((j.transfers ?? trainLegs.length - 1) <= 0) return Infinity;
      return j.minTransferMargin ?? -Infinity;
    };
    js.sort((a, b) => {
      const ta = tier(a), tb = tier(b);
      if (ta !== tb) return ta - tb;
      if (!past) {
        const ma = margin(a), mb = margin(b);
        if (ma !== mb) return mb - ma;  // biggest slack first
      }
      return 0;  // stable sort keeps departure order on ties
    });
  }
  return js;
}

// --- rendering ---

function fmtTime(iso) {
  // sollzeit is Berlin-local naive, e.g. "2026-07-13T09:36:00" - show as-is
  return iso ? iso.slice(11, 16) : "–";
}

// planned walk duration in whole minutes; both stamps are Berlin-local naive, so
// parsing them in the browser's zone cancels out
function walkMinutes(leg) {
  if (!leg.plannedDeparture || !leg.plannedArrival) return null;
  const mins = Math.round(
    (new Date(leg.plannedArrival) - new Date(leg.plannedDeparture)) / 60000);
  return mins > 0 ? mins : null;
}

function fmtDuration(seconds) {
  if (seconds == null) return "";
  const mins = Math.round(seconds / 60);
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}min`;
}

// today's connections carry live (echtzeit) times where they deviate from the
// schedule; show the struck-out schedule next to the live time, red like on bahn.de
function timeNode(planned, live) {
  if (!live) return document.createTextNode(fmtTime(planned));
  const frag = document.createDocumentFragment();
  frag.append(
    Object.assign(document.createElement("s"), {
      className: "time-planned", textContent: fmtTime(planned),
    }),
    document.createTextNode(" "),
    Object.assign(document.createElement("span"), {
      className: "time-live", textContent: fmtTime(live), title: t("liveTimeTooltip"),
    }),
  );
  return frag;
}

// products the backend collects no delay data for (matches UNTRACKED_PRODUCTS in app/main.py)
const UNTRACKED_PRODUCTS = new Set(["BUS", "TRAM", "UBAHN", "SCHIFF", "ANRUFPFLICHTIG"]);

function notTrackedBadge() {
  const el = document.createElement("span");
  el.className = "badge gray";
  el.textContent = t("notTracked");
  el.title = t("notTrackedTooltip");
  return el;
}

function delayBadge(stats, big) {
  // badges with per-day data become buttons that toggle the day chart
  const clickable = !!stats?.days?.length;
  const el = document.createElement(clickable ? "button" : "span");
  el.className = "badge";
  if (clickable) el.type = "button";
  if (!stats || stats.medianDelay == null) {
    el.classList.add("gray");
    el.textContent = t("noData");
  } else {
    const v = stats.medianDelay;
    el.classList.add(v < 3 ? "green" : v < 10 ? "yellow" : "red");
    el.innerHTML = `${v >= 0 ? "+" : ""}${v} min${big ? ` <small>${t("badgeDays", stats.daysMatched, state.windowUsed)}</small>` : ""}`;
    el.title = t("badgeTooltip", state.windowUsed, stats.maxDelay);
  }
  if (clickable) {
    el.title = (el.title ? `${el.title} – ` : "") + t("badgeClickHint");
    const caret = document.createElement("span");
    caret.className = "badge-caret";
    caret.setAttribute("aria-hidden", "true");
    caret.textContent = "▾";
    el.append(caret);
  }
  return el;
}

// past mode: badge for a concrete delay value in minutes
function delayValueBadge(v, title) {
  const el = document.createElement("span");
  el.className = `badge ${v < 3 ? "green" : v < 10 ? "yellow" : "red"}`;
  el.textContent = `${v >= 0 ? "+" : ""}${v} min`;
  if (title) el.title = title;
  return el;
}

// past mode: the actual delay of one leg on the searched day
function exactDelayBadge(d) {
  const reason = reasonText(d?.reason);
  if (d && !d.canceled) {
    return delayValueBadge(d.delayMin, t("thatDayTooltip") + (reason ? ` – ${reason}` : ""));
  }
  const el = document.createElement("span");
  el.className = "badge";
  if (!d) {
    el.classList.add("gray");
    // on a live day the arrival simply hasn't been reported yet, which is not
    // the same as having no data for this train at all
    el.textContent = state.liveDay ? t("notYetBadge") : t("noData");
    if (state.liveDay) el.title = t("notYetTooltip");
  } else {
    el.classList.add("red");
    el.textContent = t("chartCanceled");
    if (reason) el.title = reason;
  }
  return el;
}

// one leg row (train or walk); struck = leg was missed in the simulated journey
function buildLegRow(leg, past, struck) {
  const row = document.createElement("div");
  row.className = "leg";
  if (leg.walking) {
    row.classList.add("leg-walk");
    const w = document.createElement("span");
    w.className = "walk";
    // the walk eats into the transfer buffer, so its duration is worth naming
    const mins = walkMinutes(leg);
    w.textContent = [
      t("walk"),
      mins != null ? t("walkMinutes", mins) : null,
      `${leg.origin?.name || ""} → ${leg.destination?.name || ""}`,
    ].filter(Boolean).join(" · ");
    row.appendChild(w);
    return row;
  }
  const train = document.createElement("span");
  train.className = "train";
  train.textContent = leg.line?.name || t("train");
  const desc = document.createElement("span");
  desc.className = "leg-desc";
  // past mode shows the schedule; the actual delay is the story of the badge next to it
  desc.append(
    document.createTextNode(`${leg.origin?.name || ""} `),
    timeNode(leg.plannedDeparture, past ? null : leg.departure),
    document.createTextNode(` → ${leg.destination?.name || ""} `),
    timeNode(leg.plannedArrival, past ? null : leg.arrival),
  );
  let badge;
  if (struck) {
    badge = document.createElement("span");
    if (leg.delayOnDate?.canceled) {
      badge.className = "badge red";
      badge.textContent = t("chartCanceled");
    } else {
      badge.className = "badge gray";
      badge.textContent = t("missedLegBadge");
    }
  } else if (UNTRACKED_PRODUCTS.has(leg.line?.product)) {
    badge = notTrackedBadge();
  } else {
    badge = past ? exactDelayBadge(leg.delayOnDate) : delayBadge(leg.delayStats, false);
  }
  row.append(train, desc, badge);
  return row;
}

// future mode: the next realistic connection if a tight transfer is missed,
// expanded on demand under the warning strip
function buildIfMissedPanel(tt) {
  const panel = document.createElement("div");
  panel.className = "if-missed-panel";
  const lead = document.createElement("div");
  lead.className = "if-missed-lead";
  lead.textContent = t("ifMissedLead");
  panel.appendChild(lead);
  const altLegs = tt.ifMissed.legs;
  altLegs.forEach((leg, i) => {
    const row = buildLegRow(leg, false, false);
    if (i === 0) row.classList.add("rail-first");
    if (i === altLegs.length - 1) row.classList.add("rail-last");
    if (!leg.walking && leg.delayStats) {
      wireDayChart(row.querySelector(".badge"), leg.delayStats, row, leg.line?.name);
    }
    panel.appendChild(row);
  });
  return panel;
}

// --- per-day delay chart ---

function wireDayChart(badge, stats, refEl, trainName) {
  if (badge.tagName !== "BUTTON") return;
  let panel = null;
  badge.setAttribute("aria-expanded", "false");
  badge.addEventListener("click", () => {
    if (panel) {
      panel.remove();
      panel = null;
      badge.setAttribute("aria-expanded", "false");
      return;
    }
    panel = buildDayChart(stats, refEl);
    badge.setAttribute("aria-expanded", "true");
    track("day-chart", { train: trainName });
  });
}

function fmtDay(iso) {
  return `${iso.slice(8, 10)}.${iso.slice(5, 7)}.`;
}

function svgEl(tag, attrs, text) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  if (text != null) el.textContent = text;
  return el;
}

function tickStep(range) {
  for (const s of [1, 2, 5, 10, 15, 20, 30, 60, 90, 120, 180, 240, 360]) {
    if (range / s <= 5) return s;
  }
  return Math.ceil(range / 5);
}

// bar growing from the baseline: square there, 4px-rounded at the data end
function barPath(x, w, yBase, yTip) {
  const up = yTip < yBase;
  const r = Math.min(4, w / 2, Math.abs(yBase - yTip));
  const yr = up ? yTip + r : yTip - r;
  return `M${x},${yBase} L${x},${yr} Q${x},${yTip} ${x + r},${yTip} L${x + w - r},${yTip} ` +
    `Q${x + w},${yTip} ${x + w},${yr} L${x + w},${yBase} Z`;
}

function buildDayChart(stats, refEl) {
  const panel = document.createElement("div");
  panel.className = "day-chart";

  const caption = document.createElement("div");
  caption.className = "day-chart-caption";
  const capText = t("chartDayCaption", state.windowUsed);
  caption.appendChild(Object.assign(document.createElement("span"), { textContent: capText }));
  if (stats.canceledDays) {
    const legend = document.createElement("span");
    legend.className = "day-chart-cancel";
    legend.textContent = `✕ ${t("chartCanceled")}`;
    caption.appendChild(legend);
  }
  panel.appendChild(caption);
  refEl.insertAdjacentElement("afterend", panel);  // insert first so we can measure width

  // one slot per calendar day of the window, so untracked days show as gaps
  const byDay = new Map(stats.days.map((d) => [d.day, d]));
  const slots = [];
  const cursor = new Date(`${stats.windowStart}T00:00:00Z`);
  for (let i = 0; i < 40; i++) {
    const iso = cursor.toISOString().slice(0, 10);
    slots.push({ iso, rec: byDay.get(iso) || null });
    if (iso >= stats.windowEnd) break;
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }

  const W = Math.max(320, panel.clientWidth || 640);
  const H = 190;
  const m = { top: 16, right: 8, bottom: 24, left: 38 };
  const plotW = W - m.left - m.right;
  const plotH = H - m.top - m.bottom;

  const values = stats.days.map((d) => d.delay).filter((v) => v != null);
  const step = tickStep(Math.max(5, ...values) - Math.min(0, ...values));
  const yMax = Math.ceil(Math.max(5, ...values) / step) * step;
  const yMin = Math.floor(Math.min(0, ...values) / step) * step;
  const y = (v) => m.top + (plotH * (yMax - v)) / (yMax - yMin);

  const svg = svgEl("svg", {
    viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img", "aria-label": capText,
  });

  for (let v = yMin; v <= yMax; v += step) {
    svg.appendChild(svgEl("line", {
      x1: m.left, x2: W - m.right, y1: y(v), y2: y(v),
      stroke: v === 0 ? "#c9ced4" : "#e6eaee", "stroke-width": 1, "shape-rendering": "crispEdges",
    }));
    svg.appendChild(svgEl("text", {
      x: m.left - 6, y: y(v) + 3, "text-anchor": "end", "font-size": 10, fill: "#646973",
    }, String(v)));
  }

  const band = plotW / slots.length;
  const barW = Math.min(24, Math.max(2, band - 2));  // 2px surface gap between bars
  const labelEvery = slots.length <= 10 ? 1 : slots.length <= 16 ? 2 : 5;
  const showVals = band >= 18;  // value labels only where adjacent ones can't collide
  const valSize = slots.length <= 10 ? 10 : 9;
  const colors = { green: "#2a7230", yellow: "#b8860b", red: "#c50014" };

  // clicking/tapping a day shows its details (incl. delay reason) in a bubble
  // above the bar — hover tooltips don't exist on touch screens
  const bubble = document.createElement("div");
  bubble.className = "day-chart-bubble";
  bubble.hidden = true;
  let selected = null;
  const closeBubble = () => {
    if (selected) selected.setAttribute("fill", "transparent");
    selected = null;
    bubble.hidden = true;
    document.removeEventListener("click", closeBubble);
  };
  const selectDay = (hit, title, tipY) => {
    if (selected === hit) return closeBubble();
    if (selected) selected.setAttribute("fill", "transparent");
    else document.addEventListener("click", closeBubble);  // click-away closes
    selected = hit;
    hit.setAttribute("fill", "rgba(21, 25, 30, 0.07)");
    bubble.textContent = title;
    bubble.hidden = false;
    // above the bar tip, centered on the column, clamped into the panel; the
    // svg scales with the panel width, so measure in CSS pixels at click time
    const panelR = panel.getBoundingClientRect();
    const svgR = svg.getBoundingClientRect();
    const hitR = hit.getBoundingClientRect();
    const cx = hitR.left + hitR.width / 2 - panelR.left;
    const left = Math.max(4, Math.min(cx - bubble.offsetWidth / 2, panelR.width - bubble.offsetWidth - 4));
    const tip = svgR.top - panelR.top + (tipY * svgR.height) / H;
    bubble.style.left = `${left}px`;
    bubble.style.top = `${Math.max(0, tip - bubble.offsetHeight - 8)}px`;
    bubble.style.setProperty("--arrow-x", `${Math.max(10, Math.min(cx - left, bubble.offsetWidth - 10))}px`);
  };

  slots.forEach((slot, i) => {
    const x0 = m.left + i * band;
    const cx = x0 + band / 2;
    const rec = slot.rec;

    const reason = reasonText(rec?.reason);
    let title = `${fmtDay(slot.iso)} ${t("noData")}`;
    if (rec?.canceled) {
      title = `${fmtDay(slot.iso)} ${t("chartCanceled")}${reason ? ` – ${reason}` : ""}`;
      svg.appendChild(svgEl("text", {
        x: cx, y: y(0) - 5, "text-anchor": "middle",
        "font-size": 13, "font-weight": 700, fill: colors.red,
      }, "✕"));
    } else if (rec) {
      const v = rec.delay;
      title = `${fmtDay(slot.iso)} ${v >= 0 ? "+" : ""}${v} min${reason ? ` – ${reason}` : ""}`;
      const fill = v < 3 ? colors.green : v < 10 ? colors.yellow : colors.red;
      if (v !== 0) {
        svg.appendChild(svgEl("path", { d: barPath(cx - barW / 2, barW, y(0), y(v)), fill }));
      } else {
        // baseline stub so an on-time day is distinguishable from a no-data gap
        svg.appendChild(svgEl("rect", { x: cx - barW / 2, y: y(0) - 2, width: barW, height: 2, fill }));
      }
      if (showVals) {
        svg.appendChild(svgEl("text", {
          x: cx, y: v >= 0 ? y(v) - 4 : y(v) + 11, "text-anchor": "middle",
          "font-size": valSize, fill: "#646973",
        }, `${v >= 0 ? "+" : ""}${v}`));
      }
    }

    if (i % labelEvery === 0) {
      svg.appendChild(svgEl("text", {
        x: cx, y: H - 8, "text-anchor": "middle", "font-size": 10, fill: "#646973",
      }, fmtDay(slot.iso)));
    }

    // bubble anchor: top of the bar, clearing the ✕ glyph / the value label
    let tipY = y(0);
    if (rec?.canceled) tipY = y(0) - 18;
    else if (rec) tipY = y(Math.max(rec.delay, 0)) - (showVals && rec.delay >= 0 ? 14 : 0);

    // full-height hover/click target with a native tooltip
    const hit = svgEl("rect", { x: x0, y: m.top, width: band, height: plotH, fill: "transparent", cursor: "pointer" });
    hit.appendChild(svgEl("title", {}, title));
    hit.addEventListener("click", (e) => {
      e.stopPropagation();
      selectDay(hit, title, tipY);
    });
    svg.appendChild(hit);
  });

  panel.appendChild(svg);
  panel.appendChild(bubble);
  return panel;
}

// the traveller ids of bahn.de's angebote/stammdaten, as the search-mask URL
// spells them out in its r= param: age bracket, then discount and its class
const AGE_CODES = { adult: 13, senior: 12, young: 9, child: 11, toddler: 8 };
const DISCOUNT_CODES = {
  "none": [16, "KLASSENLOS"],
  "bc25-2": [17, "KLASSE_2"], "bc25-1": [17, "KLASSE_1"],
  "bc50-2": [23, "KLASSE_2"], "bc50-1": [23, "KLASSE_1"],
  "bc100-2": [24, "KLASSE_2"], "bc100-1": [24, "KLASSE_1"],
};

// `outbound` is set only on the return step of a round trip: the mask is then
// built from the outbound journey and `journey` supplies the return date
function bahnDeUrl(journey, outbound) {
  const trip = outbound || journey;
  const legs = trip.legs || [];
  const first = legs[0], last = legs[legs.length - 1];
  const fromName = first.origin?.name || "", fromEva = first.origin?.id || "";
  const toName = last.destination?.name || "", toEva = last.destination?.id || "";
  const hd = (first.plannedDeparture || "").slice(0, 19);
  const soid = encodeURIComponent(`A=1@O=${fromName}@L=${fromEva}@`);
  const zoid = encodeURIComponent(`A=1@O=${toName}@L=${toEva}@`);
  // dlt/dltv mirror the bahn.de search-mask toggles ("nur Deutschland-Ticket-
  // Verbindungen" / "Deutschland-Ticket vorhanden") so the filter carries over
  const dt = state.dticketUsed === "only" ? "&dlt=true&dltv=true"
    : state.dticketUsed === "all" ? "&dltv=true" : "";
  // r pins who the mask prices for — one <type>:<discount>:<class>:<count> per
  // traveller, comma-separated, with the ids of bahn.de's angebote/stammdaten.
  // bahn.de already defaults to a single adult without a discount, so r only has
  // to be sent for any other party.
  const travs = state.travellersUsed;
  const plain = travs.length === 1 && travs[0].age === "adult"
    && travs[0].count === 1 && travs[0].discount === "none";
  const r = plain ? "" : `&r=${travs.map((tr) =>
    `${AGE_CODES[tr.age]}:${DISCOUNT_CODES[tr.discount].join(":")}:${tr.count}`).join(",")}`;
  // rd switches the mask to "Hin- und Rückfahrt"; hza/rza pin both dates to a
  // departure time rather than an arrival time
  const returnDep = outbound ? ((journey.legs || [])[0]?.plannedDeparture || "").slice(0, 19) : "";
  const rt = returnDep ? `&hza=D&rd=${returnDep}&rza=D` : "";
  // vm carries the Verkehrsmittel toggles over as bahn.de's two-digit codes
  // (indices into PRODUCTS); the mask has one list, so a round trip gets the
  // union of both legs' filters. Omitted when nothing is filtered.
  const used = new Set(outbound
    ? [...state.products.outbound, ...state.products.return]
    : state.products[state.leg === "return" ? "return" : "outbound"]);
  const vm = state.mode === "past" || used.size === PRODUCTS.length ? ""
    : `&vm=${PRODUCTS.flatMap((p, i) => used.has(p) ? [String(i).padStart(2, "0")] : []).join(",")}`;
  return `https://www.bahn.de/buchung/fahrplan/suche#sts=true&so=${encodeURIComponent(fromName)}` +
    `&zo=${encodeURIComponent(toName)}&soid=${soid}&zoid=${zoid}&hd=${hd}${rt}&kl=2${dt}${r}${vm}`;
}

// --- claim modal: walks through the steps on bahn.de instead of a bare redirect ---

const claimModal = document.getElementById("claim-modal");

function fmtBahnDate(iso) {
  // mimic the date format of the bahn.de trip list, e.g. "Di., 7. Jul. 2026"
  return new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString(
    state.lang === "de" ? "de-DE" : "en-GB",
    { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

// non-interactive replica of a bahn.de button, so users know what to look for
function bahnBtn(label, outline) {
  const el = document.createElement("span");
  el.className = `bahn-btn${outline ? " bahn-btn-outline" : ""}`;
  el.textContent = label;
  return el;
}

function populateClaimModal() {
  const j = state.claimJourney;
  if (!j) return;
  const legs = j.legs || [];
  const first = legs[0], last = legs[legs.length - 1];
  const pct = j.compensationPct;

  document.getElementById("claim-modal-title").textContent =
    pct != null && pct >= 25 ? t("claimModalTitlePct", pct) : t("claimModalTitle");
  document.getElementById("claim-modal-lead").textContent = t("claimModalLead");
  document.getElementById("claim-modal-close").setAttribute("aria-label", t("claimModalClose"));

  // replica of the journey's row in the bahn.de past-trips list
  const journeyEl = document.createElement("div");
  journeyEl.className = "bahn-journey";
  const jHead = document.createElement("div");
  jHead.className = "bahn-journey-head";
  jHead.append(
    Object.assign(document.createElement("span"), {
      className: "bahn-journey-date",
      textContent: fmtBahnDate(first.plannedDeparture || first.departure || ""),
    }),
    Object.assign(document.createElement("strong"), { textContent: last.destination?.name || "" }),
  );
  const jSub = document.createElement("div");
  jSub.className = "bahn-journey-sub";
  jSub.textContent = `${fmtTime(first.plannedDeparture || first.departure)} – ` +
    `${fmtTime(last.plannedArrival || last.arrival)} · ${first.origin?.name || ""} → ${last.destination?.name || ""}`;
  journeyEl.append(jHead, jSub);

  const steps = [
    [t("claimModalStepFind"), journeyEl],
    [t("claimModalStepDetails"), bahnBtn(t("bahnBtnDetails"), false)],
    [t("claimModalStepRequest"), bahnBtn(t("bahnBtnRequest"), true)],
    [t("claimModalStepSubmit"), bahnBtn(t("bahnBtnSubmit"), false)],
  ];
  const list = document.getElementById("claim-modal-steps");
  list.innerHTML = "";
  for (const [text, body] of steps) {
    const li = document.createElement("li");
    li.append(
      Object.assign(document.createElement("p"), { className: "claim-step-text", textContent: text }),
      body);
    list.appendChild(li);
  }

  document.getElementById("claim-modal-go").textContent = t("claimModalGo");
  document.getElementById("claim-modal-alt-pre").textContent = t("claimAltPre");
  document.getElementById("claim-modal-alt").textContent = t("claimAltLink");
}

function openClaimModal(journey) {
  state.claimJourney = journey;
  populateClaimModal();
  claimModal.showModal();
  track("claim-modal", {
    from: state.from?.name,
    to: state.to?.name,
    pct: journey.compensationPct ?? "na",
  });
}

document.getElementById("claim-modal-go").href = CLAIM_URL;
document.getElementById("claim-modal-alt").href = CLAIM_FORM_URL;
document.getElementById("claim-modal-close").addEventListener("click", () => claimModal.close());
// a click on the backdrop lands on the dialog element itself (the inner wrapper covers the rest)
claimModal.addEventListener("click", (e) => { if (e.target === claimModal) claimModal.close(); });
claimModal.addEventListener("close", () => { state.claimJourney = null; });
// the modal stays open so the steps remain visible next to the bahn.de tab
document.getElementById("claim-modal-go").addEventListener("click", () => {
  const j = state.claimJourney;
  track("claim-db", {
    from: state.from?.name,
    to: state.to?.name,
    pct: j?.compensationPct ?? "na",
    canceled: j?.arrivalCanceled,
    missed: (j?.missedTransfers || []).length > 0,
  });
});

// --- journey report: the bell orders a forecast-vs-actual email after the trip ---
// The order belongs to the Delay Stories account, whose Firebase session is
// shared by every page of the site: a bell pressed by a signed-in visitor is
// the whole order, a signed-out one is sent to /login and the order completes
// on return. The SDK is only downloaded when a bell is pressed or the login
// page left its "account" hint behind, so anonymous visitors never pay for it.

const reportModal = document.getElementById("report-modal");
const reportLoginEl = document.getElementById("report-login");
const reportDoneEl = document.getElementById("report-done");
const reportDoneMsgEl = document.getElementById("report-done-msg");
const reportBookEl = document.getElementById("report-book");
const reportStatusEl = document.getElementById("report-status");
const reportCancelBtn = document.getElementById("report-cancel");
// sessionStorage: the order that waits for the login page to hand the visitor back
const REPORT_PENDING_KEY = "reportPending";
const REPORT_PENDING_MAX_AGE = 60 * 60 * 1000;

let fb = null;               // firebase.js module, imported on demand
let reportSubs = new Map();  // journey key -> order id, for the signed-in account
let reportEmail = "";        // where the account's reports go
let reportView = "busy";     // "busy" | "login" | "done" | "cancelled" | "error"
let reportError = "reportErrGeneric";

// a leg is resolvable after the trip iff normalize_leg attached a delayStats key
// (even a null one); walks and untracked products never carry it
function lastTrackedArrival(journey) {
  const tracked = (journey.legs || []).filter((l) => "delayStats" in l);
  return tracked.length ? tracked[tracked.length - 1].plannedArrival : null;
}

function reportEligible(journey) {
  const arrival = lastTrackedArrival(journey);
  return !!arrival && new Date(arrival) > new Date();
}

// the historic median for the teaser: the destination's, or - when the last
// legs have no history of their own - the last stop on the way that does have
// one, named so the number is not read as the destination's
function teaserStats(journey) {
  const tracked = (journey.legs || []).filter((l) => "delayStats" in l);
  for (let i = tracked.length - 1; i >= 0; i--) {
    const median = tracked[i].delayStats?.medianDelay;
    if (median != null) {
      return { median, upTo: i < tracked.length - 1 ? tracked[i].destination?.name || "" : "" };
    }
  }
  return null;
}

// the same string reports.journey_key builds server-side, from the same fields
function reportKey(journey) {
  return (journey.legs || [])
    .filter((l) => "delayStats" in l)
    .map((l) => `${l.line?.fahrtNr ?? ""}|${l.destination?.id ?? ""}|${l.plannedArrival ?? ""}`)
    .sort()
    .join("\n");
}

const BELL_SVG =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor"'
  + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
  + '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/>'
  + '<path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>';

function paintBell(bell, on) {
  bell.classList.toggle("on", on);
  bell.setAttribute("aria-pressed", String(on));
  const label = t(on ? "reportBellOnTitle" : "reportBellTitle");
  bell.title = label;
  bell.setAttribute("aria-label", label);
}

function refreshBells() {
  document.querySelectorAll(".bell-btn[data-report-key]").forEach((bell) => {
    paintBell(bell, reportSubs.has(bell.dataset.reportKey));
  });
}

/* The account this browser holds, or null. `verified` is whether the sign-in
   proved the address - the server insists on that before it mails anything. */
async function currentAccount() {
  if (!fb) fb = await import("/firebase.js?v=1");
  if (!fb.auth) return null;
  await fb.auth.authStateReady();
  const user = fb.auth.currentUser;
  if (!user) return null;
  const who = await fb.identity(user);
  return { user, email: user.email || "", verified: who.verified && !!user.email };
}

async function reportApi(user, path, opts = {}) {
  const token = await user.getIdToken();
  const resp = await fetch(path, {
    ...opts,
    headers: { ...(opts.headers || {}), "Authorization": "Bearer " + token },
  });
  if (!resp.ok) {
    const err = new Error("http " + resp.status);
    err.status = resp.status;
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}

function populateReportModal() {
  const j = state.reportJourney;
  if (!j) return;
  const teaser = document.getElementById("report-teaser");
  teaser.innerHTML = "";

  const seen = document.createElement("div");
  seen.className = "report-teaser-row";
  const seenLabel = document.createElement("span");
  seenLabel.textContent = t("reportTeaserSeen");
  const seenStats = teaserStats(j);
  let seenChip;
  if (seenStats) {
    seenChip = delayValueBadge(seenStats.median);
  } else {
    seenChip = document.createElement("span");
    seenChip.className = "badge gray";
    seenChip.textContent = t("noData");
  }
  seen.append(seenLabel, seenChip);
  if (seenStats?.upTo) {
    const upTo = document.createElement("span");
    upTo.className = "report-teaser-note";
    upTo.textContent = t("reportTeaserUpTo", seenStats.upTo);
    seen.append(upTo);
  }

  const actual = document.createElement("div");
  actual.className = "report-teaser-row";
  const actualLabel = document.createElement("span");
  actualLabel.textContent = t("reportTeaserActual");
  const actualChip = document.createElement("span");
  actualChip.className = "badge gray report-blur";
  actualChip.textContent = "+? min";
  const note = document.createElement("span");
  note.className = "report-teaser-note";
  note.textContent = t("reportTeaserAfter");
  actual.append(actualLabel, actualChip, note);

  teaser.append(seen, actual);

  reportLoginEl.classList.toggle("hidden", reportView !== "login");
  reportDoneEl.classList.toggle("hidden", reportView !== "done");
  if (reportView === "done") {
    reportDoneMsgEl.textContent = t("reportDoneMsg", reportEmail);
    // the bell only rides on one-way, future journeys, so the mask never needs
    // an outbound leg here - the same link the card's book button carries
    reportBookEl.href = bahnDeUrl(j, null);
  }
  reportStatusEl.classList.toggle("ok", reportView === "cancelled");
  reportStatusEl.textContent = reportView === "busy" ? t("reportBusy")
    : reportView === "cancelled" ? t("reportCancelledMsg")
    : reportView === "error" ? t(reportError)
    : "";
}

function setReportView(view, errorKey) {
  reportView = view;
  if (errorKey) reportError = errorKey;
  populateReportModal();
}

function openReportModal(journey) {
  state.reportJourney = journey;
  setReportView("busy");
  if (!reportModal.open) reportModal.showModal();
}

document.getElementById("report-modal-close").addEventListener("click", () => reportModal.close());
// a click on the backdrop lands on the dialog element itself (the inner wrapper covers the rest)
reportModal.addEventListener("click", (e) => { if (e.target === reportModal) reportModal.close(); });
// the close event is queued, so a bell pressed right after closing may already
// have reopened the modal for its journey - which must not be forgotten then
reportModal.addEventListener("close", () => { if (!reportModal.open) state.reportJourney = null; });

const reportSearchMeta = () =>
  ({ fromName: state.from?.name, toName: state.to?.name, window: state.windowUsed });

async function orderReport(account, journey, search) {
  setReportView("busy");
  try {
    const res = await reportApi(account.user, "/api/reports/subscribe", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ lang: state.lang, journey, search }),
    });
    reportSubs.set(res.key, res.id);
    reportEmail = res.email;
    refreshBells();
    track("report-subscribe", { created: res.created });
    setReportView("done");
  } catch (e) {
    // no account after all, or one short of a proven address: the login page knows which
    if (e.status === 401 || e.status === 403) { setReportView("login"); return; }
    setReportView("error", e.status === 422 ? "reportErrInvalid"
      : e.status === 429 ? "reportErrThrottle" : "reportErrGeneric");
  }
}

async function onBellClick(journey) {
  openReportModal(journey);
  track("report-modal", { from: state.from?.name, to: state.to?.name });
  if (reportSubs.has(reportKey(journey))) { setReportView("done"); return; }
  let account = null;
  try { account = await currentAccount(); } catch (e) { /* SDK unreachable: same as signed out */ }
  if (!account || !account.verified) { setReportView("login"); return; }
  await orderReport(account, journey, reportSearchMeta());
}

document.getElementById("report-login-btn").addEventListener("click", () => {
  try {
    sessionStorage.setItem(REPORT_PENDING_KEY, JSON.stringify({
      journey: state.reportJourney, search: reportSearchMeta(), ts: Date.now(),
    }));
  } catch (e) { /* no storage: the bell has to be pressed again after the login */ }
  track("report-login");
  const next = location.pathname + location.search;
  location.assign("/login?next=" + encodeURIComponent(next) + "&reason=report");
});

reportBookEl.addEventListener("click", () => {
  const j = state.reportJourney;
  track("book-bahn", {
    from: state.from?.name,
    to: state.to?.name,
    price: j?.dticketCovered ? 0 : j?.price ?? "na",
    trip: "oneway",
    via: "report-modal",
  });
});

reportCancelBtn.addEventListener("click", async () => {
  const journey = state.reportJourney;
  const id = journey ? reportSubs.get(reportKey(journey)) : undefined;
  if (id === undefined) return;
  reportCancelBtn.disabled = true;
  try {
    const account = await currentAccount();
    if (!account) throw new Error("signed out");
    await reportApi(account.user, "/api/reports/" + id, { method: "DELETE" });
    reportSubs.delete(reportKey(journey));
    refreshBells();
    track("report-cancel");
    setReportView("cancelled");
  } catch (e) {
    setReportView("error", "reportErrGeneric");
  } finally {
    reportCancelBtn.disabled = false;
  }
});

/* On load: complete an order that waited for the login, and light the bells of
   journeys this account already ordered a report for. Both need the account,
   which is only looked up when there is a reason to - a parked order, or the
   hint the login page leaves behind. */
async function initReports() {
  let pending = null;
  try {
    pending = JSON.parse(sessionStorage.getItem(REPORT_PENDING_KEY));
    sessionStorage.removeItem(REPORT_PENDING_KEY);
  } catch (e) { /* no storage */ }
  if (pending && !(pending.journey && Date.now() - pending.ts < REPORT_PENDING_MAX_AGE)) pending = null;
  let hinted = false;
  try { hinted = localStorage.getItem("account") === "1"; } catch (e) {}
  if (!pending && !hinted) return;
  let account = null;
  try { account = await currentAccount(); } catch (e) { return; }
  if (!account || !account.verified) return;  // the login was abandoned: nothing is ordered
  if (pending) {
    openReportModal(pending.journey);
    await orderReport(account, pending.journey, pending.search || reportSearchMeta());
  }
  try {
    const res = await reportApi(account.user, "/api/reports/mine");
    reportSubs = new Map(res.subscriptions.map((sub) => [sub.key, sub.id]));
    reportEmail = res.email;
    refreshBells();
  } catch (e) { /* the bells simply start unlit */ }
}

// price slot: the D-Ticket label when the connection is covered by the ticket,
// an offer price, or a pointer to bahn.de when the search turned up no price.
// `journey` is the row the price belongs to; the trip total passes the combined
// verdict for its two legs instead.
function priceNode(value, journey) {
  const price = document.createElement("span");
  price.className = "price";
  if (journey?.dticketCovered) {
    price.classList.add("price-dticket");
    price.textContent = t("dticketIncluded");
    price.title = t("dticketIncludedTooltip");
  } else if (value != null) {
    price.textContent = t("priceFrom", value);
    // the D-Ticket already paid for part of this journey: say what the rest costs
    if (journey?.pricePartial) price.title = t("dticketPartialTooltip");
  } else if (state.dticketUsed === "only") {
    // D-Ticket-filtered results carry no offer price: the ticket is the fare
    price.classList.add("price-dticket");
    price.textContent = t("dticketIncluded");
    price.title = t("dticketIncludedTooltip");
  } else if (state.travellersUsed.every((tr) => tr.age === "toddler")) {
    // 0-5-year-olds never need a ticket, so no offer price comes back for them
    price.classList.add("price-dticket");
    price.textContent = t("priceFree");
    price.title = t("priceFreeTooltip");
  } else {
    price.classList.add("price-na");
    price.textContent = t("priceNa");
  }
  return price;
}

// future-mode journey badges; `stats` comes back set only when the caller still
// has to wire the day chart onto the returned badge
function journeyBadges(journey, finalLeg) {
  const unlikelyTts = (journey.tightTransfers || []).filter((tt) => tt.unlikely);
  if (unlikelyTts.length) {
    // final-leg stats are meaningless if an earlier connection is likely missed
    const badge = document.createElement("span");
    badge.className = "badge red";
    badge.textContent = t("unlikelyBadge");
    badge.title = t("unlikelyBadgeTooltip", unlikelyTts.map((tt) => tt.station).join(", "));
    return { badge, tightBadge: null, stats: null };
  }
  if (UNTRACKED_PRODUCTS.has(finalLeg?.line?.product)) {
    return { badge: notTrackedBadge(), tightBadge: null, stats: null };
  }
  const stats = finalLeg ? finalLeg.delayStats : null;
  const badge = delayBadge(stats, true);
  let tightBadge = null;
  const tts = journey.tightTransfers || [];
  if (tts.length) {
    tightBadge = document.createElement("span");
    tightBadge.className = "badge yellow";
    tightBadge.textContent = t("tightBadge");
    tightBadge.title = t("tightBadgeTooltip", tts.map((tt) => tt.station).join(", "));
  }
  return { badge, tightBadge, stats };
}

function render() {
  // step 3 replaces the result list with the trip as a whole
  if (state.leg === "summary") return renderSummary();
  resultsEl.innerHTML = "";
  for (const journey of sortedJourneys()) {
    const legs = journey.legs || [];
    if (!legs.length) continue;
    const first = legs[0], last = legs[legs.length - 1];
    const trainLegs = legs.filter((l) => !l.walking);
    const transfers = journey.transfers ?? Math.max(0, trainLegs.length - 1);

    const card = document.createElement("div");
    card.className = "journey";

    const head = document.createElement("div");
    head.className = "journey-head";

    const sim = state.mode === "past" ? journey.simulation : null;
    const times = document.createElement("span");
    times.className = "journey-times";
    if (sim?.actualArrival) {
      // planned arrival is struck out, the simulated actual arrival follows
      times.append(
        document.createTextNode(`${fmtTime(first.plannedDeparture)} → `),
        Object.assign(document.createElement("s"), { textContent: fmtTime(last.plannedArrival) }),
        document.createTextNode(` ${fmtTime(sim.actualArrival)}`),
      );
    } else if (state.mode === "past") {
      times.textContent = `${fmtTime(first.plannedDeparture)} → ${fmtTime(last.plannedArrival)}`;
    } else {
      times.append(
        timeNode(first.plannedDeparture, first.departure),
        document.createTextNode(" → "),
        timeNode(last.plannedArrival, last.arrival),
      );
    }

    const meta = document.createElement("span");
    meta.className = "journey-meta";
    // ezDurationSeconds: journey duration as bahn.de re-planned it with live delays
    const duration = state.mode === "past"
      ? journey.durationSeconds
      : journey.ezDurationSeconds ?? journey.durationSeconds;
    meta.textContent = `${fmtDuration(duration)} · ` +
      (transfers === 0 ? t("direct") : t("transfers", transfers));

    const spacer = document.createElement("span");
    spacer.className = "spacer";

    const past = state.mode === "past";
    const finalLeg = trainLegs.length ? trainLegs[trainLegs.length - 1] : null;
    const missed = past && (journey.missedTransfers || []).length > 0;
    let badge;
    let tightBadge = null;
    if (past) {
      if (sim && journey.arrivalDelay != null) {
        // simulated delay at the destination, replacement connections included
        badge = delayValueBadge(journey.arrivalDelay, t("simBadgeTooltip"));
      } else if (missed) {
        // connection missed and no replacement found: arrival unknown
        badge = document.createElement("span");
        badge.className = "badge red";
        badge.textContent = t("missedBadge");
        badge.title = (journey.missedTransfers || []).map((mt) => mt.station).join(", ");
      } else if (UNTRACKED_PRODUCTS.has(finalLeg?.line?.product)) {
        badge = notTrackedBadge();
      } else {
        badge = exactDelayBadge(finalLeg?.delayOnDate);
      }
    } else {
      const badges = journeyBadges(journey, finalLeg);
      badge = badges.badge;
      tightBadge = badges.tightBadge;
      if (badges.stats) wireDayChart(badge, badges.stats, head, finalLeg.line?.name);
    }

    let claimable = false;
    if (past) {
      const pct = journey.compensationPct;
      // with a completed simulation pct reflects the realistic arrival; the
      // cancelled/missed wordings only apply when the outcome stayed unknown
      const canceledish = journey.arrivalCanceled
        || (journey.missedTransfers || []).some((mt) => mt.canceled);
      claimable = (pct != null && pct >= 25) || (pct == null && (canceledish || missed));
      let action;
      if (claimable) {
        action = document.createElement("button");
        action.type = "button";
        action.className = "claim-btn";
        action.textContent = pct != null && pct >= 25 ? t("claimPct", pct)
          : canceledish ? t("claimCanceled")
          : t("claimMissed");
        action.addEventListener("click", () => openClaimModal(journey));
      } else {
        action = document.createElement("span");
        action.className = "claim-none";
        // pending: the day is live and some leg hasn't been reported yet, so the
        // arrival - and with it the claim - can't be settled until the morning
        action.textContent = pct === 0 ? t("claimNone")
          : journey.pending ? t("claimPending")
          : t("noData");
      }
      head.append(times, meta, spacer, badge, action);
    } else {
      const price = priceNode(journey.price, journey);

      // on a round trip both list steps only pick a journey; booking waits for
      // the summary, where the two dates go into one bahn.de link
      let action;
      if (state.returnTrip) {
        action = document.createElement("button");
        action.type = "button";
        action.className = "continue-btn";
        action.textContent = t("continueBtn");
        action.addEventListener("click", () =>
          state.leg === "outbound" ? selectOutbound(journey) : selectReturn(journey));
      } else {
        action = document.createElement("a");
        action.className = "book-btn";
        action.textContent = t("book");
        action.href = bahnDeUrl(journey, null);
        action.target = "_blank";
        action.rel = "noopener";
        action.addEventListener("click", () =>
          track("book-bahn", {
            from: state.from?.name,
            to: state.to?.name,
            // covered by the D-Ticket costs nothing extra: 0, not "no price known"
            price: journey.dticketCovered ? 0 : journey.price ?? "na",
            trip: "oneway",
          })
        );
      }

      // the bell orders a post-journey forecast-vs-actual report email; only
      // where a tracked leg can still be resolved after the trip
      let bookSlot = action;
      if (!state.returnTrip && reportEligible(journey)) {
        const bell = document.createElement("button");
        bell.type = "button";
        bell.className = "bell-btn";
        bell.dataset.reportKey = reportKey(journey);
        bell.innerHTML = BELL_SVG;
        paintBell(bell, reportSubs.has(bell.dataset.reportKey));
        bell.addEventListener("click", () => onBellClick(journey));
        bookSlot = document.createElement("div");
        bookSlot.className = "book-row";
        bookSlot.append(action, bell);
      }

      // badges, price and booking button wrap together as one right-aligned block
      const cta = document.createElement("div");
      cta.className = "journey-cta";
      // next to a tight-transfer warning the delay badge is only worth the space when red
      const showDelayBadge = !tightBadge || badge.classList.contains("red");
      cta.append(...(tightBadge ? [tightBadge] : []), ...(showDelayBadge ? [badge] : []), price, bookSlot);
      head.append(times, meta, spacer, cta);
    }
    card.appendChild(head);

    const legsEl = document.createElement("div");
    legsEl.className = "legs";
    // future mode only: in past mode the struck-out legs already carry
    // missed/cancelled badges, so no extra warning strip
    const warnByLeg = past
      ? new Map()
      : new Map((journey.tightTransfers || []).map((tt) => [tt.legIndex, tt]));
    let canceledTotal = 0;
    const missedAt = sim ? sim.missedAtLegIndex : null;
    // while an if-missed panel is open, the original legs it replaces are crossed
    // out; with several panels open the earliest miss wins (everything after it
    // wouldn't happen)
    const legRows = [];
    const tightStrips = [];
    const openMisses = new Set();
    const applyMissed = () => {
      const cut = openMisses.size ? Math.min(...openMisses) : Infinity;
      legRows.forEach((row, i) => row.classList.toggle("leg-missed", i >= cut));
      tightStrips.forEach(({ strip, legIndex }) =>
        strip.classList.toggle("leg-tight-dimmed", legIndex >= cut));
    };
    legs.forEach((leg, i) => {
      const struck = missedAt != null && i >= missedAt;
      const row = buildLegRow(leg, past, struck);
      if (i === 0) row.classList.add("rail-first");
      if (i === legs.length - 1) row.classList.add("rail-last");
      if (struck) row.classList.add("leg-missed");
      if (!past && !leg.walking) {
        const legBadge = row.querySelector(".badge");
        if (leg.delayStats) wireDayChart(legBadge, leg.delayStats, row, leg.line?.name);
        if (leg.delayStats?.canceledDays) canceledTotal += leg.delayStats.canceledDays;
      }
      legRows.push(row);
      legsEl.appendChild(row);
      const tt = warnByLeg.get(i);
      if (tt) {
        const warn = document.createElement("div");
        warn.className = "leg-tight";
        tightStrips.push({ strip: warn, legIndex: i });
        const lead = document.createElement("strong");
        lead.textContent = tt.unlikely ? `⛔ ${t("unlikelyTitle")}` : `⚠ ${t("tightTitle")}`;
        warn.append(lead, document.createTextNode(" " + t("tightDetail", tt.transferMinutes, tt.medianDelay)));
        if (tt.ifMissed?.legs?.length) {
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "if-missed-btn";
          btn.setAttribute("aria-expanded", "false");
          const caret = document.createElement("span");
          caret.className = "badge-caret";
          caret.setAttribute("aria-hidden", "true");
          caret.textContent = "▾";
          btn.append(document.createTextNode(t("ifMissedBtn", fmtTime(tt.ifMissed.arrival))), caret);
          let panel = null;
          btn.addEventListener("click", () => {
            if (panel) {
              panel.remove();
              panel = null;
              btn.setAttribute("aria-expanded", "false");
              openMisses.delete(tt.depLegIndex);
              applyMissed();
              return;
            }
            panel = buildIfMissedPanel(tt);
            warn.insertAdjacentElement("afterend", panel);
            btn.setAttribute("aria-expanded", "true");
            openMisses.add(tt.depLegIndex);
            applyMissed();
            track("if-missed", { station: tt.station });
          });
          warn.appendChild(btn);
        }
        legsEl.appendChild(warn);
      }
    });
    if (sim) {
      if (sim.legs?.length) {
        const contHead = document.createElement("div");
        contHead.className = "leg-continuation";
        contHead.textContent = t("simContinuation");
        legsEl.appendChild(contHead);
        sim.legs.forEach((leg, i) => {
          const row = buildLegRow(leg, true, false);
          if (i === 0) row.classList.add("rail-first");
          if (i === sim.legs.length - 1) row.classList.add("rail-last");
          legsEl.appendChild(row);
        });
      }
      if (sim.incomplete) {
        const note = document.createElement("div");
        note.className = "sim-note";
        note.textContent = t("simIncomplete");
        legsEl.appendChild(note);
      }
    }
    card.appendChild(legsEl);

    if (canceledTotal > 0) {
      const note = document.createElement("div");
      note.className = "cancel-note";
      note.textContent = t("cancelNote", state.windowUsed, canceledTotal);
      card.appendChild(note);
    }

    resultsEl.appendChild(card);
  }
}

// --- step 3: the picked outbound and return on one screen, then one booking link ---

function summaryLegBox(journey, n, labelKey, onChange) {
  const legs = journey.legs || [];
  const first = legs[0], last = legs[legs.length - 1];
  const trainLegs = legs.filter((l) => !l.walking);
  const transfers = journey.transfers ?? Math.max(0, trainLegs.length - 1);

  const change = document.createElement("button");
  change.type = "button";
  change.className = "trip-change";
  change.textContent = t("changeOutbound");
  change.addEventListener("click", () => goToStep(onChange));
  const head = document.createElement("div");
  head.className = "summary-leg-head";
  head.append(
    // the arrow and the step number carry the direction at a glance, so the two
    // blocks don't rely on reading their labels to be told apart
    Object.assign(document.createElement("strong"), {
      className: "summary-leg-chip",
      textContent: `${n === 1 ? "→" : "←"} ${n}. ${t(labelKey)}`,
    }),
    Object.assign(document.createElement("span"), {
      className: "summary-leg-day",
      textContent: fmtTripDay(first.plannedDeparture),
    }),
    change,
  );

  const route = Object.assign(document.createElement("div"), {
    className: "summary-leg-route",
    textContent: `${first.origin?.name || ""} → ${last.destination?.name || ""}`,
  });

  const meta = document.createElement("div");
  meta.className = "summary-leg-meta";
  const times = document.createElement("span");
  times.className = "journey-times";
  times.append(
    timeNode(first.plannedDeparture, first.departure),
    document.createTextNode(" → "),
    timeNode(last.plannedArrival, last.arrival),
  );
  const dur = document.createElement("span");
  dur.className = "journey-meta";
  dur.textContent = `${fmtDuration(journey.ezDurationSeconds ?? journey.durationSeconds)} · ` +
    (transfers === 0 ? t("direct") : t("transfers", transfers));
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  const finalLeg = trainLegs.length ? trainLegs[trainLegs.length - 1] : null;
  const badges = journeyBadges(journey, finalLeg);
  if (badges.stats) wireDayChart(badges.badge, badges.stats, meta, finalLeg.line?.name);
  meta.append(times, dur, spacer,
    ...(badges.tightBadge ? [badges.tightBadge] : []), badges.badge,
    priceNode(journey.price, journey));

  const legsEl = document.createElement("div");
  legsEl.className = "legs";
  legs.forEach((leg, i) => {
    const row = buildLegRow(leg, false, false);
    if (i === 0) row.classList.add("rail-first");
    if (i === legs.length - 1) row.classList.add("rail-last");
    // the day chart stays reachable here: the pick is made, but not yet booked
    if (!leg.walking && leg.delayStats) {
      wireDayChart(row.querySelector(".badge"), leg.delayStats, row, leg.line?.name);
    }
    legsEl.appendChild(row);
  });

  const box = document.createElement("div");
  box.className = "summary-leg";
  box.append(head, route, meta, legsEl);
  return box;
}

function renderSummary() {
  resultsEl.innerHTML = "";
  const out = state.outbound, ret = state.returnJourney;
  if (!out || !ret) return;

  const panel = document.createElement("div");
  panel.className = "trip-summary";
  panel.append(
    Object.assign(document.createElement("h2"), {
      className: "trip-summary-title", textContent: t("summaryTitle"),
    }),
    summaryLegBox(out, 1, "stepOutbound", backToOutbound),
    summaryLegBox(ret, 2, "stepReturn", backToReturn),
  );

  const total = document.createElement("div");
  total.className = "trip-total";
  total.append(
    Object.assign(document.createElement("span"), {
      className: "trip-total-label", textContent: t("summaryTotal"),
    }),
    priceNode(tripTotal(), tripPriceKind()),
  );

  // the mask carries both dates and both departure minutes; bahn.de has no link
  // format that pins the two trains themselves
  const book = document.createElement("a");
  book.className = "book-btn book-btn-lg";
  book.textContent = t("bookBoth");
  book.href = bahnDeUrl(ret, out);
  book.target = "_blank";
  book.rel = "noopener";
  book.addEventListener("click", () =>
    track("book-bahn", {
      from: state.from?.name,
      to: state.to?.name,
      price: tripTotal() ?? "na",
      trip: "roundtrip",
    })
  );

  panel.append(total, book);
  resultsEl.appendChild(panel);
}

// --- install prompt (PWA awareness) ---
// The manifest + service worker make the site installable, but browsers surface
// that only faintly (Android: a buried menu entry) or not at all (iOS).
// A single dismissable banner with an Install button: on Chromium (Android and
// desktop) it triggers the native install; on iOS — where no browser has an
// install API — the button opens a bottom sheet with the manual Share -> Add to
// Home Screen steps.

(function initInstallPrompt() {
  const promptEl = document.getElementById("install-prompt");
  if (!promptEl) return;
  const acceptBtn = document.getElementById("install-accept");
  const dismissBtn = document.getElementById("install-dismiss");
  const iosSheet = document.getElementById("ios-install-sheet");

  const DISMISS_KEY = "installPromptDismissed";
  const DISMISS_DAYS = 60;

  const standalone = window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true;
  const dismissedTs = Number(localStorage.getItem(DISMISS_KEY) || 0);
  const recentlyDismissed = dismissedTs > 0 && Date.now() - dismissedTs < DISMISS_DAYS * 864e5;

  // Already installed (running standalone) or recently dismissed: never offer.
  if (standalone || recentlyDismissed) return;

  const show = () => promptEl.classList.remove("hidden");
  const hide = () => promptEl.classList.add("hidden");
  const remember = () => localStorage.setItem(DISMISS_KEY, String(Date.now()));

  // Dismiss and install-completion apply on every platform.
  dismissBtn.addEventListener("click", () => {
    remember();
    hide();
    track("install", { step: "dismiss" });
  });
  window.addEventListener("appinstalled", () => {
    remember();
    hide();
    track("install", { step: "installed" });
  });

  const ua = navigator.userAgent || "";
  const isIOS = /iphone|ipad|ipod/i.test(ua)
    || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  // Chrome/Firefox/Edge on iOS are WebKit skins: no install API, but since
  // iOS 16.4 their share menu can add a real standalone web app too, so they get
  // the same sheet with the wording pointed at the right browser.
  const iosBrowser = !isIOS ? null
    : /crios/i.test(ua) ? "chrome"
    : /fxios/i.test(ua) ? "firefox"
    : /edgios/i.test(ua) ? "edge"
    : "safari";

  // iOS: same banner + Install button, but there is no install API, so the
  // button opens a bottom sheet with the manual Share -> Add to Home Screen steps.
  // Stop here: iOS never fires beforeinstallprompt, so the Android handler below
  // must not run and rebind the button to a prompt that will never arrive.
  if (isIOS) {
    acceptBtn.classList.remove("hidden");
    show();
    track("install", { step: "shown", platform: "ios", browser: iosBrowser });
    if (iosSheet) {
      if (iosBrowser !== "safari") {
        const sheetLead = iosSheet.querySelector(".ios-sheet-lead");
        const leadKey = iosBrowser === "chrome" ? "iosSheetLeadChrome" : "iosSheetLeadOther";
        sheetLead.dataset.i18n = leadKey; // keeps it in sync on lang switch
        sheetLead.textContent = t(leadKey);
      }
      if (iosBrowser === "chrome") {
        // Chrome's own ⋯ menu offers "Share Chrome", which shares the app rather
        // than the page; the share icon in the address bar is the one that opens
        // the iOS sheet, and Add to Home Screen sits below its fold.
        const stepEls = iosSheet.querySelectorAll(".ios-step-text > span[data-i18n]");
        ["iosStep1Chrome", "iosStep2Chrome"].forEach((key, i) => {
          if (!stepEls[i]) return;
          stepEls[i].dataset.i18n = key;
          stepEls[i].textContent = t(key);
        });
      }
      acceptBtn.addEventListener("click", () => {
        track("install", { step: "ios-sheet", browser: iosBrowser });
        iosSheet.showModal();
      });
      const closeSheet = () => iosSheet.close();
      document.getElementById("ios-sheet-close").addEventListener("click", closeSheet);
      document.getElementById("ios-sheet-done").addEventListener("click", closeSheet);
      iosSheet.addEventListener("click", (e) => { if (e.target === iosSheet) closeSheet(); });
    }
    return;
  }

  // Chromium (Android and desktop): the browser tells us the app is installable.
  // Desktop keeps its own omnibox install icon as well — preventDefault only
  // suppresses the mobile mini-infobar, which we replace with this banner.
  const uaData = navigator.userAgentData;
  const isMobile = uaData ? uaData.mobile === true : /android|mobile/i.test(ua);
  const leadEl = promptEl.querySelector(".install-lead");
  let deferred = null;
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    deferred = e;
    if (!isMobile && leadEl) { // "home screen" only makes sense on a phone
      leadEl.dataset.i18n = "installLeadDesktop"; // keeps it in sync on lang switch
      leadEl.textContent = t("installLeadDesktop");
    }
    acceptBtn.classList.remove("hidden");
    show();
    track("install", { step: "shown", platform: isMobile ? "android" : "desktop" });
  });
  acceptBtn.addEventListener("click", async () => {
    if (!deferred) return;
    track("install", { step: "accept" });
    deferred.prompt();
    const choice = await deferred.userChoice;
    track("install", { step: choice && choice.outcome === "accepted" ? "accepted" : "declined" });
    deferred = null;
    hide();
  });
})();

// --- init ---

applyLang(state.lang);

// restore a search from the URL (refresh, bookmark, shared link)
const qp = new URLSearchParams(location.search);
(async () => {
  if (!PAST_PAGE && qp.get("mode") === "past") {
    // legacy links from before past mode moved to its own sub-page
    qp.delete("mode");
    location.replace(`${pagePath("past")}${qp.size ? `?${qp}` : ""}`);
    return;
  }
  if (PAST_PAGE) await initPastPage();
  if (qp.get("fromId") && qp.get("toId")) {
    state.from = { id: qp.get("fromId"), name: qp.get("from") || "" };
    state.to = { id: qp.get("toId"), name: qp.get("to") || "" };
    document.getElementById("from").value = state.from.name;
    document.getElementById("to").value = state.to.name;
    // date after initPastPage so a restored past date wins over the coverage clamp
    if (qp.get("date")) document.getElementById("date").value = qp.get("date");
    if (qp.get("time")) document.getElementById("time").value = qp.get("time");
    if (["7", "15", "30"].includes(qp.get("window"))) document.getElementById("window").value = qp.get("window");
    // "1" is the legacy value from before the "all trains" mode existed
    document.getElementById("dticket").checked = ["1", "only"].includes(qp.get("dticket"));
    document.getElementById("dticket-all").checked = qp.get("dticket") === "all";
    // "age" is the legacy single-traveller form, from before the party was a list
    const restored = parseTravellers(qp.get("travellers"))
      || (["senior", "young", "child", "toddler"].includes(qp.get("age"))
        ? [{ count: 1, age: qp.get("age"), discount: "none" }] : null);
    if (restored) {
      travellerRows().forEach((row) => row.remove());
      restored.forEach((tr) => addTravellerRow(tr));
      syncTravellerUI();
    }
    if (["10", "15", "20", "25", "30", "35", "40", "45"].includes(qp.get("transfer"))) {
      document.getElementById("transfer").value = qp.get("transfer");
    }
    const parseProducts = (raw) => {
      const names = (raw || "").split(",").filter((p) => PRODUCTS.includes(p));
      return names.length ? new Set(names) : null;
    };
    const tm = parseProducts(qp.get("tm"));
    if (tm) state.products.outbound = tm;
    // no tmr means the legs were filtered alike; "all" is a deliberately open return
    state.products.return = qp.get("tmr") === "all" ? new Set(PRODUCTS)
      : parseProducts(qp.get("tmr")) || new Set(state.products.outbound);
    updateTransportBtn();
    if (travellersParam() || transferMinutes() !== "0" || productsParam("outbound") || productsParam("return")) setAdvancedOpen(true);
    if (!PAST_PAGE && qp.get("rdate")) {
      // the picked outbound isn't in the URL, so a restored round trip
      // starts over at step 1
      setReturnTrip(true);
      returnDateEl.value = qp.get("rdate");
      if (qp.get("rtime")) returnTimeEl.value = qp.get("rtime");
    }
    if (!PAST_PAGE) {
      const restoreVia = (param, key) => {
        if (!qp.get(`${param}Id`)) return;
        state[key] = { id: qp.get(`${param}Id`), name: qp.get(param) || "" };
        document.getElementById(key).value = state[key].name;
        setStay(key, parseStay(qp.get(`${param}Stay`)));
        viaRow(key).classList.remove("hidden");
      };
      restoreVia("vo1", "viaOut1");
      restoreVia("vo2", "viaOut2");
      if (state.returnTrip) {
        restoreVia("vr1", "viaRet1");
        restoreVia("vr2", "viaRet2");
      }
      if (ALL_VIA_KEYS.some((k) => state[k])) setAdvancedOpen(true);
      syncStopoverUI();
    }
    search();
  }
})();

initReports();
