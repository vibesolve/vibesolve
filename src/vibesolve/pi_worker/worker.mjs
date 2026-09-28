import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { PiRuntime } from "./runtime.mjs";

const MAX_LINE = 16 * 1024 * 1024;
// Stdout is exclusively the machine protocol. SDK diagnostic logs belong on stderr.
console.log = (...args) => console.error(...args);
const send = event => {
  const line = JSON.stringify(event);
  if (Buffer.byteLength(line) > MAX_LINE) throw new Error("Pi response exceeds protocol limit");
  process.stdout.write(line + "\n");
};
const models = await ModelRuntime.create({ allowModelNetwork: false });
const runtime = new PiRuntime({ modelRuntime: models, emit: send });
const configured = new Set();
const genericKey = process.env.VIBESOLVE_API_KEY;
delete process.env.VIBESOLVE_API_KEY;
let lastId = 0;

async function receive(line) {
  const request = JSON.parse(line);
  if (!Number.isSafeInteger(request.id) || request.id <= lastId) throw new Error("Non-monotonic Pi request ID");
  lastId = request.id;
  if (genericKey && !configured.has(request.provider)) {
    if (!models.getProvider(request.provider)?.auth.apiKey) {
      send({ type: "result", id: request.id, ok: false, error: "VIBESOLVE_API_KEY is not supported by " +
        request.provider + "; unset it and configure the provider's native Pi authentication" });
      return;
    }
    // Pi's runtime overlay does not persist this key in its credential store.
    await models.setRuntimeApiKey(request.provider, genericKey);
    configured.add(request.provider);
  }
  send(await runtime.run(request));
}
let pending = Buffer.alloc(0);
for await (const chunk of process.stdin) {
  pending = Buffer.concat([pending, chunk]);
  if (pending.length > MAX_LINE) throw new Error("Pi request exceeds protocol limit");
  let newline;
  while ((newline = pending.indexOf(10)) >= 0) {
    const line = pending.subarray(0, newline).toString("utf8");
    pending = pending.subarray(newline + 1);
    await receive(line);
  }
}
if (pending.length) throw new Error("Incomplete Pi request");
