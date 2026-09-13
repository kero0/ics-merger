const state = {
  events: [],
  filter: "all",
  page: 0,
};

const calendar = document.querySelector("#calendar");
const rangeTitle = document.querySelector("#range-title");
const sourceState = document.querySelector("#source-state");
const eventCount = document.querySelector("#event-count");
const emptyState = document.querySelector("#empty-state");
const errorState = document.querySelector("#error-state");
const refreshButton = document.querySelector("#refresh");
const previousButton = document.querySelector("#previous");
const dialog = document.querySelector("#event-dialog");
const sourceLabel = new URLSearchParams(window.location.search).get("source_label");

function calendarUrl(path) {
  const url = new URL(path, window.location.origin);
  if (sourceLabel !== null) url.searchParams.set("source_label", sourceLabel);
  return `${url.pathname}${url.search}`;
}

document.querySelector(".download-button").href = calendarUrl("/api/calendars/merged.ics");

const dayFormatter = new Intl.DateTimeFormat(undefined, { weekday: "short" });
const rangeFormatter = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric" });
const timeFormatter = new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" });
const fullFormatter = new Intl.DateTimeFormat(undefined, {
  weekday: "long", month: "long", day: "numeric", hour: "numeric", minute: "2-digit",
});

function localDate(value, allDay) {
  return new Date(allDay ? `${value}T00:00:00` : value);
}

function startOfDay(value) {
  const result = new Date(value);
  result.setHours(0, 0, 0, 0);
  return result;
}

function addDays(value, days) {
  const result = new Date(value);
  result.setDate(result.getDate() + days);
  return result;
}

function eventKind(event) {
  const availability = event.availability.toLowerCase();
  if (event.status === "tentative" || availability === "tentative") return "tentative";
  if (["free", "transparent", "oof", "outofoffice", "workingelsewhere"].includes(availability)) return "free";
  return "busy";
}

function timeLabel(event, dayStart, dayEnd) {
  if (event.allDay) return "All day";
  const start = localDate(event.start, false);
  const end = localDate(event.end, false);
  if (start < dayStart && end > dayEnd) return "Continues all day";
  if (start < dayStart) return `Until ${timeFormatter.format(end)}`;
  if (end > dayEnd) return `From ${timeFormatter.format(start)}`;
  return `${timeFormatter.format(start)} - ${timeFormatter.format(end)}`;
}

function visibleEvents(dayStart, dayEnd) {
  return state.events.filter((event) => {
    if (state.filter !== "all" && eventKind(event) !== state.filter) return false;
    const start = localDate(event.start, event.allDay);
    const end = localDate(event.end, event.allDay);
    return start < dayEnd && end > dayStart;
  });
}

function eventButton(event, dayStart, dayEnd) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `event ${eventKind(event)}`;
  button.addEventListener("click", () => showEvent(event));

  const time = document.createElement("span");
  time.className = "event-time";
  time.textContent = timeLabel(event, dayStart, dayEnd);
  const title = document.createElement("span");
  title.className = "event-title";
  title.textContent = event.title;
  button.append(time, title);
  if (event.location) {
    const location = document.createElement("span");
    location.className = "event-location";
    location.textContent = event.location;
    button.append(location);
  }
  return button;
}

function render() {
  const today = startOfDay(new Date());
  const firstDay = addDays(today, state.page * 7);
  const lastDay = addDays(firstDay, 6);
  rangeTitle.textContent = `${rangeFormatter.format(firstDay)} - ${rangeFormatter.format(lastDay)}`;
  calendar.replaceChildren();

  let visibleCount = 0;
  for (let index = 0; index < 7; index += 1) {
    const dayStart = addDays(firstDay, index);
    const dayEnd = addDays(dayStart, 1);
    const events = visibleEvents(dayStart, dayEnd);
    visibleCount += events.length;

    const day = document.createElement("section");
    day.className = `day${dayStart.getTime() === today.getTime() ? " today" : ""}${events.length ? "" : " no-events"}`;
    const header = document.createElement("header");
    header.className = "day-header";
    const name = document.createElement("span");
    name.className = "day-name";
    name.textContent = dayFormatter.format(dayStart);
    const number = document.createElement("span");
    number.className = "day-number";
    number.textContent = String(dayStart.getDate());
    header.append(name, number);

    const list = document.createElement("div");
    list.className = "day-events";
    events.forEach((event) => list.append(eventButton(event, dayStart, dayEnd)));
    day.append(header, list);
    calendar.append(day);
  }

  eventCount.textContent = `${visibleCount} ${visibleCount === 1 ? "event" : "events"}`;
  emptyState.hidden = visibleCount !== 0 || !errorState.hidden;
}

function showEvent(event) {
  const start = localDate(event.start, event.allDay);
  const end = localDate(event.end, event.allDay);
  document.querySelector("#dialog-status").textContent = eventKind(event);
  document.querySelector("#dialog-title").textContent = event.title;
  document.querySelector("#dialog-time").textContent = event.allDay
    ? `${rangeFormatter.format(start)} - ${rangeFormatter.format(addDays(end, -1))}`
    : `${fullFormatter.format(start)} - ${fullFormatter.format(end)}`;
  const locationRow = document.querySelector("#dialog-location-row");
  locationRow.hidden = !event.location;
  document.querySelector("#dialog-location").textContent = event.location || "";
  const description = document.querySelector("#dialog-description");
  description.hidden = !event.description;
  description.textContent = event.description || "";
  dialog.showModal();
}

async function loadCalendar() {
  calendar.setAttribute("aria-busy", "true");
  refreshButton.classList.add("loading");
  errorState.hidden = true;
  try {
    const response = await fetch(calendarUrl("/api/calendars/merged.json"), {
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`Calendar request failed (${response.status})`);
    const payload = await response.json();
    state.events = payload.events;
    const successful = payload.sourceCount - payload.failedSourceCount;
    sourceState.className = `source-state ${payload.failedSourceCount ? "degraded" : "ready"}`;
    sourceState.lastElementChild.textContent = payload.failedSourceCount
      ? `${successful} connected, ${payload.failedSourceCount} unavailable`
      : `${successful} ${successful === 1 ? "source" : "sources"} connected`;
    render();
  } catch (error) {
    state.events = [];
    sourceState.className = "source-state failed";
    sourceState.lastElementChild.textContent = "Unavailable";
    errorState.hidden = false;
    emptyState.hidden = true;
    document.querySelector("#error-message").textContent = error.message;
    render();
  } finally {
    calendar.setAttribute("aria-busy", "false");
    refreshButton.classList.remove("loading");
  }
}

document.querySelector("#filters").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  document.querySelectorAll("button[data-filter]").forEach((item) => {
    const active = item === button;
    item.classList.toggle("active", active);
    item.setAttribute("aria-pressed", String(active));
  });
  render();
});

previousButton.addEventListener("click", () => { state.page -= 1; render(); });
document.querySelector("#today").addEventListener("click", () => { state.page = 0; render(); });
document.querySelector("#next").addEventListener("click", () => { state.page += 1; render(); });
refreshButton.addEventListener("click", loadCalendar);
document.querySelector("#retry").addEventListener("click", loadCalendar);
document.querySelector("#dialog-close").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); });

loadCalendar();