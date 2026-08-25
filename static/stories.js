"use strict";
/* Delay Stories: a small HN-style board. All user content is rendered via
   textContent — nothing user-written ever reaches innerHTML. */
(function () {

const I18N = {
  de: {
    docTitle: "Delay Geschichten – DelayBahn",
    logoSrc: "/logo_delay_stories_wide_german_transparent.png",
    logoAlt: "Delay Geschichten",
    tagline: "Gestrandet, verspätet, überlebt – erzähl’s hier",
    topHeading: "Top-Geschichten",
    listHeading: "Geschichten",
    sortNew: "Neueste",
    sortLiked: "Beliebteste",
    sortCommented: "Meistkommentiert",
    feedbackAsk: "Wie findest du Delay Geschichten?",
    feedbackYes: "Gut",
    feedbackNo: "Nicht gut",
    feedbackDismiss: "Ausblenden",
    feedbackFollowUp: "Danke! Was können wir besser machen?",
    feedbackPlaceholder: "Was gefällt dir, was fehlt oder stört? Je konkreter, desto besser.",
    feedbackSend: "Senden",
    feedbackThanks: "Danke für dein Feedback.",
    composeToggle: "+ Geschichte erzählen",
    fromLabel: "Von",
    fromPlaceholder: "z.B. Hannover Hbf",
    toLabel: "Nach (optional)",
    toPlaceholder: "z.B. Berlin Hbf",
    dateLabel: "Datum",
    timeLabel: "Uhrzeit",
    trainLabel: "Zug (optional)",
    trainPlaceholder: "z.B. ICE 574",
    problemsLabel: "Was war los? (Mehrfachauswahl)",
    problem_delay: "Verspätung",
    problem_cancelled: "Zugausfall",
    problem_missed: "Anschluss verpasst",
    problem_ac: "Klimaanlage defekt",
    problem_wc: "WC defekt/schmutzig",
    problem_crowding: "Überfüllt",
    problem_wifi: "WLAN geht nicht",
    problem_other: "Sonstiges",
    otherPlaceholder: "Was war sonst los?",
    titleLabel: "Titel",
    titlePlaceholder: "z.B. Der ICE nach München endete in Fulda – wie wir alle",
    textLabel: "Deine Geschichte",
    textPlaceholder: "Wie spät, wie voll, wie absurd? Bonuspunkte für die Durchsage im Wortlaut – und für die Miene der Person gegenüber.",
    composeNote: "Beiträge erscheinen öffentlich unter deinem Benutzernamen. Halt es unterhaltsam: Wut wird witzig, wenn sie gut erzählt ist. Beleidigungen und persönliche Angriffe helfen niemandem und werden entfernt.",
    sharePromise: "🏆 Jede Woche teilen wir die Geschichte mit den meisten Stimmen auf unseren Kanälen – und markieren die Deutsche Bahn dabei. Also: abstimmen und mitschreiben.",
    boardHeading: "Störungsbilanz",
    boardIntro: "Jede Geschichte hier zählt oben mit. Heute auch was davon erlebt, aber keine Lust zu schreiben? Kachel antippen, Strecke und Zeit angeben, der Zähler geht eins hoch.",
    tapHint: "Heute auch passiert? Antippen und mitzählen (Anmeldung nötig).",
    tapHintDone: "Gezählt. Nochmal antippen, um es zurückzunehmen.",
    tapped: "heute von dir gemeldet",
    tapTitle: "{n} – wo und wann?",
    tapSend: "Mitzählen",
    otherLabel: "Was war sonst los?",
    tapOtherPlaceholder: "z.B. Tür klemmte, Durchsage nur auf Klingonisch",
    spanWeek: "Woche",
    spanMonth: "Monat",
    spanYear: "Jahr",
    spanAll: "Gesamt",
    spanGroup: "Zeitraum",
    navLogin: "Anmelden",
    navLogout: "Abmelden",
    composeSend: "Veröffentlichen",
    sending: "Wird gesendet …",
    footerBack: "← Zur Verbindungssuche",
    footerLegal: "Impressum & Datenschutz",
    footerContact: "Kontakt",
    footerUgc: "Beiträge stammen von Nutzerinnen und Nutzern und geben nicht die Meinung von DelayBahn wieder. Beiträge mit Beleidigungen oder personenbezogenen Daten werden entfernt.",
    loadMore: "Mehr laden",
    anon: "anonym",
    edit: "Bearbeiten",
    del: "Löschen",
    save: "Speichern",
    cancel: "Abbrechen",
    edited: "bearbeitet",
    removed: "[entfernt]",
    moreTitle: "Mehr",
    confirmDeleteStory: "Diese Geschichte wirklich löschen?",
    confirmDeleteComment: "Diesen Kommentar wirklich löschen?",
    errEdit: "Konnte nicht gespeichert werden.",
    errDelete: "Konnte nicht gelöscht werden.",
    upvoteTitle: "Gefällt mir",
    downvoteTitle: "Gefällt mir nicht",
    share: "Teilen",
    copyLink: "Link kopieren",
    embed: "Einbetten",
    linkCopied: "Link kopiert",
    copy: "Kopieren",
    copied: "Kopiert",
    embedLead: "Diesen Code in deine Seite einfügen – die Geschichte erscheint dort als Karte:",
    copyBlocked: "Kopieren ist hier blockiert – Text markieren und selbst kopieren.",
    permalinkHead: "Geteilte Geschichte",
    permalinkGone: "Diese Geschichte gibt es nicht mehr.",
    embedRead: "Auf DelayBahn lesen",
    comments0: "Kommentieren",
    comments1: "1 Kommentar",
    commentsN: "{n} Kommentare",
    commentPlaceholder: "Dein Kommentar …",
    commentSend: "Senden",
    reply: "Antworten",
    readMore: "Mehr lesen",
    readLess: "Weniger anzeigen",
    empty: "Noch keine Geschichten. Erzähl die erste!",
    errLoad: "Konnte nicht geladen werden – bitte später erneut versuchen.",
    errSubmit: "Senden fehlgeschlagen – bitte später erneut versuchen.",
    errRate: "Zu viele Beiträge – bitte warte kurz und versuch es erneut.",
    justNow: "gerade eben",
    minAgo: "vor {n} min",
    hourAgo: "vor {n} Std.",
    dayAgo1: "vor 1 Tag",
    dayAgo: "vor {n} Tagen",
  },
  en: {
    docTitle: "Delay Stories – DelayBahn",
    logoSrc: "/logo_delay_stories_wide_transparent.png",
    logoAlt: "Delay Stories",
    tagline: "Stranded, delayed, survived – tell it here",
    topHeading: "Top stories",
    listHeading: "Stories",
    sortNew: "Newest",
    sortLiked: "Most liked",
    sortCommented: "Most commented",
    feedbackAsk: "How do you like Delay Stories?",
    feedbackYes: "Good",
    feedbackNo: "Not good",
    feedbackDismiss: "Dismiss",
    feedbackFollowUp: "Thanks! What could be better?",
    feedbackPlaceholder: "What do you like, what is missing or in the way? The more specific, the better.",
    feedbackSend: "Send",
    feedbackThanks: "Thanks for your feedback.",
    composeToggle: "+ Tell your story",
    fromLabel: "From",
    fromPlaceholder: "e.g. Hannover Hbf",
    toLabel: "To (optional)",
    toPlaceholder: "e.g. Berlin Hbf",
    dateLabel: "Date",
    timeLabel: "Time",
    trainLabel: "Train (optional)",
    trainPlaceholder: "e.g. ICE 574",
    problemsLabel: "What went wrong? (pick any)",
    problem_delay: "Delayed",
    problem_cancelled: "Cancelled",
    problem_missed: "Missed connection",
    problem_ac: "AC not working",
    problem_wc: "WC broken/dirty",
    problem_crowding: "Overcrowded",
    problem_wifi: "Wi-Fi not working",
    problem_other: "Other",
    otherPlaceholder: "What else went wrong?",
    titleLabel: "Title",
    titlePlaceholder: "e.g. The ICE to Munich terminated in Fulda – so did we",
    textLabel: "Your story",
    textPlaceholder: "How late, how packed, how absurd? Bonus points for quoting the announcement word for word – and for the face of the person opposite.",
    composeNote: "Posts appear publicly under your username. Keep it entertaining: anger is funny when it's told well. Insults and personal attacks help nobody and get removed.",
    sharePromise: "🏆 Every week we post the most upvoted story on our channels – and tag Deutsche Bahn in it. So vote, and keep them coming.",
    boardHeading: "Damage report",
    boardIntro: "Every story on this page feeds the board. Had one of these today but don't feel like writing? Tap the tile, say where and when, and the counter goes up one.",
    tapHint: "Happened to you today? Tap to count it (login needed).",
    tapHintDone: "Counted. Tap again to take it back.",
    tapped: "reported by you today",
    tapTitle: "{n} – where and when?",
    tapSend: "Count it",
    otherLabel: "What else went wrong?",
    tapOtherPlaceholder: "e.g. doors stuck, announcement in Klingon only",
    spanWeek: "Week",
    spanMonth: "Month",
    spanYear: "Year",
    spanAll: "All time",
    spanGroup: "Time span",
    navLogin: "Login",
    navLogout: "Logout",
    composeSend: "Publish",
    sending: "Sending …",
    footerBack: "← Back to the journey search",
    footerLegal: "Legal notice & privacy",
    footerContact: "Contact",
    footerUgc: "Posts are user-submitted and do not reflect the views of DelayBahn. Posts containing insults or personal data will be removed.",
    loadMore: "Load more",
    anon: "anonymous",
    edit: "Edit",
    del: "Delete",
    save: "Save",
    cancel: "Cancel",
    edited: "edited",
    removed: "[removed]",
    moreTitle: "More",
    confirmDeleteStory: "Delete this story for good?",
    confirmDeleteComment: "Delete this comment for good?",
    errEdit: "Could not save that.",
    errDelete: "Could not delete that.",
    upvoteTitle: "Upvote",
    downvoteTitle: "Downvote",
    share: "Share",
    copyLink: "Copy link",
    embed: "Embed",
    linkCopied: "Link copied",
    copy: "Copy",
    copied: "Copied",
    embedLead: "Paste this code into your page – the story shows up there as a card:",
    copyBlocked: "Copying is blocked here – select the text and copy it yourself.",
    permalinkHead: "Shared story",
    permalinkGone: "That story is gone.",
    embedRead: "Read on DelayBahn",
    comments0: "Comment",
    comments1: "1 comment",
    commentsN: "{n} comments",
    commentPlaceholder: "Your comment …",
    commentSend: "Send",
    reply: "Reply",
    readMore: "Read more",
    readLess: "Show less",
    empty: "No stories yet. Tell the first one!",
    errLoad: "Could not load – please try again later.",
    errSubmit: "Sending failed – please try again later.",
    errRate: "Too many posts – please wait a moment and try again.",
    justNow: "just now",
    minAgo: "{n} min ago",
    hourAgo: "{n} h ago",
    dayAgo1: "1 day ago",
    dayAgo: "{n} days ago",
  },
};

const PAGE = 10;
const TOP_N = 5;
const CLAMP = 600;

// the URL is the language: /stories is English, /geschichten German. The
// login and verify pages have no language of their own and read it from here.
const lang = location.pathname.startsWith("/stories") ? "en" : "de";
try { localStorage.setItem("lang", lang); } catch (e) {}
const t = (key) => (I18N[lang][key] != null ? I18N[lang][key] : I18N.de[key]);
const fmt = (key, n) => t(key).replace("{n}", n);
// no-op when the Umami script is blocked or unavailable. Events carry
// categories and outcomes only - never story text, names or addresses.
const track = (name, data) => window.umami?.track(name, data);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

// a masked line icon (style.css .ico-*), in the color of the text around it
const icon = (name) => el("span", "ico ico-" + name);

/* -- identity: the Firebase account this browser holds, once it is complete
   (signed in, contact proven, username claimed). `me.name` attributes rows;
   `me.user` mints the bearer token every writing request carries. The SDK
   is loaded only when the login page left the "account" hint behind, so
   the anonymous majority never downloads it. -- */
let me = null;
let fb = null;

function toLogin() {
  track("login-prompt");  // how often the board sends someone off to sign in
  location.assign("/login");
}

function renderAuth() {
  document.getElementById("auth-login").classList.toggle("hidden", !!me);
  document.getElementById("auth-user").classList.toggle("hidden", !me);
  document.getElementById("auth-name").textContent = me ? me.name : "";
}

/* -- API: every request from a signed-in visitor carries their token, so the
   lists come back with their own votes marked and the writes are theirs -- */
async function api(path, opts = {}) {
  if (me) {
    // cached until shortly before expiry; the SDK refreshes it on its own
    const token = await me.user.getIdToken();
    opts.headers = { ...(opts.headers || {}), "Authorization": "Bearer " + token };
  }
  const resp = await fetch(path, opts);
  if (!resp.ok) {
    const err = new Error("http " + resp.status);
    err.status = resp.status;
    throw err;
  }
  return resp.status === 204 ? null : resp.json();
}
const postJSON = (path, body) => api(path, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

function timeAgo(ts) {
  const secs = (Date.now() - Date.parse(ts)) / 1000;
  if (!isFinite(secs) || secs < 60) return t("justNow");
  const mins = Math.floor(secs / 60);
  if (mins < 60) return fmt("minAgo", mins);
  const hours = Math.floor(mins / 60);
  if (hours < 24) return fmt("hourAgo", hours);
  const days = Math.floor(hours / 24);
  return days === 1 ? t("dayAgo1") : fmt("dayAgo", days);
}

/* -- voting, Reddit-style: 1/-1/0; the same arrow again clears, the
   opposite arrow switches, the count everywhere is ups minus downs -- */
function voteEvent(kind, dir) {
  return dir === 1 ? kind + "-vote" : dir === -1 ? kind + "-downvote" : kind + "-unvote";
}

async function castVote(item, kind, dir) {
  if (!me) { toLogin(); return; }
  const target = item.voted === dir ? 0 : dir;
  const path = kind === "story" ? "/api/stories/" + item.id + "/vote"
                                : "/api/comments/" + item.id + "/vote";
  try {
    const res = await postJSON(path, { vote: target });
    item.score = res.score;
    item.voted = res.voted;
    track(voteEvent(kind, res.voted));
  } catch (e) {
    // login lapsed, or the account lost a step (403): the login page knows which
    if (e.status === 401 || e.status === 403) { toLogin(); return; }
    /* otherwise leave the arrows as they were */
  }
  updateVoteEls(item, kind);
}

// a story can be on screen twice (top strip + newest list), and its votes in
// two places per card (side column + action bar); update every copy
function updateVoteEls(item, kind) {
  const sel = '[data-vote-kind="' + kind + '"][data-vote-id="' + item.id + '"]';
  document.querySelectorAll(sel).forEach((col) => {
    const count = col.querySelector(".vote-count");
    if (count) count.textContent = item.score;
    col.querySelector(".vote-up").classList.toggle("voted", item.voted === 1);
    const down = col.querySelector(".vote-down");
    if (down) down.classList.toggle("voted-down", item.voted === -1);
  });
  if (kind !== "story") return;
  document.querySelectorAll('.top-score[data-story-id="' + item.id + '"]').forEach((s) => {
    s.querySelector(".top-num").textContent = item.score;
  });
}

/* -- vote column and action bar --------------------------------------------

   The arrow keeps its own column down the left of a story and of every
   comment; reply/comments and the overflow menu sit in a bar underneath.
   Upvote only, no downvote: this is a place to say "that happened to me too",
   and a burial button would just turn it into a place to argue. The menu is
   built only for the account that wrote the row, so Edit and Delete never
   appear as something to be refused - the server checks again anyway, because
   a hidden button is not a permission. */

const mine = (item) => !!(me && item.author && item.author === me.name);

// one open menu at a time, closed by the next click anywhere
let openMenu = null;
function closeMenu() {
  if (openMenu) { openMenu.classList.remove("open"); openMenu = null; }
}
document.addEventListener("click", closeMenu);

function attachMenuToggle(wrap, btn) {
  btn.addEventListener("click", (ev) => {
    ev.stopPropagation();  // the document listener would close it again
    const wasOpen = wrap.classList.contains("open");
    closeMenu();
    if (!wasOpen) { wrap.classList.add("open"); openMenu = wrap; }
  });
}

function overflowMenu(onEdit, onDelete) {
  const wrap = el("div", "menu");
  const btn = el("button", "menu-btn", "···");
  btn.type = "button";
  btn.title = t("moreTitle");
  btn.setAttribute("aria-label", t("moreTitle"));
  const list = el("div", "menu-list");
  const edit = el("button", "menu-item", t("edit"));
  edit.type = "button";
  const del = el("button", "menu-item danger", t("del"));
  del.type = "button";
  list.append(edit, del);
  attachMenuToggle(wrap, btn);
  edit.addEventListener("click", () => { closeMenu(); onEdit(); });
  del.addEventListener("click", () => { closeMenu(); onDelete(); });
  wrap.append(btn, list);
  return wrap;
}

function arrowBtn(item, kind, dir) {
  const cls = dir === 1 ? "vote-btn vote-up" + (item.voted === 1 ? " voted" : "")
                        : "vote-btn vote-down" + (item.voted === -1 ? " voted-down" : "");
  const btn = el("button", cls);
  btn.append(icon(dir === 1 ? "up" : "down"));
  btn.type = "button";
  const label = t(dir === 1 ? "upvoteTitle" : "downvoteTitle");
  btn.title = label;
  btn.setAttribute("aria-label", label);
  btn.addEventListener("click", () => castVote(item, kind, dir));
  return btn;
}

// the side column: the up arrow with the net count under it. A comment has no
// action bar, so its column carries the down arrow too, old-Reddit style; a
// story's down arrow lives in the bar under the text instead.
function voteColumn(item, kind) {
  const col = el("div", "vote-col");
  col.dataset.voteKind = kind;
  col.dataset.voteId = item.id;
  col.append(arrowBtn(item, kind, 1), el("span", "vote-count", item.score));
  if (kind === "comment") col.append(arrowBtn(item, kind, -1));
  return col;
}

// the up/down pair in a story's action bar, next to "comment"
function voteBar(story) {
  const bar = el("span", "vote-inline");
  bar.dataset.voteKind = "story";
  bar.dataset.voteId = story.id;
  bar.append(arrowBtn(story, "story", 1), arrowBtn(story, "story", -1));
  return bar;
}

/* -- sharing: the permalink to copy, or an iframe snippet for other sites -- */
const STORY_PATH = lang === "en" ? "/stories/" : "/geschichten/";
const storyUrl = (story) => location.origin + STORY_PATH + story.id;

function embedCode(story) {
  const title = (t("logoAlt") + ": " + story.title).replace(/"/g, "&quot;");
  return '<iframe src="' + location.origin + "/embed" + STORY_PATH + story.id + '"'
    + ' width="100%" height="360" style="border:0;max-width:640px" loading="lazy"'
    + ' title="' + title + '"></iframe>';
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    return false;  // no clipboard API (plain http, old browser) or permission denied
  }
}

// after a copy the button says so for a moment, then goes back to its label
function flash(labelEl, text, back) {
  labelEl.textContent = text;
  setTimeout(() => { labelEl.textContent = back; }, 1600);
}

// the panel under the action bar: the embed snippet, or the bare link when
// the clipboard is off limits, in a read-only box with its own copy button
function showSharePanel(panel, mode, lead, code) {
  panel.textContent = "";
  panel.dataset.mode = mode;
  panel.append(el("p", "share-lead", lead));
  const field = el("textarea", "share-code");
  field.readOnly = true;
  field.rows = mode === "embed" ? 3 : 1;
  field.value = code;
  field.addEventListener("focus", () => field.select());
  const copyBtn = el("button", "act share-copy", t("copy"));
  copyBtn.type = "button";
  copyBtn.addEventListener("click", async () => {
    field.select();
    flash(copyBtn, (await copyText(code)) ? t("copied") : t("copyBlocked"), t("copy"));
  });
  const row = el("div", "share-panel-row");
  row.append(copyBtn);
  panel.append(field, row);
  panel.classList.remove("hidden");
}

function shareMenu(story, panel) {
  const wrap = el("div", "menu share-menu");
  const btn = el("button", "act share-btn");
  btn.type = "button";
  const label = el("span", "share-label", t("share"));
  btn.append(icon("share"), label);
  const list = el("div", "menu-list");
  const link = el("button", "menu-item");
  link.type = "button";
  link.append(icon("link"), el("span", null, t("copyLink")));
  const embed = el("button", "menu-item");
  embed.type = "button";
  embed.append(icon("code"), el("span", null, t("embed")));
  list.append(link, embed);
  attachMenuToggle(wrap, btn);
  link.addEventListener("click", async () => {
    closeMenu();
    track("story-share-link");
    const url = storyUrl(story);
    if (await copyText(url)) {
      panel.classList.add("hidden");
      flash(label, t("linkCopied"), t("share"));
    } else {
      showSharePanel(panel, "link", t("copyBlocked"), url);
    }
  });
  embed.addEventListener("click", () => {
    closeMenu();
    if (panel.dataset.mode === "embed" && !panel.classList.contains("hidden")) {
      panel.classList.add("hidden");  // the same entry again folds it away
      return;
    }
    track("story-share-embed");
    showSharePanel(panel, "embed", t("embedLead"), embedCode(story));
  });
  wrap.append(btn, list);
  return wrap;
}

/* -- story cards -- */
function storyText(text) {
  const p = el("p", "story-text");
  if (text.length <= CLAMP + 100) { p.textContent = text; return p; }
  const short = text.slice(0, CLAMP) + " … ";
  p.textContent = short;
  const more = el("button", "read-more", t("readMore"));
  more.type = "button";
  let expanded = false;
  more.addEventListener("click", () => {
    expanded = !expanded;
    p.firstChild.textContent = expanded ? text + " " : short;
    more.textContent = expanded ? t("readLess") : t("readMore");
  });
  p.append(more);
  return p;
}

function commentsLabel(n) {
  return n === 0 ? t("comments0") : n === 1 ? t("comments1") : fmt("commentsN", n);
}

function storyCard(story) {
  const card = el("article", "story");
  card.dataset.storyId = story.id;
  const body = el("div", "story-body");

  // a removed story is kept only so the thread under it still hangs off
  // something; there is nothing left to show, vote on or act upon
  if (story.deleted) {
    body.append(el("p", "story-removed", t("removed")));
    const wrap = el("div", "story-comments hidden");
    const btn = el("button", "act comments-toggle", commentsLabel(story.comments));
    btn.type = "button";
    btn.addEventListener("click", () => toggleComments(story, btn, wrap));
    const bar = el("div", "story-actions");
    bar.append(btn);
    body.append(bar, wrap);
    card.append(body);
    return card;
  }

  const meta = el("div", "story-meta");
  meta.append(
    el("span", "story-station", "📍 " + legOf(story)),
    el("span", "meta-sep", "·"),
    el("span", null, story.author || t("anon")),
    el("span", "meta-sep", "·"),
    el("span", null, timeAgo(story.ts))
  );
  if (story.edited) {
    meta.append(el("span", "meta-sep", "·"), el("span", "edited-mark", t("edited")));
  }
  // when the journey was, as opposed to when the story was posted
  const departure = departureText(story);
  if (departure) {
    meta.append(el("span", "meta-sep", "·"), el("span", "story-departure", "🕑 " + departure));
  }
  if (story.train) {
    meta.append(el("span", "meta-sep", "·"), el("span", "story-train", "🚆 " + story.train));
  }

  const commentsWrap = el("div", "story-comments hidden");
  const commentsBtn = el("button", "act comments-toggle", commentsLabel(story.comments));
  commentsBtn.type = "button";
  commentsBtn.addEventListener("click", () => toggleComments(story, commentsBtn, commentsWrap));

  const share = el("div", "share-panel hidden");
  const bar = el("div", "story-actions");
  bar.append(voteBar(story), commentsBtn, shareMenu(story, share));
  if (mine(story)) {
    bar.append(overflowMenu(
      () => editStory(card, story),
      () => removeStory(story)));
  }

  // between the meta line and the text: what went wrong, before why it stung
  const tags = problemTags(story);
  const tagRow = el("div", "story-tags");
  tags.forEach((label) => tagRow.append(el("span", "story-tag", label)));
  body.append(el("h3", "story-title", story.title), meta);
  if (tags.length) body.append(tagRow);
  body.append(storyText(story.text), bar, share, commentsWrap);

  card.append(voteColumn(story, "story"), body);
  return card;
}

/* Editing happens in place: the card turns into the two fields the author is
   allowed to change and turns back when the server has taken them. */
function editStory(card, story) {
  const form = el("form", "edit-form");
  const title = el("input", "edit-title");
  title.type = "text";
  title.value = story.title;
  title.required = true;
  title.minLength = 3;
  title.maxLength = 120;
  const text = el("textarea", "edit-text");
  text.value = story.text;
  text.required = true;
  text.minLength = 10;
  text.maxLength = 5000;
  text.rows = 6;
  const save = el("button", "comment-send", t("save"));
  save.type = "submit";
  const cancel = el("button", "reply-btn", t("cancel"));
  cancel.type = "button";
  const status = el("p", "comment-status");
  const row = el("div", "comment-form-row");
  row.append(save, cancel, status);
  form.append(title, text, row);

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    save.disabled = true;
    try {
      const updated = await api("/api/stories/" + story.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ title: title.value.trim(), text: text.value.trim() }),
      });
      replaceStory(story.id, updated);
    } catch (e) {
      if (e.status === 401) { toLogin(); return; }
      status.textContent = t("errEdit");
      save.disabled = false;
    }
  });
  cancel.addEventListener("click", () => form.replaceWith(storyCard(story)));
  card.replaceWith(form);
  title.focus();
}

async function removeStory(story) {
  if (!window.confirm(t("confirmDeleteStory"))) return;
  try {
    await api("/api/stories/" + story.id, { method: "DELETE" });
  } catch (e) {
    if (e.status === 401) { toLogin(); return; }
    window.alert(t("errDelete"));
    return;
  }
  // a story with replies survives as a tombstone, one without is simply gone;
  // reloading both lists is the honest way to find out which happened
  resetNew();
  loadNew();
  loadTop();
}

// the same story can be on screen twice (top strip + newest list)
function replaceStory(id, updated) {
  const at = cacheNew.findIndex((s) => s.id === id);
  if (at >= 0) cacheNew[at] = updated;
  const top = cacheTop.findIndex((s) => s.id === id);
  if (top >= 0) { cacheTop[top] = updated; renderTop(); }
  rerenderNew();
}

/* -- comments -- */
async function toggleComments(story, btn, wrap) {
  if (!wrap.classList.contains("hidden")) { wrap.classList.add("hidden"); return; }
  wrap.classList.remove("hidden");
  if (wrap.dataset.loaded) return;
  wrap.textContent = "…";
  try {
    const list = await api("/api/stories/" + story.id + "/comments");
    wrap.dataset.loaded = "1";
    renderComments(story, wrap, btn, list);
  } catch (e) {
    wrap.textContent = t("errLoad"); // stays un-"loaded", so reopening retries
  }
}

function renderComments(story, wrap, btn, list) {
  story.comments = list.length;
  btn.textContent = commentsLabel(story.comments);
  wrap.textContent = "";
  const children = new Map();
  list.forEach((c) => {
    const key = c.parent_id || 0;
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(c);
  });
  const thread = el("div", "comment-thread");
  (function renderLevel(parentKey, container, depth) {
    (children.get(parentKey) || []).forEach((c) => {
      const node = el("div", "comment");
      // everything the arrow sits beside; replies and the reply box stay
      // siblings of it, so nesting indents by the thread rule alone
      const cbody = el("div", "comment-body");
      // past depth 5 replies stay possible but stop indenting, so a long
      // back-and-forth can't squeeze the text into a one-word column
      const kids = el("div", depth < 5 ? "comment-children" : "comment-children flat");

      // a removed comment keeps its place so its replies stay attached, and
      // loses everything else - name, text, score and every action
      if (c.deleted) {
        cbody.append(el("p", "comment-removed", t("removed")));
        node.append(cbody, kids);
        container.append(node);
        renderLevel(c.id, kids, depth + 1);
        return;
      }

      const meta = el("div", "comment-meta");
      meta.append(
        el("strong", null, c.author || t("anon")),
        el("span", "meta-sep", "·"),
        el("span", null, timeAgo(c.ts))
      );
      if (c.edited) {
        meta.append(el("span", "meta-sep", "·"), el("span", "edited-mark", t("edited")));
      }

      const replyBtn = el("button", "act", t("reply"));
      replyBtn.type = "button";
      replyBtn.addEventListener("click", () => {
        const open = node.querySelector(":scope > .comment-form");
        if (open) { open.remove(); return; }
        node.insertBefore(commentForm(story, wrap, btn, c.id), kids);
      });

      const body = el("p", "comment-text", c.text);
      const bar = el("div", "comment-actions");
      bar.append(replyBtn);
      if (mine(c)) {
        bar.append(overflowMenu(
          () => editComment(cbody, body, bar, c),
          () => removeComment(story, wrap, btn, c)));
      }
      cbody.append(meta, body, bar);
      node.append(voteColumn(c, "comment"), cbody, kids);
      container.append(node);
      renderLevel(c.id, kids, depth + 1);
    });
  })(0, thread, 0);
  wrap.append(thread, commentForm(story, wrap, btn, null));
}

/* In place, like the story: the text swaps for a box and swaps back. Only the
   text and the bar are hidden - the meta line stays, so it is still obvious
   whose comment is being rewritten. */
function editComment(cbody, body, bar, comment) {
  const form = el("form", "comment-form");
  const box = el("textarea", null, null);
  box.value = comment.text;
  box.required = true;
  box.maxLength = 2000;
  box.rows = 3;
  const save = el("button", "comment-send", t("save"));
  save.type = "submit";
  const cancel = el("button", "reply-btn", t("cancel"));
  cancel.type = "button";
  const status = el("p", "comment-status");
  const row = el("div", "comment-form-row");
  row.append(save, cancel, status);
  form.append(box, row);

  function restore() {
    form.remove();
    body.classList.remove("hidden");
    bar.classList.remove("hidden");
  }
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    save.disabled = true;
    try {
      const updated = await api("/api/comments/" + comment.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: box.value.trim() }),
      });
      comment.text = updated.text;
      comment.edited = updated.edited;
      body.textContent = updated.text;
      // the "edited" mark belongs in the meta line, which is still on screen
      const meta = cbody.querySelector(":scope > .comment-meta");
      if (meta && !meta.querySelector(".edited-mark")) {
        meta.append(el("span", "meta-sep", "·"), el("span", "edited-mark", t("edited")));
      }
      restore();
    } catch (e) {
      if (e.status === 401) { toLogin(); return; }
      status.textContent = t("errEdit");
      save.disabled = false;
    }
  });
  cancel.addEventListener("click", restore);
  body.classList.add("hidden");
  bar.classList.add("hidden");
  cbody.insertBefore(form, body.nextSibling);
  box.focus();
}

async function removeComment(story, wrap, btn, comment) {
  if (!window.confirm(t("confirmDeleteComment"))) return;
  try {
    await api("/api/comments/" + comment.id, { method: "DELETE" });
  } catch (e) {
    if (e.status === 401) { toLogin(); return; }
    window.alert(t("errDelete"));
    return;
  }
  // whether it vanished or left a tombstone depends on replies; re-fetching
  // the thread is what tells us, and it keeps the count honest
  try {
    renderComments(story, wrap, btn, await api("/api/stories/" + story.id + "/comments"));
  } catch (e) { /* the thread is stale but still readable */ }
}

function commentForm(story, wrap, btn, parentId) {
  const form = el("form", "comment-form");
  const text = document.createElement("textarea");
  text.rows = 3;
  text.maxLength = 2000;
  text.required = true;
  text.placeholder = t("commentPlaceholder");
  // writing needs an account; steer to login before typing, not after
  text.addEventListener("focus", () => { if (!me) toLogin(); });
  const send = el("button", "comment-send", t("commentSend"));
  send.type = "submit";
  const status = el("span", "comment-status");
  const row = el("div", "comment-form-row");
  row.append(send, status);
  form.append(text, row);
  document.getElementById("p-other").addEventListener("change", syncOther);
  setupStationPicker("c-from", "c-from-dropdown");
  setupStationPicker("c-to", "c-to-dropdown");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!me) { toLogin(); return; }
    const body = { text: text.value.trim() };
    if (!body.text) return;
    if (parentId) body.parent_id = parentId;
    send.disabled = true;
    status.textContent = "";
    try {
      await postJSON("/api/stories/" + story.id + "/comments", body);
      track(parentId ? "story-reply" : "story-comment");
      // re-render from a fresh fetch: places the reply correctly and picks up
      // anything others wrote in the meantime
      const list = await api("/api/stories/" + story.id + "/comments");
      renderComments(story, wrap, btn, list);
    } catch (e) {
      if (e.status === 401) { toLogin(); return; }
      status.textContent = e.status === 429 ? t("errRate") : t("errSubmit");
      send.disabled = false;
    }
  });
  if (parentId) text.focus();
  return form;
}

/* -- top strip -- */
let cacheTop = [];

function topRow(story) {
  const li = el("li", "top-row");
  const line = el("button", "top-line");
  line.type = "button";
  const score = el("span", "top-score");
  score.dataset.storyId = story.id;
  score.append(icon("up"), el("span", "top-num", story.score));
  line.append(score, el("span", "top-title", story.title),
              el("span", "top-station", story.from_station));
  const detail = el("div", "top-detail hidden");
  line.addEventListener("click", () => {
    if (detail.classList.contains("hidden")) {
      if (!detail.hasChildNodes()) detail.append(storyCard(story));
      detail.classList.remove("hidden");
    } else {
      detail.classList.add("hidden");
    }
  });
  li.append(line, detail);
  return li;
}

async function loadTop() {
  try {
    cacheTop = await api("/api/stories?sort=top&limit=" + TOP_N);
  } catch (e) {
    cacheTop = [];
  }
  renderTop();
}

function renderTop() {
  const strip = document.getElementById("top-strip");
  const list = document.getElementById("top-list");
  list.textContent = "";
  strip.classList.toggle("hidden", !cacheTop.length);
  cacheTop.forEach((s) => list.append(topRow(s)));
}

/* -- newest list -- */
let cacheNew = [];
let newOffset = 0;
let newSort = "new";
const seen = new Set();

function resetNew() {
  cacheNew = [];
  newOffset = 0;
  seen.clear();
  document.getElementById("story-list").textContent = "";
}

function appendStory(story) {
  if (seen.has(story.id)) return;
  seen.add(story.id);
  cacheNew.push(story);
  document.getElementById("story-list").append(storyCard(story));
}

async function loadNew() {
  const status = document.getElementById("stories-status");
  const moreBtn = document.getElementById("more-btn");
  if (!cacheNew.length) status.textContent = "…";
  try {
    const page = await api(`/api/stories?sort=${newSort}&limit=${PAGE}&offset=${newOffset}`);
    newOffset += page.length;
    status.textContent = "";
    page.forEach(appendStory);
    if (!cacheNew.length) status.textContent = t("empty");
    moreBtn.classList.toggle("hidden", page.length < PAGE);
  } catch (e) {
    status.textContent = t("errLoad");
  }
}

function rerenderNew() {
  const listEl = document.getElementById("story-list");
  listEl.textContent = "";
  cacheNew.forEach((s) => listEl.append(storyCard(s)));
}

/* -- permalink: /stories/42 pins that story above the board -- */
const permalinkId = (location.pathname.match(/^\/(?:stories|geschichten)\/(\d+)$/) || [])[1];

async function loadPermalink() {
  if (!permalinkId) return;
  const box = document.getElementById("permalink");
  const slot = box.querySelector(".permalink-slot");
  box.classList.remove("hidden");
  slot.textContent = "…";
  try {
    const story = await api("/api/stories/" + permalinkId);
    slot.textContent = "";
    // the tab and history entry name the story, as the server-rendered head did
    if (story.title) document.title = story.title + " – " + t("logoAlt");
    const card = storyCard(story);
    slot.append(card);
    // the link was shared for the thread as much as for the story
    if (story.comments) card.querySelector(".comments-toggle").click();
  } catch (e) {
    slot.textContent = e.status === 404 ? t("permalinkGone") : t("errLoad");
  }
}

/* -- compose -- */

/* "Hannover Hbf → Berlin Hbf", or just the origin when the story never left
   it. Stories written before the journey fields existed have only an origin
   too, so both cases render the same way and neither needs a special case. */
function legOf(story) {
  return story.to_station
    ? `${story.from_station} → ${story.to_station}` : story.from_station;
}

/* The departure the story is about, as typed: a local wall-clock stamp, so it
   is shown back verbatim rather than re-interpreted in the reader's zone. */
function departureText(story) {
  if (!story.departure) return null;
  const [date, time] = story.departure.split("T");
  const [y, m, d] = date.split("-");
  return lang === "en" ? `${d}/${m}/${y}, ${time}` : `${d}.${m}.${y}, ${time}`;
}

/* A deliberately plain station picker: type, pick, done. The search page's
   autocomplete carries favourites, recents and stars, none of which belong in
   a compose form - and the field takes free text either way, so a station the
   list has never heard of is still a place a story can happen. */
function setupStationPicker(inputId, dropdownId) {
  const input = document.getElementById(inputId);
  const dropdown = document.getElementById(dropdownId);
  let timer = null;

  function close() { dropdown.classList.remove("open"); }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/locations?query=${encodeURIComponent(q)}`);
        if (!resp.ok) return;
        const items = await resp.json();
        dropdown.innerHTML = "";
        items.forEach((item) => {
          const row = el("div", "dropdown-item");
          row.append(el("span", "dropdown-name", item.name));
          // mousedown, not click: the input's blur tears the dropdown down first
          row.addEventListener("mousedown", () => {
            input.value = item.name;
            close();
          });
          dropdown.appendChild(row);
        });
        dropdown.classList.toggle("open", items.length > 0);
      } catch (e) { /* network hiccup: the field still takes typing */ }
    }, 250);
  });

  input.addEventListener("blur", () => setTimeout(close, 150));
}

// today and now, in the visitor's own clock - the journey they are about to
// describe is nearly always the one they just had
function fillNow(prefix) {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  document.getElementById(prefix + "-date").value =
    `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  document.getElementById(prefix + "-time").value =
    `${pad(now.getHours())}:${pad(now.getMinutes())}`;
}

/* The chips a story carries, shown back as tags. "other" renders the text the
   visitor typed instead of the word "Other", which on its own says nothing. */
function problemTags(story) {
  return (story.problems || []).map((code) =>
    code === "other" && story.problem_other ? story.problem_other : t("problem_" + code));
}

const chipInputs = () => document.querySelectorAll(".chip-input");

function chosenProblems() {
  return [...chipInputs()].filter((c) => c.checked).map((c) => c.value);
}

// the free-text field only exists while there is a chip for it to specify
function syncOther() {
  const other = document.getElementById("p-other");
  const input = document.getElementById("c-other");
  input.classList.toggle("hidden", !other.checked);
  input.required = other.checked;
  if (other.checked) input.focus(); else input.value = "";
}

// both halves or neither: a date with no time is not a departure
function departureValue(prefix) {
  const date = document.getElementById(prefix + "-date").value;
  const time = document.getElementById(prefix + "-time").value;
  return date && time ? `${date}T${time}` : "";
}

function initCompose() {
  const toggle = document.getElementById("compose-toggle");
  const form = document.getElementById("compose-form");
  const status = document.getElementById("compose-status");
  toggle.addEventListener("click", () => {
    if (!me) { toLogin(); return; }
    form.classList.toggle("hidden");
    if (!form.classList.contains("hidden")) {
      fillNow("c");  // re-stamped each time it opens, not once on page load
      document.getElementById("c-from").focus();
    }
  });
  document.getElementById("p-other").addEventListener("change", syncOther);
  setupStationPicker("c-from", "c-from-dropdown");
  setupStationPicker("c-to", "c-to-dropdown");
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!me) { toLogin(); return; }
    const send = form.querySelector('[type="submit"]');
    send.disabled = true;
    status.textContent = t("sending");
    try {
      const created = await postJSON("/api/stories", {
        from_station: document.getElementById("c-from").value.trim(),
        to_station: document.getElementById("c-to").value.trim(),
        departure: departureValue("c"),
        train: document.getElementById("c-train").value.trim(),
        problems: chosenProblems(),
        problem_other: document.getElementById("c-other").value.trim(),
        title: document.getElementById("c-title").value.trim(),
        text: document.getElementById("c-text").value.trim(),
      });
      track("story-post", { problems: created.problems?.length ?? 0 });
      // own story lands on top of the newest list right away
      seen.add(created.id);
      cacheNew.unshift(created);
      newOffset += 1;
      const listEl = document.getElementById("story-list");
      listEl.prepend(storyCard(created));
      document.getElementById("stories-status").textContent = "";
      form.reset();
      syncOther();  // reset unchecks "other"; the text field has to follow
      form.classList.add("hidden");
      status.textContent = "";
      refreshBoard(); // the new report clatters onto the tally right away
    } catch (e) {
      if (e.status === 401) { toLogin(); return; }
      status.textContent = e.status === 429 ? t("errRate") : t("errSubmit");
    } finally {
      send.disabled = false;
    }
  });
}

/* -- split-flap tally board ------------------------------------------------

   The compose chips, added up: one little Solari module per problem code,
   counted over a calendar span. Each module is an odometer - on load it
   shows 0 and clatters up to its number, because a tally that arrives
   counting is the one dignity a broken WC can have. */

const BOARD_CODES = ["delay", "cancelled", "missed", "ac", "wc", "crowding", "wifi", "other"];
const stillPlease = window.matchMedia("(prefers-reduced-motion: reduce)");

function flapCell() {
  const cell = el("span", "flap");
  const halves = {};
  ["top", "bottom", "leaf"].forEach((part) => {
    const half = el("span", "flap-half flap-" + part);
    half.append(el("span", "flap-char", " "));
    halves[part] = half;
    cell.append(half);
  });
  const paint = (part, ch) => { halves[part].firstChild.textContent = ch; };

  let cur = " ";
  let timer = null;

  function jump(ch) {
    clearTimeout(timer);
    cur = ch;
    paint("top", ch);
    paint("bottom", ch);
    halves.leaf.classList.remove("drop");
  }

  // one flap turn: the old char falls off the top, the next was behind it
  function setChar(next) {
    if (next === cur) return;
    if (stillPlease.matches) { jump(next); return; }
    clearTimeout(timer);
    paint("top", next);
    paint("leaf", cur);
    halves.leaf.classList.remove("drop");
    void halves.leaf.offsetHeight; // restart the fall from upright
    halves.leaf.classList.add("drop");
    cur = next;
    timer = setTimeout(() => {
      paint("bottom", next);
      halves.leaf.classList.remove("drop");
    }, 90); // a shade past the CSS transition, so the leaf lands first
  }

  return { cell, setChar, jump };
}

/* A row of cells driven as one number. setValue counts the displayed number
   toward the target - every step is at most one flap per digit, so the ones
   digit clatters while the tens turn only when they carry. */
function flapCounter() {
  const wrap = el("span", "board-digits");
  wrap.setAttribute("aria-hidden", "true");
  const cells = []; // most significant first
  let shown = 0;
  let timer = null;

  function fit(width) {
    while (cells.length < width) {
      const c = flapCell();
      cells.unshift(c);
      wrap.prepend(c.cell);
    }
    while (cells.length > width) cells.shift().cell.remove();
  }

  function display(n, animate) {
    const text = String(n).padStart(cells.length, "0");
    cells.forEach((c, i) => (animate ? c.setChar(text[i]) : c.jump(text[i])));
    shown = n;
  }

  fit(2);
  display(0, false); // the board opens at zero, not blank

  function setValue(target) {
    clearTimeout(timer);
    const width = Math.max(2, String(target).length);
    if (width !== cells.length) { fit(width); display(0, false); }
    if (stillPlease.matches || target === shown) { display(target, false); return; }
    // ~1.2s ease-out from wherever the counter stands: quick out of the
    // gate, single ticks at the landing
    const TICKS = 14, MS = 85;
    const from = shown;
    let tick = 0;
    (function run() {
      tick += 1;
      const eased = 1 - Math.pow(1 - tick / TICKS, 3);
      const value = tick >= TICKS ? target : Math.round(from + (target - from) * eased);
      if (value !== shown) display(value, true);
      if (tick < TICKS) timer = setTimeout(run, MS);
    })();
  }

  return { wrap, setValue };
}

const board = { tiles: new Map(), span: "month", epoch: 0 };

function boardGroupLabels() {
  document.querySelector(".board-spans").setAttribute("aria-label", t("spanGroup"));
  document.getElementById("board").setAttribute("aria-label", t("boardHeading"));
}

function labelTile(tile, code) {
  tile.label.textContent = t("problem_" + code);
  tile.root.title = t(tile.mine ? "tapHintDone" : "tapHint");
  tile.root.setAttribute("aria-label",
    t("problem_" + code) + ": " + tile.count + (tile.mine ? ", " + t("tapped") : ""));
}

function relabelBoard() {
  boardGroupLabels();
  board.tiles.forEach(labelTile);
}

function markTile(tile, code, mine) {
  tile.mine = mine;
  tile.root.classList.toggle("tapped", mine);
  tile.root.setAttribute("aria-pressed", String(mine));
  labelTile(tile, code);
}

// one answer from the server: counts over the span, plus which tiles carry
// the viewer's own tap today
function applyBoard(res, stagger) {
  board.tiles.forEach((tile, code) => {
    tile.count = res.counts[code] || 0;
    markTile(tile, code, res.mine.includes(code));
    if (stagger) setTimeout(() => tile.counter.setValue(tile.count), Math.random() * 200);
    else tile.counter.setValue(tile.count);
  });
}

async function refreshBoard() {
  const epoch = ++board.epoch;
  let res;
  try {
    res = await api("/api/stories/problems?span=" + board.span);
  } catch (e) {
    return; // the board keeps its last numbers rather than going dark
  }
  if (epoch !== board.epoch) return; // a newer span click already answered
  applyBoard(res, true);
}

/* A tap on a tile is the shortest story there is: "this happened to me
   today, on this leg". An unlit tile opens the form below the board and
   nothing counts until that is sent; a lit tile takes today's report back. */
async function tapProblem(code) {
  if (!me) { toLogin(); return; }
  const tile = board.tiles.get(code);
  if (!tile.mine) {
    if (tap.code === code) closeTapForm(); else openTapForm(code);
    return;
  }
  closeTapForm();
  const epoch = ++board.epoch;
  try {
    const res = await postJSON(
      "/api/stories/problems/" + code + "?span=" + board.span, { vote: false });
    track("report-untap", { problem: code });
    if (epoch === board.epoch) applyBoard(res, false);
  } catch (e) {
    if (e.status === 401) { toLogin(); return; } // session expired mid-visit
  }
}

/* -- the tap form: where and when, asked before a tap counts -- */
const tap = { code: null };

function openTapForm(code) {
  const form = document.getElementById("tap-form");
  if (tap.code) board.tiles.get(tap.code).root.classList.remove("asking");
  tap.code = code;
  board.tiles.get(code).root.classList.add("asking");
  form.reset();
  document.getElementById("tap-status").textContent = "";
  document.getElementById("tap-title").textContent = fmt("tapTitle", t("problem_" + code));
  document.getElementById("q-other-field").classList.toggle("hidden", code !== "other");
  document.getElementById("q-other").required = code === "other";
  fillNow("q");
  form.classList.remove("hidden");
  form.scrollIntoView({ block: "nearest", behavior: stillPlease.matches ? "auto" : "smooth" });
  document.getElementById("q-from").focus();
}

function closeTapForm() {
  if (tap.code) board.tiles.get(tap.code).root.classList.remove("asking");
  tap.code = null;
  document.getElementById("tap-form").classList.add("hidden");
}

function initTapForm() {
  const form = document.getElementById("tap-form");
  const status = document.getElementById("tap-status");
  setupStationPicker("q-from", "q-from-dropdown");
  setupStationPicker("q-to", "q-to-dropdown");
  document.getElementById("tap-cancel").addEventListener("click", closeTapForm);
  form.addEventListener("keydown", (ev) => { if (ev.key === "Escape") closeTapForm(); });
  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (!me) { toLogin(); return; }
    const code = tap.code;
    const send = form.querySelector('[type="submit"]');
    send.disabled = true;
    status.textContent = t("sending");
    try {
      const res = await postJSON("/api/stories/problems/" + code + "?span=" + board.span, {
        vote: true,
        from_station: document.getElementById("q-from").value.trim(),
        to_station: document.getElementById("q-to").value.trim(),
        departure: departureValue("q"),
        train: document.getElementById("q-train").value.trim(),
        problem_other: document.getElementById("q-other").value.trim(),
      });
      track("report-tap", { problem: code });
      closeTapForm();
      board.epoch += 1; // an in-flight span fetch must not paint over this
      applyBoard(res, false);
    } catch (e) {
      if (e.status === 401) { toLogin(); return; }
      status.textContent = e.status === 429 ? t("errRate") : t("errSubmit");
    } finally {
      send.disabled = false;
    }
  });
}

function initBoard() {
  const host = document.getElementById("board");
  host.setAttribute("role", "group");
  BOARD_CODES.forEach((code) => {
    const root = el("button", "board-tile");
    root.type = "button";
    const counter = flapCounter();
    const label = el("span", "board-label");
    root.append(counter.wrap, label);
    root.addEventListener("click", () => tapProblem(code));
    host.append(root);
    const tile = { root, label, counter, count: 0, mine: false };
    board.tiles.set(code, tile);
    markTile(tile, code, false);
  });
  boardGroupLabels();
  document.querySelectorAll(".board-span").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (btn.dataset.span === board.span) return;
      board.span = btn.dataset.span;
      document.querySelectorAll(".board-span").forEach((b) =>
        b.classList.toggle("active", b === btn));
      refreshBoard();
    });
  });
  refreshBoard();
}

/* -- auth -- */
async function loadMe() {
  let hinted = false;
  try { hinted = localStorage.getItem("account") === "1"; } catch (e) {}
  if (hinted) {
    try {
      fb = await import("/firebase.js?v=1");
      if (fb.auth) {
        await fb.auth.authStateReady();
        const user = fb.auth.currentUser;
        if (user) {
          const who = await fb.identity(user);
          if (who.verified && who.name) me = { user, name: who.name };
        }
      }
      // signed out elsewhere, or a step short: stop paying for the SDK
      if (!me) fb.remember(false);
    } catch (e) {
      me = null;
    }
  }
  renderAuth();
}

document.getElementById("auth-logout").addEventListener("click", async () => {
  try {
    await fb.signOut(fb.auth);
  } catch (e) { /* the SDK may still hold the user; reload sorts it out */ }
  fb.remember(false);
  // reload rather than patch state: every rendered "voted" arrow is stale now
  location.reload();
});

/* -- feedback ask at the foot of the page -------------------------------
   Same widget as the connection search: a vote, then an optional comment
   under the same id so the two land as one row. A failed send is swallowed -
   the visitor is doing us a favour and must never see an error for it. */
(function initFeedback() {
  const box = document.getElementById("feedback-nudge");
  const lead = document.getElementById("feedback-lead");
  const form = document.getElementById("feedback-form");
  const input = document.getElementById("feedback-text");
  const sid = crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;

  async function send(vote, text) {
    try {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sid, vote, text, lang, context: "stories" }),
      });
    } catch (e) { /* swallowed by design */ }
  }
  // swapping data-i18n keeps the line right if the language changes afterwards
  function say(key) { lead.dataset.i18n = key; lead.textContent = t(key); }

  box.querySelectorAll(".feedback-vote").forEach((btn) => {
    btn.addEventListener("click", () => {
      box.dataset.vote = btn.dataset.vote;
      track(`feedback-${btn.dataset.vote}`);
      send(btn.dataset.vote, "");
      say("feedbackFollowUp");
      box.classList.add("feedback-voted");
      form.classList.remove("hidden");
      input.focus();
    });
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) form.requestSubmit();
  });
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const text = input.value.trim();
    if (text) {
      track("feedback-text", { vote: box.dataset.vote });
      send(box.dataset.vote, text);
    }
    say("feedbackThanks");
    form.classList.add("hidden");
    box.classList.add("feedback-done");
  });
  document.getElementById("feedback-skip").addEventListener("click", () => {
    track("feedback-dismiss");
    box.classList.add("hidden");
  });
})();

/* -- language -- */
function applyStatic() {
  document.documentElement.lang = lang;
  document.title = t("docTitle");
  const logo = document.getElementById("site-logo");
  logo.src = t("logoSrc");
  logo.alt = t("logoAlt");
  document.querySelectorAll("[data-i18n]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18n];
    if (text != null) node.textContent = text;
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18nPlaceholder];
    if (text != null) node.placeholder = text;
  });
  document.querySelectorAll("[data-i18n-title]").forEach((node) => {
    const text = I18N[lang][node.dataset.i18nTitle];
    if (text != null) { node.title = text; node.setAttribute("aria-label", text); }
  });
  document.querySelectorAll(".lang-btn").forEach((b) => {
    b.classList.toggle("active", b.dataset.lang === lang);
  });
}


applyStatic();
initCompose();
initTapForm();
// the lists wait for the identity: they come back with the viewer's own
// votes marked only when the request carried the token
loadMe().then(() => {
  initBoard();
  loadPermalink();
  loadTop();
  loadNew();
});

document.getElementById("more-btn").addEventListener("click", loadNew);
document.querySelectorAll(".list-sort").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.dataset.sort === newSort) return;
    newSort = btn.dataset.sort;
    track("stories-sort", { sort: newSort });
    document.querySelectorAll(".list-sort").forEach((b) =>
      b.classList.toggle("active", b === btn));
    resetNew();
    loadNew();
  });
});

})();
