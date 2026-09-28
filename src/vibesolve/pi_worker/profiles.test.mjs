import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { InMemoryCredentialStore } from "@earendil-works/pi-ai";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { stageStreamOptions } from "./runtime.mjs";

const profiles = JSON.parse(await readFile(new URL("../config/provider_models.json", import.meta.url), "utf8"));

test("every bundled profile resolves in the pinned Pi catalog without credentials or network", async t => {
  t.mock.method(globalThis, "fetch", async () => assert.fail("No network permitted"));
  const catalog = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(), modelsPath: null,
    allowModelNetwork: false, refreshOnCreate: false,
  });
  for (const [provider, profile] of Object.entries(profiles)) {
    assert(catalog.getProvider(provider), `${provider} must be a native Pi provider`);
    // Require a complete default: unlisted roles must not rely on implicit efforts.
    assert(profile._default.model && profile._default.effort, `${provider} needs a complete default`);
    for (const [role, override] of Object.entries(profile)) {
      const { model: id, effort } = { ...profile._default, ...override };
      const model = catalog.getModel(provider, id);
      assert(model, `${provider}/${role}: unknown model ${id}`);
      assert.doesNotThrow(() => stageStreamOptions(model, effort), `${provider}/${role}: effort=${effort}`);
    }
  }
});
