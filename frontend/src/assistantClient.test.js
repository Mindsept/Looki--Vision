import test from "node:test";
import assert from "node:assert/strict";
import { AssistantClient, freshAlert, exportQuestion } from "./assistantClient.js";

test("closing accepts an empty HTTP 202 response before polling", async () => {
  const client = new AssistantClient({}, () => {}, async () => new Response(null, { status: 202 }));
  client.session = "test";
  client.token = "temporary";
  assert.equal(await client.api("/realtime/calls/id", "DELETE"), null);
});

test("expired, duplicate and cross-session alerts cannot interrupt a fresh answer", () => {
  const event = { id: 9, session_id: "a", type: "alert.possible_proximity", payload: { expires_at: "2026-11-06T12:00:03Z" } };
  const now = Date.parse("2026-11-06T12:00:02Z");
  assert.equal(freshAlert(event, "a", 8, now), true);
  assert.equal(freshAlert(event, "a", 9, now), false);
  assert.equal(freshAlert(event, "b", 0, now), false);
  assert.equal(freshAlert(event, "a", 8, now + 2000), false);
  assert.equal(freshAlert({ ...event, id: null }, "a", 0, now), false);
});

test("stop mutes and destroys local media before the first remote command", async () => {
  const operations = [];
  const audio = { muted: false, pause() { operations.push("pause"); }, srcObject: {} };
  const client = new AssistantClient(audio, () => {});
  client.pc = { close() { operations.push("close-peer"); } };
  client.closeRemote = async () => { operations.push("remote"); assert.equal(audio.muted, true); assert.equal(audio.srcObject, null); };
  await client.stop("alert");
  assert.deepEqual(operations, ["pause", "close-peer", "remote"]);
});

test("retry sends the same logical request, never a fresh UUID", async () => {
  const payloads = [];
  const client = new AssistantClient({}, () => {});
  client.pending = { request_id: "stable", call_id: "call", media_epoch: 1, text: "Question", playback_ready: true };
  client.watchQuestion = () => {};
  client.api = async (_path, _method, body) => { payloads.push(structuredClone(body)); return { status: "accepted" }; };
  await client.sendPending(); await client.sendPending();
  assert.deepEqual(payloads[0], payloads[1]);
});

test("provider completion alone never reports local playback finished", async () => {
  const client = new AssistantClient({}, () => {});
  client.state = { question: { status: "completed" }, providerDrained: false };
  let calls = 0;
  client.api = async () => { calls++; };
  await assert.rejects(client.finish());
  assert.equal(calls, 0);
});

test("a late read from an old request cannot overwrite the next question", async () => {
  const client = new AssistantClient({}, () => {});
  client.pending = { request_id: "old" };
  let resolve;
  client.api = () => new Promise((done) => { resolve = done; });
  const reading = client.resolveQuestion();
  client.pending = { request_id: "new" };
  client.state.question = { request_id: "new", status: "accepted" };
  resolve({ request_id: "old", status: "failed" });
  await reading;
  assert.equal(client.state.question.request_id, "new");
});

test("diagnostic export excludes private answer content and transport credentials", () => {
  const result = exportQuestion({ request_id: "id", status: "completed", generation: { model: "gpt-realtime-2.1" }, text: "private question", transcript: "private answer", sdp_answer: "private SDP", token: "private token", error: { code: "timeout", message: "detail" } });
  assert.equal(result.error_code, "timeout");
  assert.equal(JSON.stringify(result).includes("private"), false);
});
