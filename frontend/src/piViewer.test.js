import test from "node:test";
import assert from "node:assert/strict";
import { parsePiCredentials, realAnalysis } from "./piViewer.js";

const credentials = { backend_url: "https://looki.example.com", session_id: "29f0bac2-a523-4f11-aa36-24c78fb30b78", mobile_token: "looki1.test.signature" };
test("Pi credentials cannot be imported into a different origin", () => {
  assert.throws(() => parsePiCredentials(JSON.stringify(credentials), "https://other.example.com"), /autre backend/);
  assert.equal(parsePiCredentials(JSON.stringify(credentials), credentials.backend_url).session_id, credentials.session_id);
});
test("expired credentials and an operator key are rejected before attachment", () => {
  assert.throws(() => parsePiCredentials(JSON.stringify({ ...credentials, expires_at: "2000-01-01T00:00:00Z" }), credentials.backend_url), /expiré/);
  assert.throws(() => parsePiCredentials(JSON.stringify({ ...credentials, mobile_token: "operator-key" }), credentials.backend_url), /jeton mobile/);
});
test("an old simulated result cannot be presented as real Pi inference", () => {
  assert.equal(realAnalysis({ payload: { simulated: true, provider: "yolo_seg_track" } }), false);
  assert.equal(realAnalysis({ payload: { simulated: false, provider: "mock" } }), false);
  assert.equal(realAnalysis({ payload: { simulated: false, provider: "yolo_seg_track" } }), true);
});
