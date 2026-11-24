import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  allSlots, clinicToday, namesMatch, newState, parseDob, runTool, spokenTime, weekday,
} from "../src/scheduler.js";

const practice = JSON.parse(readFileSync(new URL("../../clinic/practice.json", import.meta.url)));
const NOW = new Date("2026-12-01T17:00:00Z"); // a Tuesday morning in the clinic's time zone

function fresh() {
  return newState(practice, NOW);
}
function call(state, name, args) {
  return runTool(practice, state, name, args);
}
function verify(state, last, dob) {
  const out = call(state, "find_patient", { last_name: last, date_of_birth: dob });
  assert.equal(out.ok, true, out.result);
  return JSON.parse(out.result);
}
function firstSlot(state, args) {
  const out = call(state, "find_openings", args);
  assert.equal(out.ok, true, out.result);
  return JSON.parse(out.result)[0];
}

test("dates are taken in the clinic's time zone", () => {
  assert.equal(clinicToday(new Date("2026-12-02T05:00:00Z"), practice.timezone), "2026-12-01");
  assert.equal(spokenTime(930), "9:30 AM");
  assert.equal(spokenTime(1400), "2:00 PM");
});

test("date of birth parsing accepts ISO and US formats, rejects nonsense", () => {
  assert.equal(parseDob("3/4/1991"), "1991-03-04");
  assert.equal(parseDob("1991-03-04"), "1991-03-04");
  assert.equal(parseDob("March fourth"), null);
  assert.equal(parseDob("13/40/1991"), null);
});

test("last names tolerate small speech-recognition slips, not different names", () => {
  assert.ok(namesMatch("Nguyen", "Nguyen"));
  assert.ok(namesMatch("Nguyan", "Nguyen"));
  assert.ok(namesMatch("Raghunathen", "Raghunathan"));
  assert.ok(!namesMatch("Win", "Nguyen"));
  assert.ok(!namesMatch("Bell", "Reyes"));
});

test("a patient is found only with the right date of birth, and the miss does not say which part was wrong", () => {
  const s = fresh();
  const bad = call(s, "find_patient", { last_name: "Nguyen", date_of_birth: "1991-04-03" });
  assert.equal(bad.ok, false);
  assert.match(bad.result, /Do not say whether the name or the date was wrong/);
  assert.equal(s.verified.length, 0);
  const p = verify(s, "Nguyen", "03/04/1991");
  assert.equal(p.patient_id, "pt_101");
  assert.equal(p.upcoming.length, 1);
});

test("nothing about appointments is available before verification", () => {
  const s = fresh();
  const out = call(s, "find_openings", { visit_type: "med_followup", patient_id: "pt_101" });
  assert.equal(out.ok, false);
  const c = call(s, "cancel_appointment", { appointment_id: "appt_1", patient_id: "pt_101" });
  assert.equal(c.ok, false);
});

test("slots follow the practice's rules", () => {
  const s = fresh();
  const slots = allSlots(practice, s.today);
  for (const sl of slots) {
    const rule = practice.visit_types[sl.visit_type];
    assert.ok(rule.days.includes(weekday(sl.date)), `${sl.id} on a closed day`);
    assert.ok(sl.date > s.today, "no same-day slots");
    if (sl.visit_type === "new_eval") assert.deepEqual(sl.modalities, ["in_person"]);
    if (weekday(sl.date) === 6) assert.deepEqual(sl.modalities, ["virtual"]);
  }
  assert.ok(slots.length > 200);
});

test("medication follow-ups stay with the patient's own provider", () => {
  const s = fresh();
  verify(s, "Nguyen", "1991-03-04");
  const out = JSON.parse(call(s, "find_openings", { visit_type: "med_followup", patient_id: "pt_101", modality: "virtual" }).result);
  assert.ok(out.every((o) => o.when.includes("Dr. Elena Ramos")));
  assert.ok(out.every((o) => o.slot_id.endsWith(":virtual")));
});

test("an established patient cannot book a new patient evaluation, and a new patient cannot book a follow-up", () => {
  const s = fresh();
  verify(s, "Bell", "11/23/1985");
  assert.equal(call(s, "find_openings", { visit_type: "new_eval", patient_id: "pt_102" }).ok, false);
  const np = JSON.parse(call(s, "register_new_patient", { first_name: "Jordan", last_name: "Price", date_of_birth: "1999-02-14", phone: "555-0199" }).result);
  assert.equal(call(s, "find_openings", { visit_type: "med_followup", patient_id: np.patient_id }).ok, false);
  assert.equal(call(s, "find_openings", { visit_type: "new_eval", patient_id: np.patient_id, modality: "virtual" }).ok, false);
});

test("book, text, reschedule and cancel change only this call's state", () => {
  const s = fresh();
  const np = JSON.parse(call(s, "register_new_patient", { first_name: "Jordan", last_name: "Price", date_of_birth: "1999-02-14", phone: "555-0199" }).result);
  const slot = firstSlot(s, { visit_type: "new_eval", patient_id: np.patient_id, clinic: "east", part_of_day: "morning" });
  assert.match(slot.when, /Eastside clinic/);
  const booked = JSON.parse(call(s, "book_appointment", { slot_id: slot.slot_id, patient_id: np.patient_id, visit_type: "new_eval" }).result);
  // the same slot cannot be booked twice
  assert.equal(call(s, "book_appointment", { slot_id: slot.slot_id, patient_id: np.patient_id, visit_type: "new_eval" }).ok, false);
  const t = call(s, "send_confirmation_text", { appointment_id: booked.appointment_id, patient_id: np.patient_id });
  assert.match(t.result, /0199/);

  const s2 = fresh();
  const p = verify(s2, "Reyes", "1978-01-30");
  const appt = p.upcoming[0].appointment_id;
  const next = firstSlot(s2, { visit_type: "med_followup", patient_id: "pt_104", part_of_day: "afternoon" });
  const moved = JSON.parse(call(s2, "reschedule_appointment", { appointment_id: appt, new_slot_id: next.slot_id, patient_id: "pt_104" }).result);
  assert.match(moved.now, /PM/);
  assert.equal(s2.appointments.find((a) => a.id === appt).status, "rescheduled");
  const c = call(s2, "cancel_appointment", { appointment_id: moved.appointment_id, patient_id: "pt_104" });
  assert.equal(c.ok, true);
  // another verified patient cannot touch Xavier's appointments
  verify(s2, "Bell", "1985-11-23");
  assert.equal(call(s2, "cancel_appointment", { appointment_id: moved.appointment_id, patient_id: "pt_102" }).ok, false);
});

test("transfers are logged with reason and summary, and every call goes in the log", () => {
  const s = fresh();
  call(s, "transfer_to_staff", { reason: "crisis", urgency: "immediate", summary: "caller says they want to end their life" });
  call(s, "nope", {});
  assert.equal(s.transfers[0].reason, "crisis");
  assert.deepEqual(s.log.map((l) => l.tool), ["transfer_to_staff", "nope"]);
  assert.equal(s.log[1].ok, false);
});

test("offers at most two openings, and nothing on closed days", () => {
  const s = fresh();
  verify(s, "Nguyen", "1991-03-04");
  const out = JSON.parse(call(s, "find_openings", { visit_type: "med_followup", patient_id: "pt_101" }).result);
  assert.ok(out.length <= 2);
  const days = new Set(allSlots(practice, s.today).map((x) => x.date));
  assert.equal(days.has("2026-12-25"), false);
});
