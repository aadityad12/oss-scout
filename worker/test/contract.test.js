import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  ACTIONS, NO_TEXT_ACTIONS, KEY_RE, MAX_TITLE, MAX_BODY, MAX_SUMMARY, MAX_DISPATCH_BODY, MAX_REQUEST, parseAct,
} from "../src/index.js";

// The shared act dispatch contract; tests/test_contract.py checks the scout's half.
const read = (path) => JSON.parse(readFileSync(new URL(path, import.meta.url), "utf8"));
const contract = read("../../contract/act-dispatch.json");
const samples = read("../../tests/fixtures/contract_key_samples.json");

test("the Worker's actions are the contract's, in order", () => {
  assert.deepEqual(ACTIONS, contract.actions);
});

test("the actions that ignore their text are the contract's", () => {
  assert.deepEqual(NO_TEXT_ACTIONS, contract.no_text_actions);
  assert.ok(ACTIONS.includes(contract.summary_action));
  assert.ok(!NO_TEXT_ACTIONS.includes(contract.summary_action));
});

test("the key pattern is the contract's and sorts the sample keys the same way", () => {
  // new RegExp writes a / as \/ in its source, so compare against the pattern as a RegExp
  assert.equal(KEY_RE.source, new RegExp(contract.key_pattern).source);
  assert.equal(KEY_RE.flags, "");
  for (const { key, valid } of samples) assert.equal(KEY_RE.test(key), valid, JSON.stringify(key));
});

test("the limits are the contract's", () => {
  assert.equal(MAX_TITLE, contract.limits.max_title);
  assert.equal(MAX_BODY, contract.limits.max_body);
  assert.equal(MAX_SUMMARY, contract.limits.max_summary);
  assert.equal(MAX_DISPATCH_BODY, contract.limits.max_dispatch_body_b64);
  assert.equal(MAX_REQUEST, contract.limits.max_request_bytes);
});

test("every action turns into exactly the workflow's inputs", () => {
  for (const action of ACTIONS) {
    const parsed = parseAct({ key: "o/r#1", action, title: "T", body: "text" });
    assert.ok(!parsed.error, `${action}: ${parsed.error}`);
    assert.deepEqual(Object.keys(parsed.inputs), contract.inputs, action);
  }
});

test("the actions that ignore their text send none", () => {
  for (const action of contract.no_text_actions) {
    const { inputs } = parseAct({ key: "o/r#1", action, title: "T", body: "text" });
    assert.equal(inputs.title, "", action);
    assert.equal(inputs.body_b64, "", action);
  }
});

test("the Worker dispatches the workflow the contract names", () => {
  const source = readFileSync(new URL("../src/index.js", import.meta.url), "utf8");
  assert.ok(source.includes(`/actions/workflows/${contract.workflow.file}/dispatches`));
  assert.ok(source.includes(`ref: "${contract.workflow.ref}", inputs: parsed.inputs`));
});
