const state = {
  from: null,   // {id, name}
  to: null,
  journeys: [],
  sort: "departure",
  windowUsed: 7,  // stats window that produced the current results
  departure: null,  // departure ISO of the current search (reused for paging)
  earlierRef: null,  // paging tokens from the API
  laterRef: null,
  lang: localStorage.getItem("lang") || "de",
  chart: null,  // which hero chart is expanded: null (collapsed) | "scatter" | "violin"
  status: null,  // {key, params} of the current status message, re-rendered on lang switch
  mode: "future",  // "future" = delay forecast, "past" = compensation check for a past journey
  coverage: null,  // {minDay, maxDay, liveMaxDay} for the date picker, fetched on demand
  liveDay: false,  // searched day is past the local data and answered live from IRIS
  claimJourney: null,  // journey shown in the claim-steps modal
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
    pageTitle: "DB Verbindungssuche mit Verspätungsstatistik",
    headerTitle: "Verbindungssuche",
    headerSubtitle: "mit Verspätungsstatistik",
    tagline: "Den Zug buchen, nicht die Verspätung",
    from: "Von",
    to: "Nach",
    fromPlaceholder: "z.B. Berlin Hbf",
    toPlaceholder: "z.B. München Hbf",
    swapTitle: "Richtung tauschen",
    date: "Datum",
    time: "Uhrzeit",
    window: "Statistik-Zeitraum",
    days7: "7 Tage",
    days15: "15 Tage",
    days30: "30 Tage",
    search: "Suchen",
    sortLabel: "Sortieren:",
    sortDeparture: "Abfahrtszeit",
    sortDelay: "Wenigste Verspätung",
    sortPrice: "Günstigster Preis",
    sortRisk: "Geringstes Anschlussrisiko",
    sortTransfers: "Wenigste Umstiege",
    earlier: "Frühere Verbindungen",
    later: "Spätere Verbindungen",
    heroClaimLate: "Verspätete Züge bleiben verspätet.",
    heroClaimPunctual: "Pünktliche Züge bleiben pünktlich.",
    heroSubScope: "41.138 Züge im Mai und Juni 2026 verglichen:",
    heroSubFinding: "Wer im Mai zu spät kam, kam auch im Juni zu spät.",
    chartSwitchLabel: "Daten ansehen",
    chartAlt: "Verspätete Züge bleiben verspätet: Züge, die im Mai verspätet waren, waren es auch im Juni.",
    violinAlt: "Pünktlich bleibt pünktlich, verspätet bleibt verspätet: Züge, gruppiert nach ihrer Mai-Verspätung, zeigen im Juni dieselbe Rangfolge.",
    chartScatter: "Punktwolke",
    chartViolin: "Verteilung",
    recentLabel: "Letzte Suchen",
    pickStations: "Bitte Start und Ziel aus der Vorschlagsliste wählen.",
    searching: "Suche Verbindungen…",
    noResults: "Keine Verbindungen gefunden.",
    error: (msg) => `Fehler: ${msg}`,
    noData: "keine Daten",
    notTracked: "nicht erfasst",
    notTrackedTooltip: "Für U-Bahn, Tram, Bus und Fähre werden keine Verspätungsdaten erhoben",
    badgeDays: (matched, total) => `(${matched}/${total} Tage)`,
    badgeTooltip: (win, max) => `Mittlere Ankunftsverspätung (Median) der letzten ${win} Tage (max. +${max} min)`,
    badgeClickHint: "Verspätung pro Tag anzeigen",
    chartDayCaption: (win) => `Ankunftsverspätung pro Tag – letzte ${win} Tage`,
    chartCanceled: "ausgefallen",
    direct: "direkt",
    transfers: (n) => `${n} Umstieg${n > 1 ? "e" : ""}`,
    walk: "Fußweg",
    train: "Zug",
    priceFrom: (price) => `ab ${price.toFixed(2).replace(".", ",")} €`,
    priceNa: "Preis auf bahn.de",
    book: "Auf bahn.de buchen",
    cancelNote: (win, n) => `⚠ In den letzten ${win} Tagen ${n}× (teil-)ausgefallen`,
    tightTitle: "Knapper Umstieg:",
    unlikelyTitle: "Unwahrscheinlicher Umstieg:",
    unlikelyBadge: "⛔ Anschlussrisiko",
    unlikelyBadgeTooltip: (stations) => `Die typische Verspätung übersteigt die Umstiegszeit deutlich (${stations})`,
    tightBadge: "⚠ Knapper Umstieg",
    tightBadgeTooltip: (stations) => `Die typische Verspätung lässt wenig Umstiegszeit (${stations})`,
    tightDetail: (transfer, delay) => `${transfer} min Umstiegszeit – dieser Zug kommt typischerweise +${delay} min verspätet an`,
    footerOpenSource: "Open Source – Quellcode auf GitHub",
    footerData: "Verspätungsdaten:",
    footerDonate: "☕ Spendier mir einen Kaffee",
    donateNudgeLead: "Hat dir das geholfen?",
    donateNudgeLink: "☕ Spendier mir einen Kaffee",
    footerLegal: "Impressum & Datenschutz",
    footerDisclaimer: "DelayBahn ist ein unabhängiges Projekt und steht in keiner Verbindung zur Deutsche Bahn AG. „DB“ und „Deutsche Bahn“ sind Marken der Deutsche Bahn AG.",
    navRefund: "Entschädigung beantragen",
    refundCtaTitle: "Über 1 Stunde Verspätung gehabt?",
    refundCtaLead: "Sieh die Reise, die du tatsächlich hattest – mit Verspätungen und verpassten Anschlüssen.",
    refundCtaSub: "Hol dir dein Geld von der DB zurück – in 3 einfachen Klicks",
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
  },
  en: {
    pageTitle: "DB Connection Search with Delay Statistics",
    headerTitle: "Connection Search",
    headerSubtitle: "with delay statistics",
    tagline: "Book the train, not the delay",
    from: "From",
    to: "To",
    fromPlaceholder: "e.g. Berlin Hbf",
    toPlaceholder: "e.g. München Hbf",
    swapTitle: "Swap direction",
    date: "Date",
    time: "Time",
    window: "Tracking period",
    days7: "7 days",
    days15: "15 days",
    days30: "30 days",
    search: "Search",
    sortLabel: "Sort:",
    sortDeparture: "Departure time",
    sortDelay: "Least delay",
    sortPrice: "Cheapest price",
    sortRisk: "Lowest connection risk",
    sortTransfers: "Fewest transfers",
    earlier: "Earlier connections",
    later: "Later connections",
    heroClaimLate: "Late trains stay late.",
    heroClaimPunctual: "Punctual trains stay punctual.",
    heroSubScope: "41,138 trains compared across May and June 2026",
    heroSubFinding: "the ones that ran late in May ran late again in June.",
    chartSwitchLabel: "See the data",
    chartAlt: "Delayed trains stay delayed: trains that ran late in May also ran late in June.",
    violinAlt: "Punctual stays punctual, late stays late: trains grouped by their May delay show the same ranking in June.",
    chartScatter: "Scatter",
    chartViolin: "Distribution",
    recentLabel: "Recent searches",
    pickStations: "Please pick origin and destination from the suggestion list.",
    searching: "Searching for connections…",
    noResults: "No connections found.",
    error: (msg) => `Error: ${msg}`,
    noData: "no data",
    notTracked: "not tracked",
    notTrackedTooltip: "Delay data isn't collected for metro, tram, bus and ferry services",
    badgeDays: (matched, total) => `(${matched}/${total} days)`,
    badgeTooltip: (win, max) => `Median arrival delay over the last ${win} days (max. +${max} min)`,
    badgeClickHint: "Show per-day delays",
    chartDayCaption: (win) => `Arrival delay per day – last ${win} days`,
    chartCanceled: "cancelled",
    direct: "direct",
    transfers: (n) => `${n} transfer${n > 1 ? "s" : ""}`,
    walk: "Walk",
    train: "Train",
    priceFrom: (price) => `from ${price.toFixed(2).replace(".", ",")} €`,
    priceNa: "Price on bahn.de",
    book: "Book on bahn.de",
    cancelNote: (win, n) => `⚠ (Partially) cancelled ${n}× in the last ${win} days`,
    tightTitle: "Tight transfer:",
    unlikelyTitle: "Unlikely transfer:",
    unlikelyBadge: "⛔ Connection risk",
    unlikelyBadgeTooltip: (stations) => `Typical delay far exceeds the transfer time (${stations})`,
    tightBadge: "⚠ Tight transfer",
    tightBadgeTooltip: (stations) => `Typical delay leaves little time to change trains (${stations})`,
    tightDetail: (transfer, delay) => `${transfer} min to change trains – this train typically arrives +${delay} min late`,
    footerOpenSource: "Open source – view the code on GitHub",
    footerData: "Delay data:",
    footerDonate: "☕ Buy me a coffee",
    donateNudgeLead: "Found this useful?",
    donateNudgeLink: "☕ Buy me a coffee",
    footerLegal: "Legal notice & privacy",
    footerDisclaimer: "DelayBahn is an independent project and is not affiliated with Deutsche Bahn AG. “DB” and “Deutsche Bahn” are trademarks of Deutsche Bahn AG.",
    navRefund: "Apply delay compensation",
    refundCtaTitle: "Hit by over 1 hour of delay?",
    refundCtaLead: "See the journey you actually took, including delays and missed connections.",
    refundCtaSub: "Get your money back from DB in 3 easy clicks",
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
  scatter: { de: "delay-correlation.svg?v=3", en: "delay-correlation-en.svg?v=3", alt: "chartAlt" },
  violin: { de: "delay-violin.svg?v=2", en: "delay-violin-en.svg?v=2", alt: "violinAlt" },
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

function applyLang(lang) {
  state.lang = lang;
  localStorage.setItem("lang", lang);
  document.documentElement.lang = lang;
  document.title = t("pageTitle");

  document.querySelectorAll(".lang-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.lang === lang));

  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = t(el.dataset.i18n); });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => { el.placeholder = t(el.dataset.i18nPlaceholder); });
  document.querySelectorAll("[data-i18n-title]").forEach((el) => { el.title = t(el.dataset.i18nTitle); });

  updateChartImg();

  if (state.status) statusEl.textContent = t(state.status.key, ...state.status.params);
  if (claimModal.open) populateClaimModal();
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

// --- autocomplete ---

function setupAutocomplete(inputId, dropdownId, key) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  let timer = null;

  function showItems(items, recent) {
    dropdown.innerHTML = "";
    if (recent && items.length) {
      const label = document.createElement("div");
      label.className = "dropdown-label";
      label.textContent = t("recentLabel");
      dropdown.appendChild(label);
    }
    items.forEach((item) => {
      const div = document.createElement("div");
      div.textContent = item.name;
      div.addEventListener("mousedown", () => {
        state[key] = item;
        input.value = item.name;
        dropdown.classList.remove("open");
      });
      dropdown.appendChild(div);
    });
    dropdown.classList.toggle("open", items.length > 0);
  }

  function showRecents() {
    if (input.value.trim() === "") showItems(getRecents(), true);
  }

  input.addEventListener("focus", showRecents);

  input.addEventListener("input", () => {
    state[key] = null;
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) {
      dropdown.classList.remove("open");
      if (q === "") showRecents();
      return;
    }
    timer = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/locations?query=${encodeURIComponent(q)}`);
        if (!resp.ok) return;
        const items = await resp.json();
        showItems(items, false);
      } catch { /* network hiccup: ignore */ }
    }, 250);
  });

  input.addEventListener("blur", () => setTimeout(() => dropdown.classList.remove("open"), 150));
}

setupAutocomplete("from", "from-dropdown", "from");
setupAutocomplete("to", "to-dropdown", "to");

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
const controlsEl = document.getElementById("controls");
const searchBtn = document.getElementById("search");
const earlierBtn = document.getElementById("earlier");
const laterBtn = document.getElementById("later");

searchBtn.addEventListener("click", search);
earlierBtn.addEventListener("click", () => loadPage("earlier"));
laterBtn.addEventListener("click", () => loadPage("later"));

document.getElementById("window").addEventListener("change", () => {
  // window is aggregated server-side: refetch, but only if results are showing
  if (state.journeys.length && state.from && state.to) search();
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

async function setMode(mode) {
  if (state.mode === mode) return;
  state.mode = mode;
  document.body.classList.toggle("past-mode", mode === "past");
  state.journeys = [];
  state.earlierRef = state.laterRef = null;
  resultsEl.innerHTML = "";
  controlsEl.classList.add("hidden");
  earlierBtn.classList.add("hidden");
  laterBtn.classList.add("hidden");
  document.getElementById("past-disclaimer").classList.add("hidden");
  setDonateNudge(false);
  statusEl.classList.remove("error");
  setStatus(null);
  searchBtn.dataset.i18n = mode === "past" ? "searchPast" : "search";
  searchBtn.textContent = t(searchBtn.dataset.i18n);
  const dateEl = document.getElementById("date");
  if (mode === "past") {
    document.getElementById("hero-chart").classList.add("hidden");
    await ensureCoverage();
    if (state.coverage?.minDay) {
      // live IRIS lookups extend the pickable range to today
      const maxDay = latestPastDay();
      dateEl.min = state.coverage.minDay;
      dateEl.max = maxDay;
      if (dateEl.value < dateEl.min || dateEl.value > dateEl.max) dateEl.value = maxDay;
      document.getElementById("past-coverage").textContent =
        `${fmtDateFull(state.coverage.minDay)} – ${fmtDateFull(maxDay)}`;
    }
  } else {
    document.getElementById("hero-chart").classList.remove("hidden");
    document.getElementById("refund-cta").classList.remove("hidden");
    dateEl.min = "";
    dateEl.max = "";
    dateEl.value = new Date().toISOString().slice(0, 10);
  }
}

document.getElementById("refund-cta").addEventListener("click", () => {
  track("refund-cta");
  setMode("past");
  document.getElementById("from").focus();
});
document.getElementById("refund-nav").addEventListener("click", () => {
  track("refund-nav");
  setMode("past");
  document.getElementById("from").focus();
});
document.getElementById("past-exit").addEventListener("click", () => setMode("future"));
document.getElementById("donate-footer-item").hidden = !DONATE_ENABLED;
document.getElementById("donate-footer").addEventListener("click", () =>
  track("donate", { placement: "footer" }));
document.querySelector("#donate-nudge a").addEventListener("click", () =>
  track("donate", { placement: "nudge" }));

async function fetchJourneys(pagingRef) {
  const win = document.getElementById("window").value;
  const params = new URLSearchParams({
    from: state.from.id, to: state.to.id, departure: state.departure, window: win,
  });
  if (state.mode === "past") params.set("mode", "past");
  if (pagingRef) params.set("pagingRef", pagingRef);
  const resp = await fetch(`/api/journeys?${params}`);
  if (!resp.ok) {
    const body = await resp.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${resp.status}`);
  }
  state.windowUsed = Number(win);
  return resp.json();
}

function updatePageButtons() {
  earlierBtn.classList.toggle("hidden", !state.journeys.length || !state.earlierRef);
  laterBtn.classList.toggle("hidden", !state.journeys.length || !state.laterRef);
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
  if (state.mode === "past") params.set("mode", "past");
  history.replaceState(null, "", `?${params}`);
}

async function search() {
  await Promise.all([resolveTyped("from"), resolveTyped("to")]);
  if (!state.from || !state.to) {
    setStatus("pickStations");
    statusEl.classList.add("error");
    return;
  }
  saveRecent(state.from);
  saveRecent(state.to);
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
  state.departure = `${document.getElementById("date").value}T${document.getElementById("time").value}:00`;
  syncUrl();
  track("search", {
    from: state.from.name,
    to: state.to.name,
    window: Number(document.getElementById("window").value),
    mode: state.mode,
  });
  statusEl.classList.remove("error");
  setStatus("searching");
  resultsEl.innerHTML = "";
  controlsEl.classList.add("hidden");
  earlierBtn.classList.add("hidden");
  laterBtn.classList.add("hidden");
  document.getElementById("hero-chart").classList.add("hidden");
  document.getElementById("refund-cta").classList.add("hidden");
  setDonateNudge(false);
  searchBtn.disabled = true;

  try {
    const data = await fetchJourneys(null);
    state.journeys = data.journeys || [];
    state.earlierRef = data.earlierRef || null;
    state.laterRef = data.laterRef || null;
    if (state.journeys.length) setStatus(null);
    else setStatus("noResults");
    controlsEl.classList.toggle("hidden", state.journeys.length === 0);
    document.getElementById("past-disclaimer").classList.toggle(
      "hidden", !(state.mode === "past" && state.journeys.length));
    setDonateNudge(state.journeys.length > 0);
    updatePageButtons();
    render();
  } catch (e) {
    setStatus("error", e.message);
    statusEl.classList.add("error");
  } finally {
    searchBtn.disabled = false;
  }
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
  btn.disabled = true;
  statusEl.classList.remove("error");

  try {
    const data = await fetchJourneys(ref);
    const seen = new Set(state.journeys.map(journeyKey));
    const fresh = (data.journeys || []).filter((j) => !seen.has(journeyKey(j)));
    if (dir === "earlier") {
      state.journeys = [...fresh, ...state.journeys];
      state.earlierRef = data.earlierRef || null;
    } else {
      state.journeys = [...state.journeys, ...fresh];
      state.laterRef = data.laterRef || null;
    }
    updatePageButtons();
    render();
  } catch (e) {
    setStatus("error", e.message);
    statusEl.classList.add("error");
  } finally {
    btn.disabled = false;
  }
}

// --- language toggle ---

document.querySelectorAll(".lang-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    track("lang", { lang: btn.dataset.lang });
    applyLang(btn.dataset.lang);
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
    js.sort((a, b) => {
      const aMissing = a.price == null, bMissing = b.price == null;
      if (aMissing !== bMissing) return aMissing ? 1 : -1;  // no price last
      if (aMissing && bMissing) return 0;
      return a.price - b.price;  // stable sort keeps departure order on ties
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

function fmtDuration(seconds) {
  if (seconds == null) return "";
  const mins = Math.round(seconds / 60);
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}min`;
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
    w.textContent = `${t("walk")} · ${leg.origin?.name || ""} → ${leg.destination?.name || ""}`;
    row.appendChild(w);
    return row;
  }
  const train = document.createElement("span");
  train.className = "train";
  train.textContent = leg.line?.name || t("train");
  const desc = document.createElement("span");
  desc.className = "leg-desc";
  desc.textContent = `${leg.origin?.name || ""} ${fmtTime(leg.plannedDeparture || leg.departure)} → ` +
    `${leg.destination?.name || ""} ${fmtTime(leg.plannedArrival || leg.arrival)}`;
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
      }
      if (slots.length <= 10) {
        svg.appendChild(svgEl("text", {
          x: cx, y: v >= 0 ? y(v) - 4 : y(v) + 11, "text-anchor": "middle",
          "font-size": 10, fill: "#646973",
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
    else if (rec) tipY = y(Math.max(rec.delay, 0)) - (slots.length <= 10 && rec.delay >= 0 ? 14 : 0);

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

function bahnDeUrl(journey) {
  const legs = journey.legs || [];
  const first = legs[0], last = legs[legs.length - 1];
  const fromName = first.origin?.name || "", fromEva = first.origin?.id || "";
  const toName = last.destination?.name || "", toEva = last.destination?.id || "";
  const hd = (first.plannedDeparture || "").slice(0, 19);
  const soid = encodeURIComponent(`A=1@O=${fromName}@L=${fromEva}@`);
  const zoid = encodeURIComponent(`A=1@O=${toName}@L=${toEva}@`);
  return `https://www.bahn.de/buchung/fahrplan/suche#sts=true&so=${encodeURIComponent(fromName)}` +
    `&zo=${encodeURIComponent(toName)}&soid=${soid}&zoid=${zoid}&hd=${hd}&kl=2`;
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

function render() {
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
    } else {
      times.textContent = `${fmtTime(first.plannedDeparture)} → ${fmtTime(last.plannedArrival)}`;
    }

    const meta = document.createElement("span");
    meta.className = "journey-meta";
    meta.textContent = `${fmtDuration(journey.durationSeconds)} · ` +
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
      const finalStats = finalLeg ? finalLeg.delayStats : null;
      const unlikelyTts = (journey.tightTransfers || []).filter((tt) => tt.unlikely);
      if (unlikelyTts.length) {
        // final-leg stats are meaningless if an earlier connection is likely missed
        badge = document.createElement("span");
        badge.className = "badge red";
        badge.textContent = t("unlikelyBadge");
        badge.title = t("unlikelyBadgeTooltip", unlikelyTts.map((tt) => tt.station).join(", "));
      } else if (UNTRACKED_PRODUCTS.has(finalLeg?.line?.product)) {
        badge = notTrackedBadge();
      } else {
        badge = delayBadge(finalStats, true);
        if (finalStats) wireDayChart(badge, finalStats, head, finalLeg.line?.name);
        const tts = journey.tightTransfers || [];
        if (tts.length) {
          tightBadge = document.createElement("span");
          tightBadge.className = "badge yellow";
          tightBadge.textContent = t("tightBadge");
          tightBadge.title = t("tightBadgeTooltip", tts.map((tt) => tt.station).join(", "));
        }
      }
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
      const price = document.createElement("span");
      price.className = "price";
      if (journey.price != null) {
        price.textContent = t("priceFrom", journey.price);
      } else {
        price.classList.add("price-na");
        price.textContent = t("priceNa");
      }

      const book = document.createElement("a");
      book.className = "book-btn";
      book.textContent = t("book");
      book.href = bahnDeUrl(journey);
      book.target = "_blank";
      book.rel = "noopener";
      book.addEventListener("click", () =>
        track("book-bahn", {
          from: state.from?.name,
          to: state.to?.name,
          price: journey.price ?? "na",
        })
      );

      // badges, price and booking button wrap together as one right-aligned block
      const cta = document.createElement("div");
      cta.className = "journey-cta";
      // next to a tight-transfer warning the delay badge is only worth the space when red
      const showDelayBadge = !tightBadge || badge.classList.contains("red");
      cta.append(...(tightBadge ? [tightBadge] : []), ...(showDelayBadge ? [badge] : []), price, book);
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
      legsEl.appendChild(row);
      const tt = warnByLeg.get(i);
      if (tt) {
        const warn = document.createElement("div");
        warn.className = "leg-tight";
        const lead = document.createElement("strong");
        lead.textContent = tt.unlikely ? `⛔ ${t("unlikelyTitle")}` : `⚠ ${t("tightTitle")}`;
        warn.append(lead, document.createTextNode(" " + t("tightDetail", tt.transferMinutes, tt.medianDelay)));
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

// --- init ---

applyLang(state.lang);

// restore a search from the URL (refresh, bookmark, shared link)
const qp = new URLSearchParams(location.search);
(async () => {
  if (qp.get("mode") === "past") await setMode("past");
  if (qp.get("fromId") && qp.get("toId")) {
    state.from = { id: qp.get("fromId"), name: qp.get("from") || "" };
    state.to = { id: qp.get("toId"), name: qp.get("to") || "" };
    document.getElementById("from").value = state.from.name;
    document.getElementById("to").value = state.to.name;
    // date after setMode so a restored past date wins over the coverage clamp
    if (qp.get("date")) document.getElementById("date").value = qp.get("date");
    if (qp.get("time")) document.getElementById("time").value = qp.get("time");
    if (["7", "15", "30"].includes(qp.get("window"))) document.getElementById("window").value = qp.get("window");
    search();
  }
})();
