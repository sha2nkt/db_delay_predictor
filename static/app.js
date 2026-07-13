const state = {
  from: null,   // {id, name}
  to: null,
  journeys: [],
  sort: "departure",
  windowUsed: 7,  // averaging window that produced the current results
  departure: null,  // departure ISO of the current search (reused for paging)
  earlierRef: null,  // paging tokens from the API
  laterRef: null,
};

// --- autocomplete ---

function setupAutocomplete(inputId, dropdownId, key) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  let timer = null;

  input.addEventListener("input", () => {
    state[key] = null;
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { dropdown.classList.remove("open"); return; }
    timer = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/locations?query=${encodeURIComponent(q)}`);
        if (!resp.ok) return;
        const items = await resp.json();
        dropdown.innerHTML = "";
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

async function fetchJourneys(pagingRef) {
  const win = document.getElementById("window").value;
  const params = new URLSearchParams({
    from: state.from.id, to: state.to.id, departure: state.departure, window: win,
  });
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

async function search() {
  if (!state.from || !state.to) {
    statusEl.textContent = "Bitte Start und Ziel aus der Vorschlagsliste wählen.";
    statusEl.classList.add("error");
    return;
  }
  state.departure = `${document.getElementById("date").value}T${document.getElementById("time").value}:00`;
  statusEl.classList.remove("error");
  statusEl.textContent = "Suche Verbindungen…";
  resultsEl.innerHTML = "";
  controlsEl.classList.add("hidden");
  earlierBtn.classList.add("hidden");
  laterBtn.classList.add("hidden");
  document.getElementById("hero-chart").classList.add("hidden");
  searchBtn.disabled = true;

  try {
    const data = await fetchJourneys(null);
    state.journeys = data.journeys || [];
    state.earlierRef = data.earlierRef || null;
    state.laterRef = data.laterRef || null;
    statusEl.textContent = state.journeys.length ? "" : "Keine Verbindungen gefunden.";
    controlsEl.classList.toggle("hidden", state.journeys.length === 0);
    updatePageButtons();
    render();
  } catch (e) {
    statusEl.textContent = `Fehler: ${e.message}`;
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
    statusEl.textContent = `Fehler: ${e.message}`;
    statusEl.classList.add("error");
  } finally {
    btn.disabled = false;
  }
}

// --- chart language toggle ---

const chartVariants = {
  de: {
    src: "delay-correlation.svg",
    alt: "Verspätete Züge bleiben verspätet: Züge, die vom 1.–15. Juni verspätet waren, waren es auch vom 16.–30. Juni.",
  },
  en: {
    src: "delay-correlation-en.svg",
    alt: "Delayed trains stay delayed: trains that ran late June 1–15 also ran late June 16–30.",
  },
};

document.querySelectorAll(".lang-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".lang-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const variant = chartVariants[btn.dataset.lang];
    const img = document.getElementById("chart-img");
    img.src = variant.src;
    img.alt = variant.alt;
  });
});

// --- sorting ---

document.querySelectorAll(".sort-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".sort-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.sort = btn.dataset.sort;
    render();
  });
});

function sortedJourneys() {
  const js = [...state.journeys];
  if (state.sort === "delay") {
    js.sort((a, b) => {
      const aMissing = a.delayScore == null, bMissing = b.delayScore == null;
      if (aMissing !== bMissing) return aMissing ? 1 : -1;  // missing data last
      if (aMissing && bMissing) return 0;
      if (a.delayScore !== b.delayScore) return a.delayScore - b.delayScore;
      return (a.maxLegAvgDelay ?? 0) - (b.maxLegAvgDelay ?? 0);
    });
  } else if (state.sort === "price") {
    js.sort((a, b) => {
      const aMissing = a.price == null, bMissing = b.price == null;
      if (aMissing !== bMissing) return aMissing ? 1 : -1;  // no price last
      if (aMissing && bMissing) return 0;
      return a.price - b.price;  // stable sort keeps departure order on ties
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

function delayBadge(stats, big) {
  const span = document.createElement("span");
  span.className = "badge";
  if (!stats || stats.avgDelay == null) {
    span.classList.add("gray");
    span.textContent = "keine Daten";
    return span;
  }
  const v = stats.avgDelay;
  span.classList.add(v < 3 ? "green" : v < 10 ? "yellow" : "red");
  span.innerHTML = `Ø +${v} min${big ? ` <small>(${stats.daysMatched}/${state.windowUsed} Tage)</small>` : ""}`;
  span.title = `Durchschnittliche Ankunftsverspätung der letzten ${state.windowUsed} Tage (max. +${stats.maxDelay} min)`;
  return span;
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

    const times = document.createElement("span");
    times.className = "journey-times";
    times.textContent = `${fmtTime(first.plannedDeparture)} → ${fmtTime(last.plannedArrival)}`;

    const meta = document.createElement("span");
    meta.className = "journey-meta";
    meta.textContent = `${fmtDuration(journey.durationSeconds)} · ` +
      (transfers === 0 ? "direkt" : `${transfers} Umstieg${transfers > 1 ? "e" : ""}`);

    const spacer = document.createElement("span");
    spacer.className = "spacer";

    const finalStats = trainLegs.length ? trainLegs[trainLegs.length - 1].delayStats : null;
    const badge = delayBadge(finalStats, true);

    const price = document.createElement("span");
    price.className = "price";
    if (journey.price != null) {
      price.textContent = `ab ${journey.price.toFixed(2).replace(".", ",")} €`;
    } else {
      price.classList.add("price-na");
      price.textContent = "Preis auf bahn.de";
    }

    const book = document.createElement("a");
    book.className = "book-btn";
    book.textContent = "Auf bahn.de buchen";
    book.href = bahnDeUrl(journey);
    book.target = "_blank";
    book.rel = "noopener";

    head.append(times, meta, spacer, badge, price, book);
    card.appendChild(head);

    const legsEl = document.createElement("div");
    legsEl.className = "legs";
    let canceledTotal = 0;
    for (const leg of legs) {
      const row = document.createElement("div");
      row.className = "leg";
      if (leg.walking) {
        const w = document.createElement("span");
        w.className = "walk";
        w.textContent = `Fußweg · ${leg.origin?.name || ""} → ${leg.destination?.name || ""}`;
        row.appendChild(w);
      } else {
        const train = document.createElement("span");
        train.className = "train";
        train.textContent = leg.line?.name || "Zug";
        const desc = document.createElement("span");
        desc.textContent = `${leg.origin?.name || ""} ${fmtTime(leg.plannedDeparture || leg.departure)} → ` +
          `${leg.destination?.name || ""} ${fmtTime(leg.plannedArrival || leg.arrival)}`;
        row.append(train, desc, delayBadge(leg.delayStats, false));
        if (leg.delayStats?.canceledDays) canceledTotal += leg.delayStats.canceledDays;
      }
      legsEl.appendChild(row);
    }
    card.appendChild(legsEl);

    if (canceledTotal > 0) {
      const note = document.createElement("div");
      note.className = "cancel-note";
      note.textContent = `⚠ In den letzten ${state.windowUsed} Tagen ${canceledTotal}× (teil-)ausgefallen`;
      card.appendChild(note);
    }

    resultsEl.appendChild(card);
  }
}
