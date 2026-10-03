/* CareSlot frontend: talks ONLY to the Flask API (see app.py). No mock data. */
"use strict";

// When Flask serves this page (http://127.0.0.1:5000) we call the same origin.
// Otherwise (e.g. Live Server) we call the Flask URL directly (CORS is enabled).
const BASE = location.port === "5000" ? "" : "http://127.0.0.1:5000";
let me = null;   // the logged-in user ({id, name, email, role}) or null
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

class ApiError extends Error {
  constructor(message, code, status) { super(message); this.code = code; this.status = status; }
}

/* Reusable API helper. Backend shape: {success:true,data} or {success:false,error:{code,message}} */
async function api(path, { method = "GET", body } = {}) {
  let res;
  try {
    res = await fetch(BASE + "/api" + path, {
      method, credentials: "include", headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    $("#offline").hidden = false;
    throw new ApiError("Cannot reach the server. Start Flask with: python app.py", "NETWORK", 0);
  }
  $("#offline").hidden = true;
  let json = null;
  try { json = await res.json(); } catch (_) { /* not JSON */ }
  if (!json || typeof json.success !== "boolean") throw new ApiError(`Unexpected server response (HTTP ${res.status}).`, "BAD_RESPONSE", res.status);
  if (!json.success) throw new ApiError(json.error.message, json.error.code, res.status);
  return json.data;
}

/* ---------- small UI helpers ---------- */
function toast(msg, type = "") {
  const el = document.createElement("div");
  el.className = "toast " + type; el.textContent = msg;
  $("#toasts").append(el); setTimeout(() => el.remove(), 4500);
}
const fail = (e) => toast(e.message || "Something went wrong.", "error");
const fmtDate = (d) => new Date(d + "T00:00").toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short", year: "numeric" });
const badge = (s) => `<span class="badge ${s.toLowerCase()}">${esc(s)}</span>`;
const spinner = '<div class="spin">Loading...</div>';
const empty = (m) => `<div class="empty">${esc(m)}</div>`;

let lastFocus = null;
function openModal(html) {
  lastFocus = document.activeElement;
  $("#mBody").innerHTML = html; $("#overlay").hidden = false; $(".modal").focus();
}
function closeModal() { $("#overlay").hidden = true; $("#mBody").innerHTML = ""; if (lastFocus) lastFocus.focus(); }
$("#mClose").onclick = closeModal;
$("#overlay").addEventListener("mousedown", (e) => { if (e.target.id === "overlay") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#overlay").hidden) closeModal(); });

/* ---------- navigation ---------- */
function showView(name) {
  // Signed-in users get their profile instead of the login page; visitors cannot open the profile.
  if (name === "profile" && !me) name = "auth";
  else if (name === "auth" && me) name = "profile";
  if (name === "profile") fillProfile();
  window.scrollTo({ top: 0 });
  if (name === "admin" && !(me && me.role === "admin")) name = "auth";   // admin pages need an admin login
  document.querySelectorAll(".view").forEach((v) => (v.hidden = v.id !== "view-" + name));
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $("#nav").classList.remove("open"); $("#menuBtn").setAttribute("aria-expanded", "false");
  if (name === "provider" && !$("#pDoc").options.length) initProvider();
  if (name === "admin") initAdmin();
}
document.addEventListener("click", (e) => {
  const v = e.target.closest("[data-view]");
  if (v) { e.preventDefault(); showView(v.dataset.view); if (v.dataset.openTab) showTab(v.dataset.openTab); }
});
$("#menuBtn").onclick = () => {
  const open = $("#nav").classList.toggle("open"); $("#menuBtn").setAttribute("aria-expanded", open);
};

/* ---------- A. doctor discovery ---------- */
function doctorCard(d) {
  const n = d.available_slots_count;
  const next = d.next_slots && d.next_slots[0];
  return `<article class="card doc">
    <h3>${esc(d.name)}</h3><span class="pill">${esc(d.specialty)}</span>
    <p class="meta">${esc(d.hospital)}, ${esc(d.location)}</p>
    <p class="meta">${esc(d.experience_years)} years experience &middot; Fee: ${esc(d.consultation_fee)}</p>
    <p class="meta">${esc(d.description)}</p>
    <div class="foot">
      <span class="avail ${n ? "" : "none"}">${next ? `Next free: ${esc(fmtDate(next.date))} ${esc(next.time)}` : n ? `${n} free slots` : "No free slots"}</span>
      <button class="btn sm" data-act="open" data-id="${d.id}" ${n ? "" : "disabled"}>View slots</button>
    </div></article>`;
}

async function loadFilterOptions() {
  try {
    const [s, l] = await Promise.all([api("/specialties"), api("/locations")]);
    $("#fSpec").insertAdjacentHTML("beforeend", s.specialties.map((x) => `<option>${esc(x)}</option>`).join(""));
    $("#fLoc").insertAdjacentHTML("beforeend", l.locations.map((x) => `<option>${esc(x)}</option>`).join(""));
  } catch (e) { fail(e); }
}

async function loadDoctors() {
  const p = new URLSearchParams();
  if ($("#fQ").value.trim()) p.set("q", $("#fQ").value.trim());
  if ($("#fSpec").value) p.set("specialty", $("#fSpec").value);
  if ($("#fLoc").value) p.set("location", $("#fLoc").value);
  if ($("#fDate").value) p.set("date", $("#fDate").value);
  if ($("#fAvail").checked) p.set("available", "true");
  $("#doctors").innerHTML = spinner;
  try {
    const data = await api("/doctors?" + p);
    $("#count").textContent = `(${data.count})`;
    $("#doctors").innerHTML = data.doctors.map(doctorCard).join("") || empty("No doctors match these filters.");
  } catch (e) { $("#doctors").innerHTML = empty(e.message); }
}

/* "Earliest available": real data from /api/integration/doctors, sorted by the first free slot. */
async function loadEarliest() {
  $("#earliest").innerHTML = spinner;
  try {
    const data = await api("/integration/doctors?slots_per_doctor=1");
    const top = data.doctors.filter((d) => d.next_slots.length)
      .sort((a, b) => (a.next_slots[0].date + a.next_slots[0].time).localeCompare(b.next_slots[0].date + b.next_slots[0].time)).slice(0, 3);
    $("#earliest").innerHTML = top.map(doctorCard).join("") || empty("No free slots at the moment.");
  } catch (e) { $("#earliest").innerHTML = empty(e.message); }
}

let filterTimer;
$("#filters").addEventListener("input", () => { clearTimeout(filterTimer); filterTimer = setTimeout(loadDoctors, 300); });
$("#filters").addEventListener("submit", (e) => e.preventDefault());
$("#fReset").onclick = () => { $("#filters").reset(); loadDoctors(); };

/* ---------- B + C. slots and booking (modal) ---------- */
let current = null; // doctor currently shown in the modal

async function openDoctor(id, notice = "") {
  openModal(spinner);
  try {
    current = await api("/doctors/" + id);   // includes available_slots (only free, upcoming ones)
    const byDate = {};
    current.available_slots.forEach((s) => (byDate[s.date] ||= []).push(s));
    const days = Object.keys(byDate).map((d) => `<h4>${esc(fmtDate(d))}</h4><div class="slots">${byDate[d].map((s) =>
      `<button class="slot" data-act="pick" data-date="${s.date}" data-time="${s.time}">${esc(s.time)}</button>`).join("")}</div>`).join("");
    openModal(`<h2 id="mTitle">${esc(current.name)}</h2>
      <span class="pill">${esc(current.specialty)}</span>
      <p class="meta">${esc(current.hospital)}, ${esc(current.location)} &middot; Fee: ${esc(current.consultation_fee)}</p>
      <p>${esc(current.description)}</p>
      ${notice ? `<p class="alert error" style="margin:0 0 .8rem">${esc(notice)}</p>` : ""}
      <p class="muted" style="margin:0">Choose a free time. Only available slots are shown.</p>
      ${days || empty("No open slots right now.")}`);
  } catch (e) { closeModal(); fail(e); }
}

function loginPrompt() {
  openModal(`<h2 id="mTitle">Please log in to book</h2>
    <p>You need a patient account to book an appointment. Registering takes a few seconds.</p>
    <div class="actions"><button class="btn ghost" data-act="close">Not now</button>
    <button class="btn" data-act="goto-login">Log in / Register</button></div>`);
}

function bookingForm(date, time) {
  openModal(`<h2 id="mTitle">Confirm your visit</h2>
    <p><b>${esc(current.name)}</b> (${esc(current.specialty)})<br>${esc(fmtDate(date))} at ${esc(time)}</p>
    <form id="bookForm" class="form" novalidate data-date="${date}" data-time="${time}">
      <label>Full name<input name="patient_name" required maxlength="100" autocomplete="name" value="${esc(me ? me.name : "")}"></label>
      <label>Phone or email<input name="contact" required autocomplete="email" value="${esc(me ? me.email : "")}"></label>
      <label>Reason (optional)<textarea name="reason" rows="2" maxlength="300"></textarea></label>
      <p class="muted" style="margin:0">Use fictional details for this demo.</p>
      <div class="err" id="formErr" role="alert"></div>
      <div class="actions"><button type="button" class="btn ghost" data-act="back">Back</button>
        <button class="btn" type="submit" id="bookBtn">Book appointment</button></div>
    </form>`);
}

const validContact = (v) => /^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v) || /^\+?\d{7,15}$/.test(v.replace(/[\s\-().]/g, ""));

document.addEventListener("submit", async (e) => {
  if (e.target.id !== "bookForm") return;
  e.preventDefault();
  const f = e.target, btn = $("#bookBtn"), err = $("#formErr");
  const name = f.patient_name.value.trim(), contact = f.contact.value.trim();
  if (name.length < 2) { err.textContent = "Please enter your name (at least 2 characters)."; return; }
  if (!validContact(contact)) { err.textContent = "Enter a valid email or a phone number (7-15 digits)."; return; }
  err.textContent = ""; btn.disabled = true; btn.textContent = "Booking...";   // blocks double submit
  try {
    const data = await api("/appointments", { method: "POST", body: {
      patient_name: name, contact, doctor_id: current.id,
      appointment_date: f.dataset.date, appointment_time: f.dataset.time, reason: f.reason.value.trim() } });
    const a = data.appointment;
    $("#lookupVal").value = a.contact;
    openModal(`<h2 id="mTitle">Appointment booked</h2>
      <div class="ok-box"><p style="margin:0">${esc(data.message)}</p><p>Appointment ID: <code>${esc(a.appointment_id)}</code></p>
      <p style="margin:0">${esc(a.doctor.name)} (${esc(a.doctor.specialty)})<br>${esc(a.doctor.hospital)}, ${esc(a.doctor.location)}<br>
      ${esc(fmtDate(a.appointment_date))} at ${esc(a.appointment_time)} ${badge(a.status)}</p></div>
      <p class="muted">Save your appointment ID. No SMS or email is sent in this demo.</p>
      <div class="actions"><button class="btn" data-act="close">Done</button></div>`);
    loadDoctors(); loadEarliest();
  } catch (x) {
    btn.disabled = false; btn.textContent = "Book appointment";
    if (x.code === "SLOT_UNAVAILABLE" || x.code === "CONFLICT" || x.code === "SLOT_NOT_FOUND" || x.code === "PAST_SLOT") {
      openDoctor(current.id, x.message + " Please pick another time.");   // refresh real availability
      loadDoctors(); loadEarliest();
    } else if (x.code === "NOT_LOGGED_IN") { setUser(null); loginPrompt(); }
    else err.textContent = x.message;
  }
});

/* ---------- D. my appointments ---------- */
let lastLookup = "";
function apptCard(a, provider = false) {
  const booked = a.status === "BOOKED";
  return `<article class="card appt"><div style="display:flex;justify-content:space-between;gap:.5rem"><h3>${esc(a.doctor.name)}</h3>${badge(a.status)}</div>
    <p class="meta">${esc(a.doctor.specialty)} &middot; ${esc(a.doctor.hospital)}, ${esc(a.doctor.location)}</p>
    <p class="meta"><b>${esc(fmtDate(a.appointment_date))} at ${esc(a.appointment_time)}</b></p>
    <p class="meta">Patient: ${esc(a.patient_name)} (${esc(a.contact)})${a.reason ? `<br>Reason: ${esc(a.reason)}` : ""}</p>
    <p class="meta">ID: <code>${esc(a.appointment_id)}</code></p>
    <div class="foot"><span></span><span>${booked ? (provider
      ? `<button class="btn sm" data-act="complete" data-id="${esc(a.appointment_id)}">Mark completed</button>`
      : `<button class="btn sm ghost" data-act="resched" data-id="${esc(a.appointment_id)}" data-doc="${a.doctor.id}">Reschedule</button>
         <button class="btn sm danger" data-act="cancel" data-id="${esc(a.appointment_id)}">Cancel</button>`) : ""}</span></div></article>`;
}

async function lookup() {
  if (!lastLookup) return;
  const key = /^APT-/i.test(lastLookup) ? "appointment_id" : "contact";
  $("#apptList").innerHTML = spinner;
  try {
    const data = await api(`/appointments?${key}=${encodeURIComponent(lastLookup)}`);
    $("#apptList").innerHTML = data.appointments.map((a) => apptCard(a)).join("") || empty("No appointments found for that phone, email or ID.");
  } catch (e) { $("#apptList").innerHTML = empty(e.message); }
}
$("#lookup").addEventListener("submit", (e) => { e.preventDefault(); lastLookup = $("#lookupVal").value.trim(); lookup(); });

function confirmCancel(id) {
  openModal(`<h2 id="mTitle">Cancel appointment?</h2><p>Appointment <code>${esc(id)}</code> will be cancelled and the slot released.</p>
    <div class="actions"><button class="btn ghost" data-act="close">Keep it</button>
    <button class="btn danger" data-act="cancel-yes" data-id="${esc(id)}">Yes, cancel</button></div>`);
}

async function openReschedule(id, docId) {
  openModal(spinner);
  try {
    const data = await api(`/doctors/${docId}/slots`);
    const byDate = {};
    data.slots.forEach((s) => (byDate[s.date] ||= []).push(s));
    const days = Object.keys(byDate).map((d) => `<h4>${esc(fmtDate(d))}</h4><div class="slots">${byDate[d].map((s) =>
      `<button class="slot" data-act="resched-pick" data-id="${esc(id)}" data-date="${s.date}" data-time="${s.time}">${esc(s.time)}</button>`).join("")}</div>`).join("");
    openModal(`<h2 id="mTitle">Pick a new time</h2><p class="muted">Appointment ${esc(id)}. Only free slots are shown.</p>${days || empty("No free slots.")}`);
  } catch (e) { closeModal(); fail(e); }
}

async function change(id, action, body, okMsg, refresh) {
  try {
    await api(`/appointments/${encodeURIComponent(id)}/${action}`, { method: "PATCH", body });
    closeModal(); toast(okMsg, "ok"); refresh();
  } catch (e) { closeModal(); fail(e); refresh(); }   // never claim success on error
}

/* ---------- Provider dashboard ---------- */
async function initProvider() {
  try {
    const data = await api("/doctors");
    $("#pDoc").innerHTML = data.doctors.map((d) => `<option value="${d.id}">${esc(d.name)} - ${esc(d.specialty)}</option>`).join("");
    loadProvider();
  } catch (e) { fail(e); }
}

async function loadProvider() {
  const id = $("#pDoc").value; if (!id) return;
  const date = $("#pDate").value, status = $("#pStatus").value;
  $("#stats").innerHTML = spinner; $("#provList").innerHTML = spinner;
  const q = new URLSearchParams(); if (date) q.set("date", date); if (status) q.set("status", status);
  try {
    const [s, list] = await Promise.all([api(`/providers/${id}/stats` + (date ? `?date=${date}` : "")), api(`/providers/${id}/appointments?${q}`)]);
    const max = Math.max(1, ...s.daily_counts.map((d) => d.count));
    $("#stats").innerHTML = `
      <div class="stat"><b>${s.total_appointments}</b><span>Total appointments</span></div>
      <div class="stat t"><b>${s.by_status.BOOKED}</b><span>Booked</span></div>
      <div class="stat g"><b>${s.by_status.COMPLETED}</b><span>Completed</span></div>
      <div class="stat r"><b>${s.by_status.CANCELLED}</b><span>Cancelled</span></div>
      <div class="stat r"><b>${s.cancellation_rate_percent.toFixed(1)}%</b><span>Cancellation rate</span></div>
      <div class="stat t"><b>${s.next_7_days.utilization_percent.toFixed(1)}%</b><span>Utilization, next 7 days (${s.next_7_days.booked_slots}/${s.next_7_days.total_slots} slots)</span></div>
      <div class="stat"><b>${s.appointments_on_date}</b><span>On ${esc(fmtDate(s.date))}</span></div>
      <div class="stat"><b>${s.upcoming_booked}</b><span>Upcoming booked</span></div>
      <div class="bars" aria-label="Appointments per day, next 7 days">${s.daily_counts.map((d) =>
        `<div class="bar" title="${esc(d.date)}: ${d.count}">${d.count}<i style="height:${(d.count / max) * 70}%"></i>${esc(d.date.slice(5))}</div>`).join("")}</div>`;
    $("#provList").innerHTML = list.appointments.map((a) => apptCard(a, true)).join("") || empty("No appointments match these filters.");
  } catch (e) { $("#stats").innerHTML = ""; $("#provList").innerHTML = empty(e.message); }
}
$("#provFilters").addEventListener("change", loadProvider);
$("#pClear").onclick = () => { $("#pDate").value = ""; $("#pStatus").value = ""; loadProvider(); };

/* ---------- one click handler for all data-act buttons ---------- */
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-act]"); if (!b) return;
  const { act, id, date, time, doc } = b.dataset;
  const inProvider = !$("#view-provider").hidden;
  if (act === "open") openDoctor(id);
  else if (act === "pick") { if (me) bookingForm(date, time); else loginPrompt(); }
  else if (act === "goto-login") { closeModal(); showView("auth"); }
  else if (act === "back") openDoctor(current.id);
  else if (act === "close") closeModal();
  else if (act === "cancel") confirmCancel(id);
  else if (act === "cancel-yes") change(id, "cancel", undefined, "Appointment cancelled.", lookup);
  else if (act === "resched") openReschedule(id, doc);
  else if (act === "resched-pick") change(id, "reschedule", { appointment_date: date, appointment_time: time }, "Appointment rescheduled.", lookup);
  else if (act === "complete") change(id, "complete", undefined, "Marked as completed.", inProvider ? loadProvider : lookup);
});

/* ---------- start ---------- */
loadFilterOptions(); loadDoctors(); loadEarliest();

/* ---------- E. Appointment Agent ---------- */

$("#agentForm").addEventListener("submit", async (e) => {
  e.preventDefault();

  const message = $("#agentMessage").value.trim();
  if (!message) return;

  $("#agentStatus").textContent = "Searching for doctors...";
  $("#agentResults").innerHTML = "";

  const button = $("#agentForm").querySelector("button[type='submit']");
  button.disabled = true;

  try {
    const data = await api("/agent", {
      method: "POST",
      body: { message }
    });

    $("#agentStatus").textContent = data.reply || "Search completed.";

    const results = data.results || [];

    $("#agentResults").innerHTML = results.length
      ? results.map(doctorCard).join("")
      : empty("No matching doctors found. Try another specialty or location.");

  } catch (error) {
    $("#agentStatus").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
// ---------- F. Login: patient, register, admin ----------
function authMsg(text, isError = false) {
  const el = $("#authStatus"); el.textContent = text; el.style.color = isError ? "crimson" : "green";
}

function setUser(u) {
  me = u;
  $("#authNav").textContent = u ? `${u.name} (${u.role})` : "My profile";
  $("#adminNav").hidden = !(u && u.role === "admin");
  $("#logoutBtn").hidden = !u;
  document.querySelectorAll(".guest-only").forEach((el) => (el.hidden = !!u));   // landing-page buttons change after login
  document.querySelectorAll(".user-only").forEach((el) => (el.hidden = !u));
  $("#whoami").textContent = u ? `Signed in as ${u.name} (${u.role}).` : "Log in to book appointments. Demo only: use fictional details.";
}

function showTab(name) {
  document.querySelectorAll("[data-pane]").forEach((f) => (f.hidden = f.dataset.pane !== name));
  document.querySelectorAll(".tabs [data-tab]").forEach((b) => b.classList.toggle("ghost", b.dataset.tab !== name));
  authMsg("");
}
document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

async function submitAuth(form, path, body) {
  const btn = form.querySelector("button[type='submit']"); btn.disabled = true;
  try {
    const data = await api(path, { method: "POST", body });
    setUser(data.user); form.reset();
    authMsg(`Welcome, ${data.user.name}!`); toast("Signed in.", "ok");
    showView(data.user.role === "admin" ? "admin" : "doctors");
  } catch (e) { authMsg(e.message, true); }
  finally { btn.disabled = false; }
}
$("#loginForm").addEventListener("submit", (e) => { e.preventDefault();
  submitAuth(e.target, "/login", { email: $("#loginEmail").value.trim(), password: $("#loginPassword").value }); });
$("#registerForm").addEventListener("submit", (e) => { e.preventDefault();
  submitAuth(e.target, "/register", { name: $("#registerName").value.trim(), email: $("#registerEmail").value.trim(), password: $("#registerPassword").value }); });
$("#adminForm").addEventListener("submit", (e) => { e.preventDefault();
  submitAuth(e.target, "/admin/login", { email: $("#adminEmail").value.trim(), password: $("#adminPassword").value }); });

$("#logoutBtn").onclick = async () => {
  try { await api("/logout", { method: "POST" }); } catch (e) { return fail(e); }
  setUser(null); showView("home"); toast("Logged out.", "ok");
};

// Restore the session when the page loads (a visitor who is not logged in can still browse).
api("/me").then((d) => setUser(d.user)).catch(() => setUser(null));

// ---------- G. Admin panel ----------
let adminDocs = [];
const F = { name: "#dName", specialty: "#dSpec", hospital: "#dHosp", location: "#dLoc", experience_years: "#dExp", consultation_fee: "#dFee", description: "#dDesc" };

async function initAdmin(selectId) {
  try {
    adminDocs = (await api("/doctors")).doctors;
    $("#aDoc").innerHTML = '<option value="">+ Add a new doctor</option>' +
      adminDocs.map((d) => `<option value="${d.id}">${esc(d.name)} - ${esc(d.specialty)}</option>`).join("");
    if (selectId) $("#aDoc").value = selectId;
    pickAdminDoctor();
  } catch (e) { fail(e); }
}

function pickAdminDoctor() {
  const id = $("#aDoc").value, d = adminDocs.find((x) => String(x.id) === id);
  Object.entries(F).forEach(([k, sel]) => ($(sel).value = d ? d[k] : ""));
  $("#aDelete").hidden = !d; $("#slotCard").hidden = !d;
  if (d) loadAdminSlots();
}
$("#aDoc").onchange = pickAdminDoctor;

$("#docForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = $("#aDoc").value, body = {};
  Object.entries(F).forEach(([k, sel]) => (body[k] = $(sel).value.trim()));
  try {
    const data = await api(id ? `/admin/doctors/${id}` : "/admin/doctors", { method: id ? "PUT" : "POST", body });
    toast(data.message, "ok"); initAdmin(data.doctor.id); loadDoctors(); loadEarliest();
  } catch (x) { fail(x); }
});

$("#aDelete").onclick = async () => {
  const id = $("#aDoc").value;
  if (!id || !confirm("Delete this doctor? This cannot be undone.")) return;
  try { toast((await api(`/admin/doctors/${id}`, { method: "DELETE" })).message, "ok"); initAdmin(); loadDoctors(); loadEarliest(); }
  catch (x) { fail(x); }
};

async function loadAdminSlots() {
  const id = $("#aDoc").value; if (!id) return;
  $("#aSlots").innerHTML = spinner;
  try {
    const { slots } = await api(`/admin/doctors/${id}/slots`);
    const byDate = {};
    slots.forEach((s) => (byDate[s.date] ||= []).push(s));
    $("#aSlots").innerHTML = Object.keys(byDate).map((d) => `<h4>${esc(fmtDate(d))}</h4><div class="slots">${byDate[d].map((s) => s.booked
      ? `<span class="slot booked" title="Booked">${esc(s.time)} booked</span>`
      : `<button class="slot del" data-slot="${s.slot_id}" title="Delete this slot">${esc(s.time)} &times;</button>`).join("")}</div>`).join("")
      || empty("No upcoming slots. Add some above.");
  } catch (e) { $("#aSlots").innerHTML = empty(e.message); }
}

$("#slotForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const times = $("#sTimes").value.split(",").map((t) => t.trim()).filter(Boolean);
  try {
    const data = await api(`/admin/doctors/${$("#aDoc").value}/slots`, { method: "POST", body: { date: $("#sDate").value, times } });
    toast(data.message + (data.skipped ? ` ${data.skipped} already existed.` : ""), "ok");
    loadAdminSlots(); loadDoctors(); loadEarliest();
  } catch (x) { fail(x); }
});

$("#aSlots").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-slot]"); if (!b) return;
  try { await api(`/admin/slots/${b.dataset.slot}`, { method: "DELETE" }); toast("Slot deleted.", "ok"); loadAdminSlots(); loadDoctors(); loadEarliest(); }
  catch (x) { fail(x); loadAdminSlots(); }
});

// ---------- H. Profile, change password, forgot password ----------
function fillProfile() {
  $("#pfName").value = me.name; $("#pfEmail").value = me.email;
  $("#pfPw").value = ""; $("#pfPwWrap").hidden = true;
}
function sessionGone(x) {   // the login expired while the page was open
  if (x.code === "NOT_LOGGED_IN") { setUser(null); showView("auth"); }
  fail(x);
}
$("#pfEmail").addEventListener("input", () => {
  $("#pfPwWrap").hidden = !me || $("#pfEmail").value.trim().toLowerCase() === me.email;   // password only needed if the email changes
});
$("#profileForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = { name: $("#pfName").value.trim(), email: $("#pfEmail").value.trim() };
  if (!$("#pfPwWrap").hidden) body.current_password = $("#pfPw").value;
  try {
    const data = await api("/profile", { method: "PUT", body });
    setUser(data.user); fillProfile(); toast(data.message, "ok");
  } catch (x) { sessionGone(x); }
});
$("#pwForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("#pwNew").value !== $("#pwNew2").value) return toast("The new passwords do not match.", "error");
  try {
    const data = await api("/profile/password", { method: "POST", body: { current_password: $("#pwCur").value, new_password: $("#pwNew").value } });
    e.target.reset(); toast(data.message, "ok");
  } catch (x) { sessionGone(x); }
});
$("#forgotForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("#forgotEmail").value.trim();
  try {
    const data = await api("/forgot-password", { method: "POST", body: { email } });
    $("#resetEmail").value = email; showTab("reset");
    authMsg(data.message + (data.demo_reset_code ? ` Demo reset code: ${data.demo_reset_code}` : ""));
  } catch (x) { authMsg(x.message, true); }
});
$("#resetForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const data = await api("/reset-password", { method: "POST", body: {
      email: $("#resetEmail").value.trim(), code: $("#resetCode").value.trim(), new_password: $("#resetPw").value } });
    e.target.reset(); showTab("login"); authMsg(data.message);
  } catch (x) { authMsg(x.message, true); }
});

// ---------- I. Landing page: scroll animations and live numbers ----------
const reveal = new IntersectionObserver((entries) => entries.forEach((en) => {
  if (en.isIntersecting) { en.target.classList.add("in"); reveal.unobserve(en.target); }
}), { threshold: 0.15 });
document.querySelectorAll(".rv").forEach((el) => reveal.observe(el));
window.addEventListener("scroll", () => $(".topbar").classList.toggle("scrolled", window.scrollY > 8), { passive: true });

function countUp(el, to) {
  const start = performance.now();
  (function tick(now) {
    const p = Math.min(1, (now - start) / 1200);
    el.textContent = Math.round(to * (1 - Math.pow(1 - p, 3)));   // ease-out
    if (p < 1) requestAnimationFrame(tick);
  })(start);
}
async function loadHome() {   // every number here comes from the real API
  try {
    const [d, s, l] = await Promise.all([api("/doctors"), api("/specialties"), api("/locations")]);
    const open = d.doctors.reduce((n, x) => n + x.available_slots_count, 0);
    Object.entries({ doctors: d.count, specialties: s.specialties.length, locations: l.locations.length, slots: open })
      .forEach(([k, v]) => countUp($(`[data-count="${k}"]`), v));
    const chips = s.specialties.map((x) => `<span>${esc(x)}</span>`).join("");
    $("#marquee").innerHTML = chips + chips;   // doubled so the loop is seamless
  } catch (_) { /* the offline banner already explains; numbers stay as dashes */ }
}
loadHome();
