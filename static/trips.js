"use strict";
/* Meine Fahrten: the journeys this account opened on bahn.de, split into
   upcoming and past. The list is fetched with the account's token, so a
   signed-out visitor only sees the invitation to log in. Everything from the
   server is rendered via textContent - nothing reaches innerHTML. */
(function () {

const I18N = {
  de: {
    docTitle: "Meine Fahrten – DelayBahn",
    headerTitle: "Meine Fahrten",
    tagline: "Deine Buchungen auf einen Blick",
    intro: "Verbindungen, die du dir mit dem Lesezeichen gemerkt oder über „Auf bahn.de buchen“ geöffnet hast. Nicht gebucht? Einfach entfernen.",
    loginTitle: "Anmelden, um deine Fahrten zu sehen",
    loginLead: "Angemeldet merkt sich DelayBahn jede Verbindung, die du dir merkst oder auf bahn.de buchst – als Liste deiner nächsten und vergangenen Fahrten.",
    loginBtn: "Anmelden",
    nextHeading: "Nächste Fahrten",
    pastHeading: "Vergangene Fahrten",
    nextEmpty: "Noch keine anstehende Fahrt.",
    nextEmptyLink: "Verbindung suchen →",
    pastEmpty: "Noch keine vergangene Fahrt.",
    onboardTitle: "Noch keine Fahrten gespeichert",
    onboardLead: "Merk dir deine Verbindungen über das Lesezeichen neben jedem Buchen-Button – DelayBahn zeigt dir dann, wie viel Zeit dich Verspätungen und Ausfälle wirklich gekostet haben.",
    onboardBtn: "Verbindung suchen",
    loading: "Lade deine Fahrten …",
    loadError: "Das hat gerade nicht geklappt – bitte später noch einmal versuchen.",
    statsHeading: "Deine Bilanz",
    spanWeek: "Woche",
    spanMonth: "Monat",
    spanYear: "Jahr",
    statDelay: "Verloren durch Verspätung",
    statCancel: "Verloren durch Ausfälle",
    statCancelNote: (n) => n === 1 ? "1 Fahrt betroffen" : `${n} Fahrten betroffen`,
    statNote: (known, total) => `Verspätungen bei ${known} von ${total} Fahrten bekannt.`,
    statTime: "Zeit im Zug",
    statTrips: "Fahrten",
    delayTitle: "Verspätung am Ziel an diesem Tag",
    canceled: "Ausgefallen",
    kindOutbound: "Hinfahrt",
    kindReturn: "Rückfahrt",
    check: "Verspätung & Entschädigung prüfen",
    reportBtn: "Störung melden",
    storyBtn: "Geschichte erzählen",
    pickReport: "Diesen Zug melden",
    pickStory: "Über diesen Zug erzählen",
    pickHintReport: "Welchen Zug möchtest du melden? Wähle ihn mit dem Pfeil.",
    pickHintStory: "Um welchen Zug geht es? Wähle ihn mit dem Pfeil.",
    pickWhat: "Was ist passiert?",
    problem_delay: "Verspätung",
    problem_cancelled: "Zugausfall",
    problem_missed: "Anschluss verpasst",
    problem_ac: "Klimaanlage defekt",
    problem_wc: "WC defekt/schmutzig",
    problem_crowding: "Überfüllt",
    problem_wifi: "WLAN geht nicht",
    problem_other: "Sonstiges",
    removeTitle: "Aus der Liste entfernen",
    checkBusy: "Prüfe …",
    checkThrottle: "Zu viele Anfragen – bitte kurz warten.",
    walk: "Fußweg",
    walkMinutes: (n) => `${n} min`,
    train: "Zug",
    legMissed: "verpasst",
    legCanceled: "ausgefallen",
    noData: "keine Daten",
    notTracked: "nicht erfasst",
    notTrackedTooltip: "Für U-Bahn, Tram, Bus und Fähre werden keine Verspätungsdaten erhoben",
    notYet: "noch offen",
    notYetTooltip: "Für diesen Halt liegt noch keine Ist-Meldung vor – sie kommt spätestens am nächsten Morgen dazu.",
    thatDayTooltip: "Tatsächliche Ankunftsverspätung an diesem Tag",
    simBadgeTooltip: "Simulierte Verspätung am Ziel – verpasste Anschlüsse und tatsächliche Weiterfahrt berücksichtigt",
    simContinuation: "↳ Tatsächliche Weiterfahrt mit der nächsten möglichen Verbindung:",
    simIncomplete: "Keine Ersatzverbindung in den Daten gefunden – tatsächliche Ankunft unbekannt",
    missedBadge: "⛔ Anschluss verpasst",
    claimPct: (pct) => `${pct} % zurückholen →`,
    claimNone: "Keine Entschädigung (unter 60 min)",
    claimCanceled: "Ausgefallen – Anspruch prüfen →",
    claimMissed: "Anschluss verpasst – Anspruch prüfen →",
    claimPending: "Ankunft noch nicht bestätigt – morgen früh prüfen",
    badgeDays: (matched, total) => `(${matched}/${total} Tage)`,
    badgeTooltip: (win, max) => `Mittlere Ankunftsverspätung (Median) der letzten ${win} Tage (max. +${max} min)`,
    cancelNote: (win, n) => `⚠ In den letzten ${win} Tagen ${n}× (teil-)ausgefallen`,
    tightTitle: "Knapper Umstieg:",
    unlikelyTitle: "Unwahrscheinlicher Umstieg:",
    tightDetail: (transfer, delay) => `${transfer} min Umstiegszeit – dieser Zug kommt typischerweise +${delay} min verspätet an`,
    unlikelyBadge: "⛔ Anschlussrisiko",
    unlikelyBadgeTooltip: (stations) => `Die typische Verspätung übersteigt die Umstiegszeit deutlich (${stations})`,
    tightBadge: "⚠ Knapper Umstieg",
    tightBadgeTooltip: (stations) => `Die typische Verspätung lässt wenig Umstiegszeit (${stations})`,
    asShown: "Verspätungsstatistik zum Zeitpunkt des Merkens",
    planBtn: "Verspätungsstatistik anzeigen",
    changes: (n) => n === 0 ? "ohne Umstieg" : n === 1 ? "1 Umstieg" : `${n} Umstiege`,
    navLogin: "Anmelden",
    navLogout: "Abmelden",
    navTrips: "Meine Fahrten",
    footerBack: "← Zur Verbindungssuche",
    footerStories: "Delay Geschichten",
    footerLegal: "Impressum & Datenschutz",
    footerContact: "Kontakt",
  },
  en: {
    docTitle: "My Trips – DelayBahn",
    headerTitle: "My Trips",
    tagline: "Your bookings at a glance",
    intro: "Connections you bookmarked or opened with “Book on bahn.de”. Didn't book one? Just remove it.",
    loginTitle: "Log in to see your trips",
    loginLead: "Logged in, DelayBahn remembers every connection you bookmark or book on bahn.de – as a list of your next and past trips.",
    loginBtn: "Log in",
    nextHeading: "Next trips",
    pastHeading: "Past trips",
    nextEmpty: "No upcoming trip yet.",
    nextEmptyLink: "Search a connection →",
    pastEmpty: "No past trip yet.",
    onboardTitle: "No trips saved yet",
    onboardLead: "Bookmark a connection next to its booking button and DelayBahn will show you how much time delays and cancellations have really cost you.",
    onboardBtn: "Search a connection",
    loading: "Loading your trips …",
    loadError: "That didn't work right now – please try again later.",
    statsHeading: "Your tally",
    spanWeek: "Week",
    spanMonth: "Month",
    spanYear: "Year",
    statDelay: "Lost to delays",
    statCancel: "Lost to cancellations",
    statCancelNote: (n) => n === 1 ? "1 trip affected" : `${n} trips affected`,
    statNote: (known, total) => `Delays known for ${known} of ${total} trips.`,
    statTime: "Time on trains",
    statTrips: "Trips",
    delayTitle: "Delay on arrival that day",
    canceled: "Cancelled",
    kindOutbound: "Outbound",
    kindReturn: "Return",
    check: "Check delay & compensation",
    reportBtn: "Report train issues",
    storyBtn: "Share delay story",
    pickReport: "Report this train",
    pickStory: "Tell about this train",
    pickHintReport: "Which train do you want to report? Pick it with the arrow.",
    pickHintStory: "Which train is your story about? Pick it with the arrow.",
    pickWhat: "What went wrong?",
    problem_delay: "Delayed",
    problem_cancelled: "Cancelled",
    problem_missed: "Missed connection",
    problem_ac: "AC not working",
    problem_wc: "WC broken/dirty",
    problem_crowding: "Overcrowded",
    problem_wifi: "Wi-Fi not working",
    problem_other: "Other",
    removeTitle: "Remove from the list",
    checkBusy: "Checking …",
    checkThrottle: "Too many requests – please wait a moment.",
    walk: "Walk",
    walkMinutes: (n) => `${n} min`,
    train: "Train",
    legMissed: "missed",
    legCanceled: "cancelled",
    noData: "no data",
    notTracked: "not tracked",
    notTrackedTooltip: "Delay data isn't collected for metro, tram, bus and ferry services",
    notYet: "pending",
    notYetTooltip: "No actual time reported for this stop yet – it lands by tomorrow morning at the latest.",
    thatDayTooltip: "Actual arrival delay on this day",
    simBadgeTooltip: "Simulated delay at destination – missed connections and the actual onward journey taken into account",
    simContinuation: "↳ Actual onward journey with the next possible connection:",
    simIncomplete: "No replacement connection found in the data – actual arrival unknown",
    missedBadge: "⛔ Missed connection",
    claimPct: (pct) => `Get ${pct}% back →`,
    claimNone: "No compensation (under 60 min)",
    claimCanceled: "Cancelled – check your claim →",
    claimMissed: "Missed connection – check your claim →",
    claimPending: "Arrival not confirmed yet – check tomorrow morning",
    badgeDays: (matched, total) => `(${matched}/${total} days)`,
    badgeTooltip: (win, max) => `Median arrival delay over the last ${win} days (max. +${max} min)`,
    cancelNote: (win, n) => `⚠ (Partially) cancelled ${n}× in the last ${win} days`,
    tightTitle: "Tight transfer:",
    unlikelyTitle: "Unlikely transfer:",
    tightDetail: (transfer, delay) => `${transfer} min to change trains – this train typically arrives +${delay} min late`,
    unlikelyBadge: "⛔ Connection risk",
    unlikelyBadgeTooltip: (stations) => `Typical delay far exceeds the transfer time (${stations})`,
    tightBadge: "⚠ Tight transfer",
    tightBadgeTooltip: (stations) => `Typical delay leaves little time to change trains (${stations})`,
    asShown: "Delay statistics as of when you saved the trip",
    planBtn: "Show delay statistics",
    changes: (n) => n === 0 ? "no change" : n === 1 ? "1 change" : `${n} changes`,
    navLogin: "Login",
    navLogout: "Logout",
    navTrips: "My trips",
    footerBack: "← Back to the journey search",
    footerStories: "Delay Stories",
    footerLegal: "Legal notice & privacy",
    footerContact: "Contact",
  },
};

const lang = location.pathname.startsWith("/en/") ? "en" : "de";
const t = (key, ...args) => {
  const entry = I18N[lang][key];
  return typeof entry === "function" ? entry(...args) : entry;
};
const $ = (id) => document.getElementById(id);
// no-op when the Umami script is blocked or unavailable
const track = (name, data) => window.umami?.track(name, data);

const SELF = lang === "en" ? "/en/my-trips" : "/meine-fahrten";
const HOME = lang === "en" ? "/en/" : "/";
const STORIES = lang === "en" ? "/stories" : "/geschichten";
// the damage report's tiles, in the stories page's order (BOARD_CODES in stories.js)
const PROBLEMS = ["delay", "cancelled", "missed", "ac", "wc", "crowding", "wifi", "other"];
// where a claim is filed: the trip overview on bahn.de, like the search's claim modal
const CLAIM_URL = "https://www.bahn.de/buchung/reiseuebersicht/vergangene";
// products IRIS never covers, as in the search (UNTRACKED_PRODUCTS in app/main.py)
const UNTRACKED = new Set(["BUS", "TRAM", "UBAHN", "SCHIFF", "ANRUFPFLICHTIG"]);
const LOGIN = "/login?next=" + encodeURIComponent(SELF) + "&reason=trips";

let fb = null;       // firebase.js module
let me = null;       // { user, name } for a finished account, else null
let current = null;  // the last /api/trips answer: the tally and a removal work on it
let span = "year";   // the tally's window

function applyStatic() {
  document.documentElement.lang = lang;
  document.title = t("docTitle");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18n];
    if (typeof text === "string") node.textContent = text;
  });
  document.querySelectorAll(".lang-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.lang === lang);
  });
  $("trips-login-btn").href = LOGIN;
  $("auth-login").href = LOGIN;
  $("auth-name").href = SELF;
  $("auth-name").title = t("navTrips");
}

function renderAuth() {
  $("auth-login").classList.toggle("hidden", !!me);
  $("auth-user").classList.toggle("hidden", !me);
  $("auth-name").textContent = me ? me.name : "";
}

function setStatus(key, error) {
  const el = $("trips-status");
  el.textContent = key ? t(key) : "";
  el.classList.toggle("error", !!error);
}

// --- formatting: the stamps are Berlin-local naive ISO ("2026-09-05T08:12:00") ---

function fmtDay(iso) {
  return new Date(`${iso.slice(0, 10)}T12:00:00`).toLocaleDateString(
    lang === "en" ? "en-GB" : "de-DE",
    { weekday: "short", day: "numeric", month: "short", year: "numeric" },
  );
}

const fmtTime = (iso) => (iso ? iso.slice(11, 16) : "–");

const fmtMinutes = (mins) => (mins >= 60
  ? `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}min` : `${mins} min`);

// planned minutes between the two stamps; parsed in the browser's zone, which cancels out
const tripMinutes = (trip) => Math.round((new Date(trip.arrival) - new Date(trip.departure)) / 60000);

function fmtDuration(dep, arr) {
  const mins = Math.round((new Date(arr) - new Date(dep)) / 60000);
  return mins > 0 ? fmtMinutes(mins) : "";
}

// --- the tally: what the past trips of the window add up to ---

// the window's first day as YYYY-MM-DD, in the calendar of the server's Berlin clock
function spanStart(now) {
  const today = now.slice(0, 10);
  if (span === "year") return `${today.slice(0, 4)}-01-01`;
  if (span === "month") return `${today.slice(0, 7)}-01`;
  const d = new Date(`${today}T12:00:00`);
  d.setDate(d.getDate() - ((d.getDay() + 6) % 7));  // back to Monday
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* Time lost is the delay at the destination summed over the window's past
   trips, an early arrival counting as nothing lost - split by what caused
   it: a trip the check had to route around a cancelled train counts under
   cancellations, every other under delays. Trips without a known arrival
   are left out and the footnote says so. */
function renderStats() {
  const section = $("trips-stats");
  const past = current ? current.trips.filter((tr) => tr.arrival <= current.now) : [];
  section.classList.toggle("hidden", past.length === 0);
  if (!past.length) return;
  const start = spanStart(current.now);
  const inSpan = past.filter((tr) => tr.departure.slice(0, 10) >= start);
  let lostDelay = 0, lostCancel = 0, known = 0, cancelTrips = 0, minutes = 0;
  for (const tr of inSpan) {
    minutes += Math.max(0, tripMinutes(tr));
    if (tr.canceled) cancelTrips++;
    if (tr.delay == null) continue;
    known++;
    if (tr.canceled) lostCancel += Math.max(0, tr.delay);
    else lostDelay += Math.max(0, tr.delay);
  }
  const paint = (el, value, anyKnown) => {
    el.textContent = anyKnown ? fmtMinutes(value) : "–";
    el.classList.toggle("lost", anyKnown && value > 0);
    el.classList.toggle("none", anyKnown && value === 0);
  };
  paint($("stat-delay"), lostDelay, known > 0);
  paint($("stat-cancel"), lostCancel, known > 0);
  $("stat-cancel-note").textContent = cancelTrips ? t("statCancelNote", cancelTrips) : "";
  $("stat-note").textContent = known < inSpan.length ? t("statNote", known, inSpan.length) : "";
  $("stat-time").textContent = fmtMinutes(minutes);
  $("stat-trips").textContent = String(inSpan.length);
  document.querySelectorAll(".board-span").forEach((b) => b.classList.toggle("active", b.dataset.span === span));
}

document.querySelectorAll(".board-span").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.span === span) return;
    span = btn.dataset.span;
    renderStats();
    track("trips-span", { span });
  });
});

// --- the in-place check: the compensation page's card for one past trip ---
// Same badges, same leg rows and the same verdict as a past-mode search,
// built from /api/trips/{id}/check; nothing leaves the page.

function badge(cls, text, title) {
  const el = document.createElement("span");
  el.className = "badge " + cls;
  el.textContent = text;
  if (title) el.title = title;
  return el;
}

const delayBadge = (v, title) =>
  badge(v < 3 ? "green" : v < 10 ? "yellow" : "red", `${v >= 0 ? "+" : ""}${v} min`, title);

// a leg's delay on the day; on a live day a missing observation is "not yet"
function dayBadge(d, live) {
  if (!d) return badge("gray", live ? t("notYet") : t("noData"), live ? t("notYetTooltip") : "");
  if (d.canceled) return badge("red", t("legCanceled"));
  return delayBadge(d.delayMin, t("thatDayTooltip"));
}

function legRow(leg, struck, live) {
  const row = document.createElement("div");
  row.className = "leg";
  const origin = leg.origin?.name || "", dest = leg.destination?.name || "";
  if (leg.walking) {
    row.classList.add("leg-walk");
    const w = document.createElement("span");
    w.className = "walk";
    const mins = leg.plannedDeparture && leg.plannedArrival
      ? Math.round((new Date(leg.plannedArrival) - new Date(leg.plannedDeparture)) / 60000) : 0;
    w.textContent = [t("walk"), mins > 0 ? t("walkMinutes", mins) : null, `${origin} → ${dest}`]
      .filter(Boolean).join(" · ");
    row.appendChild(w);
    return row;
  }
  const train = document.createElement("span");
  train.className = "train";
  train.textContent = leg.line?.name || t("train");
  const desc = document.createElement("span");
  desc.className = "leg-desc";
  desc.textContent = `${origin} ${fmtTime(leg.plannedDeparture)} → ${dest} ${fmtTime(leg.plannedArrival)}`;
  let mark;
  if (struck) mark = leg.delayOnDate?.canceled ? badge("red", t("legCanceled")) : badge("gray", t("legMissed"));
  else if (UNTRACKED.has(leg.line?.product)) mark = badge("gray", t("notTracked"), t("notTrackedTooltip"));
  else mark = dayBadge(leg.delayOnDate, live);
  row.append(train, desc, mark);
  if (struck) row.classList.add("leg-missed");
  return row;
}

function buildCheck(data) {
  const panel = document.createElement("div");
  panel.className = "trip-check";
  // the hand-off to the stories page: shown above the legs while a leg is being picked
  const hint = document.createElement("div");
  hint.className = "pick-hint";
  const legs = data.legs || [];
  const trainLegs = legs.filter((l) => !l.walking);
  const finalLeg = trainLegs[trainLegs.length - 1];
  const sim = data.simulation;
  const missedTransfers = data.missedTransfers || [];
  const missed = missedTransfers.length > 0;

  // the arrival at the destination, as the search's card head shows it
  let head;
  if (sim && data.arrivalDelay != null) head = delayBadge(data.arrivalDelay, t("simBadgeTooltip"));
  else if (missed) head = badge("red", t("missedBadge"), missedTransfers.map((mt) => mt.station).join(", "));
  else if (UNTRACKED.has(finalLeg?.line?.product)) head = badge("gray", t("notTracked"), t("notTrackedTooltip"));
  else head = dayBadge(finalLeg?.delayOnDate, data.liveDay);

  const pct = data.compensationPct;
  // with a completed simulation pct reflects the realistic arrival; the
  // cancelled/missed wordings only apply when the outcome stayed unknown
  const canceledish = data.arrivalCanceled || missedTransfers.some((mt) => mt.canceled);
  const claimable = (pct != null && pct >= 25) || (pct == null && (canceledish || missed));
  let verdict;
  if (claimable) {
    verdict = document.createElement("a");
    verdict.className = "claim-btn";
    verdict.href = CLAIM_URL;
    verdict.target = "_blank";
    verdict.rel = "noopener";
    verdict.textContent = pct != null && pct >= 25 ? t("claimPct", pct)
      : canceledish ? t("claimCanceled") : t("claimMissed");
    verdict.addEventListener("click", () => track("claim-bahn", { via: "my-trips" }));
  } else {
    verdict = document.createElement("span");
    verdict.className = "claim-none";
    verdict.textContent = pct === 0 ? t("claimNone") : data.pending ? t("claimPending") : t("noData");
  }
  const top = document.createElement("div");
  top.className = "trip-check-verdict";
  top.append(head, verdict);

  const legsEl = document.createElement("div");
  legsEl.className = "legs";
  const missedAt = sim ? sim.missedAtLegIndex : null;
  const addRows = (list, live) => list.forEach((leg, i) => {
    const struck = missedAt != null && list === legs && i >= missedAt;
    const row = legRow(leg, struck, live);
    // the trip's own trains can be picked; the replan's are the check's guess, not a ride
    if (list === legs && !leg.walking) row.appendChild(legPick(panel, leg, struck));
    if (i === 0) row.classList.add("rail-first");
    if (i === list.length - 1) row.classList.add("rail-last");
    legsEl.appendChild(row);
  });
  addRows(legs, data.liveDay);
  if (sim?.legs?.length) {
    const cont = document.createElement("div");
    cont.className = "leg-continuation";
    cont.textContent = t("simContinuation");
    legsEl.appendChild(cont);
    addRows(sim.legs, data.liveDay);
  }
  if (sim?.incomplete) {
    const note = document.createElement("div");
    note.className = "sim-note";
    note.textContent = t("simIncomplete");
    legsEl.appendChild(note);
  }
  panel.append(top, hint, legsEl);
  return panel;
}

/* The stories page with a train of the trip in its form: `story` opens the
   compose form, `report` the damage report's tap form. The stations, day,
   departure and train travel as query parameters the page reads on load;
   the leg's outcome on the day lets it pre-tick what went wrong; a report
   names the tile the visitor chose. */
function storiesLink(kind, leg, struck, problem) {
  const d = leg.delayOnDate;
  const dep = leg.plannedDeparture || "";
  const params = new URLSearchParams({
    trip: kind, from: leg.origin?.name || "", to: leg.destination?.name || "",
    date: dep.slice(0, 10), time: dep.slice(11, 16), train: leg.line?.name || "",
  });
  if (d?.delayMin != null) params.set("delay", String(d.delayMin));
  if (d?.canceled) params.set("canceled", "1");
  else if (struck) params.set("missed", "1");
  if (problem) params.set("problem", problem);
  return `${STORIES}?${params}`;
}

/* A leg row's hand-off: an arrow pointed at the form of whichever mode the
   panel is in. A story goes straight to the compose form; a report first
   asks what went wrong, as chips under the row, each leading to its tile. */
function legPick(panel, leg, struck) {
  const pick = document.createElement("a");
  pick.className = "leg-pick";
  pick.textContent = "→";
  pick.dataset.report = storiesLink("report", leg, struck);
  pick.dataset.story = storiesLink("story", leg, struck);
  pick.addEventListener("click", (e) => {
    if (panel.dataset.pick !== "report") {
      track("trip-story");
      return;
    }
    e.preventDefault();
    const row = pick.closest(".leg");
    const open = row.nextElementSibling?.classList.contains("leg-problems");
    closeProblems(panel);
    if (open) return;
    row.after(problemChips(leg, struck));
    pick.setAttribute("aria-expanded", "true");
  });
  return pick;
}

function problemChips(leg, struck) {
  const box = document.createElement("div");
  box.className = "leg-problems";
  const lead = document.createElement("div");
  lead.className = "leg-problems-lead";
  lead.textContent = t("pickWhat");
  const chips = document.createElement("div");
  chips.className = "chips";
  for (const code of PROBLEMS) {
    const chip = document.createElement("a");
    chip.className = "chip";
    chip.href = storiesLink("report", leg, struck, code);
    chip.textContent = t("problem_" + code);
    chip.addEventListener("click", () => track("trip-report", { problem: code }));
    chips.appendChild(chip);
  }
  box.append(lead, chips);
  return box;
}

// at most one leg asks what went wrong at a time
function closeProblems(panel) {
  panel.querySelectorAll(".leg-problems").forEach((el) => el.remove());
  panel.querySelectorAll(".leg-pick").forEach((a) => a.removeAttribute("aria-expanded"));
}

// --- an upcoming trip: the search's card as it stood when the trip was filed ---
// The statistics are the ones the visitor pressed on, straight from the
// snapshot: what they saw, not a fresh lookup.

function statsBadge(stats, big, window) {
  if (!stats || stats.medianDelay == null) return badge("gray", t("noData"));
  const v = stats.medianDelay;
  const el = badge(v < 3 ? "green" : v < 10 ? "yellow" : "red", `${v >= 0 ? "+" : ""}${v} min`,
    t("badgeTooltip", window, stats.maxDelay));
  if (big && stats.daysMatched != null) {
    const small = document.createElement("small");
    small.textContent = " " + t("badgeDays", stats.daysMatched, window);
    el.appendChild(small);
  }
  return el;
}

function planLegRow(leg, window) {
  const row = document.createElement("div");
  row.className = "leg";
  const origin = leg.origin || "", dest = leg.destination || "";
  if (leg.walking) {
    row.classList.add("leg-walk");
    const w = document.createElement("span");
    w.className = "walk";
    const mins = leg.plannedDeparture && leg.plannedArrival
      ? Math.round((new Date(leg.plannedArrival) - new Date(leg.plannedDeparture)) / 60000) : 0;
    w.textContent = [t("walk"), mins > 0 ? t("walkMinutes", mins) : null, `${origin} → ${dest}`]
      .filter(Boolean).join(" · ");
    row.appendChild(w);
    return row;
  }
  const train = document.createElement("span");
  train.className = "train";
  train.textContent = leg.line || t("train");
  const desc = document.createElement("span");
  desc.className = "leg-desc";
  desc.textContent = `${origin} ${fmtTime(leg.plannedDeparture)} → ${dest} ${fmtTime(leg.plannedArrival)}`;
  const mark = UNTRACKED.has(leg.product)
    ? badge("gray", t("notTracked"), t("notTrackedTooltip"))
    : statsBadge(leg.delayStats, false, window);
  row.append(train, desc, mark);
  return row;
}

/* The search's card for the trip, folded away until its button is pressed:
   the head badges on top, then the legs with their statistics and
   tight-transfer warnings, and the cancellation note. */
function buildPlan(trip) {
  const panel = document.createElement("div");
  panel.className = "trip-check hidden";
  const legs = trip.legs || [];
  const trainLegs = legs.filter((l) => !l.walking);
  const finalLeg = trainLegs[trainLegs.length - 1];
  const tts = trip.tightTransfers || [];
  const unlikely = tts.filter((tt) => tt.unlikely);
  const window = trip.window || 7;
  let head, tight = null;
  if (unlikely.length) {
    // final-leg stats are meaningless if an earlier connection is likely missed
    head = badge("red", t("unlikelyBadge"), t("unlikelyBadgeTooltip", unlikely.map((tt) => tt.station).join(", ")));
  } else if (UNTRACKED.has(finalLeg?.product)) {
    head = badge("gray", t("notTracked"), t("notTrackedTooltip"));
  } else {
    head = statsBadge(finalLeg?.delayStats, true, window);
    if (tts.length) tight = badge("yellow", t("tightBadge"), t("tightBadgeTooltip", tts.map((tt) => tt.station).join(", ")));
  }
  const top = document.createElement("div");
  top.className = "trip-check-verdict";
  top.title = t("asShown");
  // next to a tight-transfer warning the delay badge is only worth the space when red
  if (tight) top.appendChild(tight);
  if (!tight || head.classList.contains("red")) top.appendChild(head);
  panel.appendChild(top);

  const legsEl = document.createElement("div");
  legsEl.className = "legs";
  const warnByLeg = new Map(tts.map((tt) => [tt.legIndex, tt]));
  let canceledTotal = 0;
  legs.forEach((leg, i) => {
    const row = planLegRow(leg, window);
    if (i === 0) row.classList.add("rail-first");
    if (i === legs.length - 1) row.classList.add("rail-last");
    legsEl.appendChild(row);
    canceledTotal += leg.delayStats?.canceledDays || 0;
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
  panel.appendChild(legsEl);
  if (canceledTotal > 0) {
    const note = document.createElement("div");
    note.className = "cancel-note";
    note.textContent = t("cancelNote", window, canceledTotal);
    panel.appendChild(note);
  }
  return panel;
}

// the same fold as the past trips' check, only nothing has to be fetched
function togglePanel(panel, button) {
  const hidden = panel.classList.toggle("hidden");
  button.setAttribute("aria-expanded", String(!hidden));
}

// the card's check panel, fetched on the first call; null when the fetch failed
async function ensureCheck(trip, card) {
  const existing = card.querySelector(".trip-check");
  if (existing) return existing;
  card.querySelector(".trip-check-status")?.remove();
  const buttons = card.querySelectorAll(".trip-actions button");
  buttons.forEach((b) => { b.disabled = true; });
  const status = document.createElement("div");
  status.className = "trip-check-status";
  status.textContent = t("checkBusy");
  card.appendChild(status);
  try {
    const data = await api(`/api/trips/${trip.id}/check`);
    status.remove();
    const panel = buildCheck(data);
    card.appendChild(panel);
    track("trip-check");
    return panel;
  } catch (e) {
    status.classList.add("error");
    status.textContent = t(e.status === 429 ? "checkThrottle" : "loadError");
    return null;
  } finally {
    buttons.forEach((b) => { b.disabled = false; });
  }
}

// the three buttons follow the panel: all expanded with it, the pick mode's own lit
function syncActions(card, panel) {
  const hidden = panel.classList.contains("hidden");
  card.querySelectorAll(".trip-actions button").forEach((b) => {
    b.setAttribute("aria-expanded", String(!hidden));
    b.classList.toggle("active", !hidden && !!b.dataset.pick && b.dataset.pick === panel.dataset.pick);
  });
}

// first press fetches and shows the check, later ones fold it away and back
async function toggleCheck(trip, card) {
  const existing = card.querySelector(".trip-check");
  const panel = existing || await ensureCheck(trip, card);
  if (!panel) return;
  if (existing) panel.classList.toggle("hidden");
  delete panel.dataset.pick;
  closeProblems(panel);
  syncActions(card, panel);
}

/* The same panel with a hand-off link on every train of the trip: the
   visitor picks the one the report or the story is about. The other mode's
   button switches the links over; the same button again folds the panel. */
async function togglePick(trip, card, kind) {
  const existing = card.querySelector(".trip-check");
  const panel = existing || await ensureCheck(trip, card);
  if (!panel) return;
  const again = !panel.classList.contains("hidden") && panel.dataset.pick === kind;
  panel.classList.toggle("hidden", again);
  closeProblems(panel);
  if (again) {
    delete panel.dataset.pick;
  } else {
    panel.dataset.pick = kind;
    panel.querySelector(".pick-hint").textContent = t(kind === "report" ? "pickHintReport" : "pickHintStory");
    panel.querySelectorAll(".leg-pick").forEach((a) => {
      a.href = a.dataset[kind];
      a.title = t(kind === "report" ? "pickReport" : "pickStory");
      a.setAttribute("aria-label", a.title);
    });
    track("trip-pick", { kind });
  }
  syncActions(card, panel);
}

function tripCard(trip, past) {
  const card = document.createElement("article");
  card.className = "trip" + (past ? " past" : "");
  card.dataset.id = trip.id;

  const head = document.createElement("div");
  head.className = "trip-head";
  const day = document.createElement("span");
  day.className = "trip-day";
  day.textContent = fmtDay(trip.departure);
  head.appendChild(day);
  if (trip.kind === "outbound" || trip.kind === "return") {
    const kind = document.createElement("span");
    kind.className = "trip-kind";
    kind.textContent = t(trip.kind === "outbound" ? "kindOutbound" : "kindReturn");
    head.appendChild(kind);
  }
  const spacer = document.createElement("span");
  spacer.className = "spacer";
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "trip-remove";
  remove.textContent = "✕";
  remove.title = t("removeTitle");
  remove.setAttribute("aria-label", t("removeTitle"));
  remove.addEventListener("click", () => removeTrip(trip, card, remove));
  head.append(spacer, remove);

  const row = document.createElement("div");
  row.className = "trip-row";
  const times = document.createElement("span");
  times.className = "trip-times";
  times.textContent = `${fmtTime(trip.departure)} – ${fmtTime(trip.arrival)}`;
  const route = document.createElement("span");
  route.className = "trip-route";
  route.textContent = `${trip.fromName} → ${trip.toName}`;
  row.append(times);
  // a past trip shows the day's delay at the destination, coloured like the
  // search's badges; a cancellation the check could not route around has no number
  if (past && (trip.delay != null || trip.canceled)) {
    const badge = document.createElement("span");
    badge.className = "badge trip-delay "
      + (trip.delay == null ? "gray" : trip.delay < 3 ? "green" : trip.delay < 10 ? "yellow" : "red");
    badge.textContent = trip.delay == null ? t("canceled") : `${trip.delay >= 0 ? "+" : ""}${trip.delay} min`;
    badge.title = t("delayTitle");
    row.append(badge);
  }
  row.append(route);

  const trains = (trip.legs || []).filter((l) => !l.walking);
  const meta = document.createElement("div");
  meta.className = "trip-meta";
  // the trains themselves are behind the button, in the leg rows
  meta.textContent = [
    fmtDuration(trip.departure, trip.arrival),
    trains.length ? t("changes", trains.length - 1) : "",
  ].filter(Boolean).join(" · ");

  const actions = document.createElement("div");
  actions.className = "trip-actions";
  if (past) {
    const check = document.createElement("button");
    check.type = "button";
    check.className = "book-btn";
    check.textContent = t("check");
    check.setAttribute("aria-expanded", "false");
    check.addEventListener("click", () => toggleCheck(trip, card));
    actions.appendChild(check);
    // the same panel, with a leg to pick for the stories page's two forms
    for (const [kind, key] of [["report", "reportBtn"], ["story", "storyBtn"]]) {
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "book-btn trip-link";
      pick.dataset.pick = kind;
      pick.textContent = t(key);
      pick.setAttribute("aria-expanded", "false");
      pick.addEventListener("click", () => togglePick(trip, card, kind));
      actions.appendChild(pick);
    }
  } else {
    // the search's card, behind a button like the past trips' check
    const panel = buildPlan(trip);
    const show = document.createElement("button");
    show.type = "button";
    show.className = "book-btn";
    show.textContent = t("planBtn");
    show.setAttribute("aria-expanded", "false");
    show.addEventListener("click", () => { togglePanel(panel, show); track("trip-plan"); });
    actions.appendChild(show);
    card.append(head, row, meta, actions, panel);
    return card;
  }

  card.append(head, row, meta, actions);
  return card;
}

function syncEmpty() {
  // a fresh account gets the pitch card instead of two empty section headings
  const none = $("trips-next").children.length === 0 && $("trips-past").children.length === 0;
  $("trips-onboard").classList.toggle("hidden", !none);
  $("trips-next-section").classList.toggle("hidden", none);
  $("trips-past-section").classList.toggle("hidden", none);
  $("trips-next-empty").classList.toggle("hidden", $("trips-next").children.length > 0);
  $("trips-past-empty").classList.toggle("hidden", $("trips-past").children.length > 0);
}

function render(data) {
  current = data;
  renderStats();
  const next = $("trips-next"), past = $("trips-past");
  next.textContent = "";
  past.textContent = "";
  // the server's Berlin clock, in the stamps' own form: a string compare sorts them
  for (const trip of data.trips) {
    const isPast = trip.arrival <= data.now;
    (isPast ? past : next).appendChild(tripCard(trip, isPast));
  }
  // past trips read newest first; upcoming ones soonest first, as they came
  past.append(...Array.from(past.children).reverse());
  $("trips-next-section").classList.remove("hidden");
  $("trips-past-section").classList.remove("hidden");
  syncEmpty();
}

async function api(path, opts = {}) {
  const token = await me.user.getIdToken();
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

async function removeTrip(trip, card, button) {
  button.disabled = true;
  try {
    await api("/api/trips/" + trip.id, { method: "DELETE" });
    card.remove();
    syncEmpty();
    current.trips = current.trips.filter((tr) => tr.id !== trip.id);
    renderStats();
    track("trip-remove");
  } catch (e) {
    button.disabled = false;
    setStatus("loadError", true);
  }
}

async function load() {
  setStatus("loading");
  try {
    render(await api("/api/trips"));
    setStatus("");
  } catch (e) {
    // signed out after all, or a step short: the login page knows which
    if (e.status === 401 || e.status === 403) { showLogin(); return; }
    setStatus("loadError", true);
  }
}

function showLogin() {
  setStatus("");
  $("trips-login").classList.remove("hidden");
}

async function init() {
  applyStatic();
  try {
    fb = await import("/firebase.js?v=1");
    if (fb.auth) {
      await fb.auth.authStateReady();
      const user = fb.auth.currentUser;
      if (user) {
        const who = await fb.identity(user);
        if (who.verified) me = { user, name: who.name || "" };
      }
    }
    if (!me) fb.remember(false);
  } catch (e) {
    me = null;
  }
  renderAuth();
  if (me) await load(); else showLogin();
}

$("auth-logout").addEventListener("click", async () => {
  try {
    await fb.signOut(fb.auth);
  } catch (e) { /* the SDK may still hold the user; the reload sorts it out */ }
  fb.remember(false);
  location.reload();
});

init();

})();
