import test from "node:test";
import assert from "node:assert/strict";
import { percentile, windowRate, resultAge, distanceText } from "./metrics.js";
test("latency statistics retain the slow first sample", () => {
  assert.ok(Math.abs(percentile([1000, 100, 100], 0.95) - 910) < 1e-9);
  assert.equal(percentile([], 0.5), null);
});
test("live rates decay to zero after processing stops", () => {
  assert.equal(windowRate([1000, 2000], 3000, 0), 2 / 3);
  assert.equal(windowRate([1000, 2000], 8000, 0), 0);
});
test("unknown depth cannot be displayed as zero metres", () => {
  assert.equal(distanceText(null), "Inconnue");
  assert.equal(distanceText(0.81), "0.81 m");
});
test("result age comes from capture rather than display time", () => {
  assert.equal(
    resultAge("2026-10-19T10:00:00Z", Date.parse("2026-10-19T10:00:03Z")),
    3,
  );
});


test("possible proximity takes precedence over a far metric label without changing metres", async () => {
  const { objectRisk, riskLabel, shortTrack } = await import("./metrics.js");
  const person = {distance_m: 7, proximity: "far", perception: {risk: "possible", track_id: "epoch:person:12"}};
  assert.equal(objectRisk(person), "possible");
  assert.equal(riskLabel(person), "Proximité possible");
  assert.equal(shortTrack(person), "person #12");
  assert.equal(distanceText(person.distance_m), "7.00 m");
});


test("approach is distinct from proximity and never implies measured metres", async () => {
  const { objectRisk, riskLabel, motionLabel } = await import("./metrics.js");
  const object = { distance_m: null, proximity: "unknown", perception: {risk: "approaching", motion: "approaching"} };
  assert.equal(objectRisk(object), "approaching");
  assert.equal(riskLabel(object), "En approche");
  assert.equal(distanceText(object.distance_m), "Inconnue");
  const held = {...object, perception: {risk: "possible", motion: "unknown"}};
  assert.equal(riskLabel(held), "Proximité possible");
  assert.equal(motionLabel(held), "Tendance incertaine");
});


test("vehicle alert filters use payload classes and retain mixed alerts once", async () => {
  const { filterAlerts, reasonText } = await import("./metrics.js");
  const events = [
    {id:1, payload:{detections:[{label:"car"}, {label:"person"}]}},
    {id:2, payload:{detections:[{label:"bicycle"}, {label:"bicycle"}]}},
  ];
  assert.deepEqual(filterAlerts(events, "car").map(e => e.id), [1]);
  assert.deepEqual(filterAlerts(events, "bicycle").map(e => e.id), [2]);
  assert.equal(filterAlerts(events, "all"), events);
  assert.equal(reasonText("large_object_mask"), "Objet imposant selon le profil de sa classe");
  assert.equal(reasonText("large_person_mask"), "Personne imposante dans l’image");
});
