import assert from "node:assert/strict";
import { test } from "node:test";
import { streamSimple as responses } from "@earendil-works/pi-ai/api/openai-responses";
import { streamSimple as codex } from "@earendil-works/pi-ai/api/openai-codex-responses";
import { streamSimple as anthropic } from "@earendil-works/pi-ai/api/anthropic-messages";
import { streamSimple as google } from "@earendil-works/pi-ai/api/google-generative-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { stageStreamOptions } from "./runtime.mjs";

const context = { messages: [{ role: "user", content: "Offline fixture", timestamp: 1 }] };
// Synthetic token satisfies the SDK's account-claim parser, never authenticates.
const apiKey = "fixture." + Buffer.from(JSON.stringify({
  "https://api.openai.com/auth": { chatgpt_account_id: "offline-fixture" },
})).toString("base64url") + ".fixture";
const model = {
  id: "fixture", name: "fixture", api: "openai-responses", provider: "openai",
  baseUrl: "https://fixture.invalid/v1", reasoning: true, input: ["text"],
  contextWindow: 128000, maxTokens: 4000,
  thinkingLevelMap: { off: "none", minimal: null, low: "low", medium: "medium", high: "high" },
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
};

for (const [api, provider, stream] of [
  ["openai-responses", "openai", responses],
  ["openai-codex-responses", "openai-codex", codex],
]) {
  for (const effort of ["auto", "none", "low", "medium", "high"]) {
    test(`${api} builds exact effort=${effort}, before any network`, async () => {
      const chosen = { ...model, api, provider };
      const options = stageStreamOptions(chosen, effort, {
        apiKey,
        fetch: async () => { assert.fail("No network permitted in payload tests"); },
      });
      let payload;
      const prepare = options.onPayload;
      options.onPayload = async (params, selected) => {
        payload = await prepare(params, selected);
        throw new Error("Offline payload captured");
      };
      const result = await stream(chosen, context, options).result();
      assert.match(result.errorMessage, /Offline payload captured/);
      assert(payload, "The real SDK must build a payload");
      if (effort === "auto") assert(!Object.hasOwn(payload, "reasoning"));
      else assert.equal(payload.reasoning.effort, effort);
      if (effort !== "none") assert(payload.include.includes("reasoning.encrypted_content"));
    });
  }
}

test("unsupported explicit reasoning fails", () => {
  assert.throws(() => stageStreamOptions({ ...model, thinkingLevelMap: { off: null } }, "none"), /does not support effort/);
  assert.throws(() => stageStreamOptions({ ...model, reasoning: false }, "high"), /does not support effort/);
});

test("unverified auto mappings fail before sending a misleading explicit off setting", () => {
  assert.throws(() => stageStreamOptions({ ...model, api: "unverified-api" }, "auto"), /not yet verified/);
});

for (const [provider, id, stream] of [
  ["anthropic", "claude-haiku-4-5-20251001", anthropic],
  ["anthropic", "claude-sonnet-4-6", anthropic],
  ["google", "gemini-3.7-flash", google],
]) {
  test(`${provider}/${id} auto omits thinking in the actual SDK payload`, async t => {
    t.mock.method(globalThis, "fetch", async () => { assert.fail("No network permitted"); });
    const catalog = await ModelRuntime.create({ allowModelNetwork: false });
    const chosen = catalog.getModel(provider, id);
    assert(chosen);
    const options = stageStreamOptions(chosen, "auto", { apiKey: "offline-fixture" });
    const prepare = options.onPayload;
    let payload;
    options.onPayload = async (params, selected) => {
      payload = await prepare(params, selected);
      throw new Error("Offline payload captured");
    };
    const result = await stream(chosen, context, options).result();
    assert.match(result.errorMessage, /Offline payload captured/);
    assert(payload);
    assert(!Object.hasOwn(payload, "thinking"));
    assert(!Object.hasOwn(payload.output_config ?? {}, "effort"));
    assert(!Object.hasOwn(payload.config ?? {}, "thinkingConfig"));
  });
}

test("application hook preserves an upstream payload hook and encrypted context", async () => {
  const options = stageStreamOptions(model, "high", {
    onPayload: async payload => ({ ...payload, upstream: "retained", include: ["fixture-item"] }),
  });
  const payload = await options.onPayload({ reasoning: { effort: "low", summary: "auto" } }, model);
  assert.deepEqual(payload.reasoning, { effort: "high", summary: "auto" });
  assert.equal(payload.upstream, "retained");
  assert.deepEqual(payload.include, ["fixture-item", "reasoning.encrypted_content"]);
});
