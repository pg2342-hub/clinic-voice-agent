// Mock scheduling system for a fictional two-clinic practice.
// Pure logic, no Cloudflare APIs: index.js keeps one state object per call in a Durable Object.
// Every name, date of birth and appointment comes from clinic/practice.json and is constructed.

export const WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
  "October", "November", "December"];
const SEARCH_DAYS = 14;

// ---- dates (all in the clinic's time zone, as plain YYYY-MM-DD strings) ----

export function clinicToday(now, timeZone) {
  const parts = new Intl.DateTimeFormat("en-CA", { timeZone, year: "numeric", month: "2-digit", day: "2-digit" })
    .format(now);
  return parts; // en-CA formats as YYYY-MM-DD
}

export function addDays(date, n) {
  const d = new Date(`${date}T12:00:00Z`);
  d.setUTCDate(d.getUTCDate() + n);
  return d.toISOString().slice(0, 10);
}

export function weekday(date) {
  return new Date(`${date}T12:00:00Z`).getUTCDay();
}

export function spokenDate(date) {
  const d = new Date(`${date}T12:00:00Z`);
  return `${WEEKDAYS[d.getUTCDay()]}, ${MONTHS[d.getUTCMonth()]} ${d.getUTCDate()}`;
}

export function spokenTime(hhmm) {
  const h = Math.floor(hhmm / 100);
  const m = hhmm % 100;
  const suffix = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(m).padStart(2, "0")} ${suffix}`;
}

// ---- names and dates of birth ----

export function normalizeName(s) {
  return String(s || "").toLowerCase().normalize("NFD").replace(/[^a-z]/g, "");
}

export function editDistance(a, b) {
  const dp = Array.from({ length: a.length + 1 }, (_, i) => [i, ...Array(b.length).fill(0)]);
  for (let j = 1; j <= b.length; j++) dp[0][j] = j;
  for (let i = 1; i <= a.length; i++) {
    for (let j = 1; j <= b.length; j++) {
      dp[i][j] = Math.min(dp[i - 1][j] + 1, dp[i][j - 1] + 1, dp[i - 1][j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
  }
  return dp[a.length][b.length];
}

export function namesMatch(said, onFile) {
  const a = normalizeName(said);
  const b = normalizeName(onFile);
  if (!a || !b) return false;
  return editDistance(a, b) <= Math.max(1, Math.floor(b.length / 4));
}

export function parseDob(s) {
  const t = String(s || "").trim();
  let m = t.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/);
  if (m) return fmt(+m[1], +m[2], +m[3]);
  m = t.match(/^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$/);
  if (m) return fmt(+m[3], +m[1], +m[2]);
  return null;
  function fmt(y, mo, d) {
    if (mo < 1 || mo > 12 || d < 1 || d > 31 || y < 1900 || y > 2026) return null;
    return `${y}-${String(mo).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
  }
}

// ---- slots ----

function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

// The therapist splits her week: Eastside on Mon/Wed/Fri, Westside on Tue/Thu, virtual only on Saturday.
export function providerClinicOn(provider, date) {
  if (provider.clinics.length === 1) return provider.clinics[0];
  const wd = weekday(date);
  return [1, 3, 5].includes(wd) ? "east" : [2, 4].includes(wd) ? "west" : null;
}

function step(visitType) {
  return visitType === "med_followup" ? 30 : 60;
}

// Every slot the practice offers in the search window, before bookings are removed.
export function allSlots(practice, today) {
  const slots = [];
  for (let n = 1; n <= SEARCH_DAYS; n++) {
    const date = addDays(today, n);
    if ((practice.closed_dates || []).includes(date)) continue; // holidays: no booked visits
    const wd = weekday(date);
    for (const p of practice.providers) {
      for (const vt of p.visit_types) {
        const rule = practice.visit_types[vt];
        if (!rule.days.includes(wd)) continue;
        const clinic = providerClinicOn(p, date);
        const virtualOnly = (rule.virtual_only_days || []).includes(wd);
        for (let min = rule.hours[0] * 60; min + rule.minutes <= rule.hours[1] * 60; min += step(vt)) {
          const hhmm = Math.floor(min / 60) * 100 + (min % 60);
          const id = `${p.id}-${date}-${String(hhmm).padStart(4, "0")}`;
          if (hash(`${id}-${vt}`) % 3 === 0) continue; // already booked by other patients
          slots.push({
            id, provider: p.id, visit_type: vt, date, hhmm, clinic: virtualOnly ? null : clinic,
            modalities: virtualOnly ? ["virtual"] : rule.modalities,
          });
        }
      }
    }
  }
  return slots;
}

// ---- state ----

export function newState(practice, now) {
  const today = clinicToday(now, practice.timezone);
  const appointments = practice.existing_appointments.map((a, i) => {
    const patient = practice.patients.find((p) => p.id === a.patient);
    let date = addDays(today, a.days_out);
    while (!practice.visit_types[a.visit_type].days.includes(weekday(date))) date = addDays(date, 1);
    return {
      id: `appt_${i + 1}`, patient: a.patient, provider: patient.provider, visit_type: a.visit_type, date,
      hhmm: a.hour * 100 + a.minute, clinic: patient.clinic, modality: a.modality, status: "booked",
    };
  });
  return { today, appointments, verified: [], new_patients: [], log: [], texts: [], transfers: [], next: 1 };
}

function taken(state, slot) {
  return state.appointments.some((a) => a.status === "booked" && a.provider === slot.provider &&
    a.date === slot.date && a.hhmm === slot.hhmm);
}

function describeSlot(practice, slot, modality) {
  const p = practice.providers.find((x) => x.id === slot.provider);
  const where = modality === "virtual" ? "by video" : `at the ${practice.clinics[slot.clinic].label}`;
  return `${spokenDate(slot.date)} at ${spokenTime(slot.hhmm)} with ${p.name}, ${where}`;
}

function describeAppt(practice, a) {
  return `${practice.visit_types[a.visit_type].label} on ${describeSlot(practice, a, a.modality)}`;
}

function verifiedPatient(practice, state, patientId) {
  if (!patientId) return null;
  if (!state.verified.includes(patientId) && !state.new_patients.some((p) => p.id === patientId)) return null;
  return practice.patients.find((p) => p.id === patientId) || state.new_patients.find((p) => p.id === patientId);
}

// ---- tools ----
// Each returns { ok, result } where result is the short text the agent reads.

export const TOOLS = {
  // Matches on date of birth plus last name. First names are not used: speech recognition garbles them most.
  find_patient(practice, state, { last_name, date_of_birth }) {
    const dob = parseDob(date_of_birth);
    if (!dob) return { ok: false, result: "Date of birth not understood. Ask for it again as month, day and year." };
    const hits = practice.patients.filter((p) => p.dob === dob && namesMatch(last_name, p.last));
    if (hits.length !== 1) {
      return {
        ok: false,
        result: "No patient on file matches that last name and date of birth. Do not say whether the name or the " +
          "date was wrong. Ask the caller to spell the last name and repeat the date of birth; if it still does not " +
          "match, they may be a new patient, or offer a staff member.",
      };
    }
    const p = hits[0];
    if (!state.verified.includes(p.id)) state.verified.push(p.id);
    const prov = practice.providers.find((x) => x.id === p.provider);
    const upcoming = state.appointments.filter((a) => a.patient === p.id && a.status === "booked");
    return {
      ok: true,
      result: JSON.stringify({
        patient_id: p.id, first_name: p.first, last_name: p.last, provider: prov.name,
        upcoming: upcoming.map((a) => ({ appointment_id: a.id, what: describeAppt(practice, a) })),
      }),
    };
  },

  register_new_patient(practice, state, { first_name, last_name, date_of_birth, phone }) {
    const dob = parseDob(date_of_birth);
    if (!dob || !normalizeName(first_name) || !normalizeName(last_name)) {
      return { ok: false, result: "Need first name, last name and a full date of birth." };
    }
    if (practice.patients.some((p) => p.dob === dob && namesMatch(last_name, p.last))) {
      return {
        ok: false,
        result: "A patient with that last name and date of birth is already on file. Use find_patient instead.",
      };
    }
    const id = `new_${state.next++}`;
    state.new_patients.push({ id, first: first_name, last: last_name, dob, phone: phone || null, provider: null });
    return { ok: true, result: JSON.stringify({ patient_id: id, status: "new patient record started" }) };
  },

  find_openings(practice, state, { visit_type, patient_id, modality, clinic, earliest_date, part_of_day }) {
    const rule = practice.visit_types[visit_type];
    if (!rule) return { ok: false, result: `Unknown visit type. Use one of ${Object.keys(practice.visit_types).join(", ")}.` };
    const patient = verifiedPatient(practice, state, patient_id);
    if (!patient) return { ok: false, result: "Verify the patient (find_patient or register_new_patient) first." };
    const isNew = patient.id.startsWith("new_");
    if (rule.new_patients_only && !isNew) {
      return { ok: false, result: "This patient is already established. A new patient evaluation is only for new patients; offer a medication follow-up with their own provider." };
    }
    if (rule.existing_patients_only && isNew) {
      return { ok: false, result: "A medication follow-up is only for established patients. A new patient starts with a new patient psychiatric evaluation, or can walk in today." };
    }
    const mod = modality || "in_person";
    if (!rule.modalities.includes(mod)) {
      return { ok: false, result: `A ${rule.label} is in person only.` };
    }
    let slots = allSlots(practice, state.today).filter((s) => s.visit_type === visit_type && s.modalities.includes(mod) && !taken(state, s));
    if (rule.own_provider) slots = slots.filter((s) => s.provider === patient.provider);
    if (clinic && clinic !== "any" && mod === "in_person") slots = slots.filter((s) => s.clinic === clinic);
    if (earliest_date) {
      const d = parseDob(earliest_date) || earliest_date;
      slots = slots.filter((s) => s.date >= d);
    }
    if (part_of_day === "morning") slots = slots.filter((s) => s.hhmm < 1200);
    if (part_of_day === "afternoon") slots = slots.filter((s) => s.hhmm >= 1200);
    slots.sort((a, b) => (a.date + a.hhmm).localeCompare(b.date + b.hhmm));
    const pick = slots.slice(0, 2); // a caller can hold two options in their head, not three
    if (!pick.length) return { ok: true, result: "No openings match. Offer a different day, time of day or clinic, or walk-in hours." };
    return {
      ok: true,
      result: JSON.stringify(pick.map((s) => ({ slot_id: `${s.id}:${mod}`, when: describeSlot(practice, s, mod) }))),
    };
  },

  book_appointment(practice, state, { slot_id, patient_id, visit_type }) {
    const patient = verifiedPatient(practice, state, patient_id);
    if (!patient) return { ok: false, result: "Verify the patient first." };
    const [base, mod] = String(slot_id || "").split(":");
    const slot = allSlots(practice, state.today).find((s) => s.id === base && s.visit_type === visit_type);
    if (!slot || !slot.modalities.includes(mod)) return { ok: false, result: "That slot does not exist for this visit type. Search again." };
    if (taken(state, slot)) return { ok: false, result: "That slot was just taken. Search again." };
    const rule = practice.visit_types[visit_type];
    const isNew = patient.id.startsWith("new_");
    if ((rule.new_patients_only && !isNew) || (rule.existing_patients_only && isNew) ||
      (rule.own_provider && slot.provider !== patient.provider)) {
      return { ok: false, result: "That visit type or provider is not allowed for this patient." };
    }
    const appt = {
      id: `appt_${100 + state.next++}`, patient: patient.id, provider: slot.provider, visit_type, date: slot.date,
      hhmm: slot.hhmm, clinic: slot.clinic, modality: mod, status: "booked",
    };
    state.appointments.push(appt);
    return { ok: true, result: JSON.stringify({ appointment_id: appt.id, booked: describeAppt(practice, appt) }) };
  },

  reschedule_appointment(practice, state, { appointment_id, new_slot_id, patient_id }) {
    const patient = verifiedPatient(practice, state, patient_id);
    const appt = state.appointments.find((a) => a.id === appointment_id && a.status === "booked");
    if (!patient || !appt || appt.patient !== patient.id) return { ok: false, result: "No booked appointment with that id for this verified patient." };
    const [base, mod] = String(new_slot_id || "").split(":");
    const slot = allSlots(practice, state.today).find((s) => s.id === base && s.visit_type === appt.visit_type);
    if (!slot || !slot.modalities.includes(mod) || taken(state, slot)) return { ok: false, result: "That new slot is not available for this visit type. Search again." };
    if (practice.visit_types[appt.visit_type].own_provider && slot.provider !== patient.provider) {
      return { ok: false, result: "A medication follow-up stays with the patient's own provider." };
    }
    appt.status = "rescheduled";
    const moved = { ...appt, id: `appt_${100 + state.next++}`, date: slot.date, hhmm: slot.hhmm, clinic: slot.clinic, modality: mod, status: "booked", from: appt.id };
    state.appointments.push(moved);
    return { ok: true, result: JSON.stringify({ appointment_id: moved.id, now: describeAppt(practice, moved) }) };
  },

  cancel_appointment(practice, state, { appointment_id, patient_id }) {
    const patient = verifiedPatient(practice, state, patient_id);
    const appt = state.appointments.find((a) => a.id === appointment_id && a.status === "booked");
    if (!patient || !appt || appt.patient !== patient.id) return { ok: false, result: "No booked appointment with that id for this verified patient." };
    appt.status = "cancelled";
    return { ok: true, result: JSON.stringify({ cancelled: describeAppt(practice, appt) }) };
  },

  send_confirmation_text(practice, state, { appointment_id, patient_id, phone }) {
    const patient = verifiedPatient(practice, state, patient_id);
    const appt = state.appointments.find((a) => a.id === appointment_id);
    if (!patient || !appt || appt.patient !== patient.id) return { ok: false, result: "Unknown appointment for this patient." };
    const to = phone || patient.phone;
    if (!to) return { ok: false, result: "No phone number on file. Ask for a mobile number." };
    const text = appt.status === "cancelled" ? `Cancelled: ${describeAppt(practice, appt)}.` : `Confirmed: ${describeAppt(practice, appt)}. Reply C to change.`;
    state.texts.push({ to, appointment_id, text });
    return { ok: true, result: `Text sent to the number ending ${String(to).slice(-4)}.` };
  },

  transfer_to_staff(practice, state, { reason, urgency, summary }) {
    const reasons = ["crisis", "clinical_question", "caller_request", "billing", "cannot_verify", "other"];
    const r = reasons.includes(reason) ? reason : "other";
    state.transfers.push({ reason: r, urgency: urgency || "routine", summary: summary || "" });
    const msg = r === "crisis"
      ? "Warm transfer to the on-call clinician started. Stay with the caller until they are connected. Demo line: no real transfer happens."
      : "Transfer to the front desk started with your summary. Demo line: no real transfer happens; tell the caller a staff member will pick up.";
    return { ok: true, result: msg };
  },
};

export function runTool(practice, state, name, args) {
  const tool = TOOLS[name];
  let out;
  if (!tool) out = { ok: false, result: `Unknown tool ${name}.` };
  else {
    try {
      out = tool(practice, state, args || {});
    } catch (e) {
      out = { ok: false, result: `Tool error: ${e.message}` };
    }
  }
  state.log.push({ tool: name, args: args || {}, ok: out.ok, result: out.result });
  return out;
}
