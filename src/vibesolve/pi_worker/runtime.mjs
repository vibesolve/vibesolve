import { getSupportedThinkingLevels } from "@earendil-works/pi-ai";

export function stageStreamOptions(model, effort, options = {}) {
  const level = effort === "none" ? "off" : effort;
  if (effort !== "auto" && !getSupportedThinkingLevels(model).includes(level)) {
    throw new Error(`Pi model ${model.provider}/${model.id} does not support effort=${effort}`);
  }
  const selected = { ...options, reasoning: effort === "auto" || effort === "none" ? undefined : effort };
  const responses = ["openai-responses", "openai-codex-responses"].includes(model.api);
  if (!responses) {
    if (model.reasoning && effort === "auto") {
      // Simple adapters otherwise turn an omitted effort into explicit off.
      const omit = {
        "anthropic-messages": params => { delete params.thinking; delete params.output_config?.effort; },
        "google-generative-ai": params => { delete params.config?.thinkingConfig; },
      }[model.api];
      if (!omit) throw new Error(`Omitted reasoning is not yet verified for Pi API ${model.api}; configure an explicit effort`);
      return { ...selected, onPayload: async (payload, chosen) => {
        const params = await options.onPayload?.(payload, chosen) ?? payload;
        omit(params);
        return params;
      } };
    }
    return selected;
  }
  return {
    ...selected,
    onPayload: async (payload, chosen) => {
      const params = await options.onPayload?.(payload, chosen) ?? payload;
      // Pi Simple treats undefined differently between these two transports.
      if (effort === "auto" || !model.reasoning) delete params.reasoning;
      else params.reasoning = { ...params.reasoning, effort };
      if (model.reasoning && effort !== "none") {
        params.include = [...new Set([...(params.include ?? []), "reasoning.encrypted_content"])];
      }
      return params;
    },
  };
}

export class PiRuntime {
  constructor({ modelRuntime, emit, complete }) {
    this.models = modelRuntime;
    this.emit = emit;
    this.complete = complete ?? modelRuntime.completeSimple.bind(modelRuntime);
    this.responses = 0;
    this.input = 0;
    this.output = 0;
    this.completionMs = 0;
  }

  async run(request) {
    try {
      if (this.responses >= 70 || this.input >= 1_500_000 || this.output >= 100_000 ||
          this.completionMs >= 1800_000) throw new Error("Pi problem budget exhausted");
      const model = this.models.getModel(request.provider, request.model);
      if (!model) throw new Error("Pi has no configured model " + request.provider + "/" + request.model);
      const native = ["openai-responses", "openai-codex-responses"].includes(model.api);
      const schema = request.result_schema;
      const systemPrompt = native ? request.system : request.system.trimEnd() +
        "\n\nReturn exactly one JSON object matching this JSON Schema. No prose or markdown fences.\n" + JSON.stringify(schema);
      const options = stageStreamOptions(model, request.effort, {
        signal: AbortSignal.timeout(request.seconds * 1000),
        maxRetries: 2,
        onPayload: native ? payload => ({ ...payload, text: { ...payload.text,
          format: { type: "json_schema", name: "response", strict: true, schema },
        } }) : undefined,
      });
      const began = Date.now();
      let message;
      try {
        message = await this.complete(model, { systemPrompt,
          messages: [{ role: "user", content: request.user, timestamp: Date.now() }] }, options);
      } finally {
        this.completionMs += Date.now() - began;
      }
      const u = message.usage;
      const rates = this.models.getModel(message.provider, message.model)?.cost;
      const metered = u.totalTokens > 0 || !["error", "aborted"].includes(message.stopReason);
      const priced = rates && Object.values(rates).some(n => n > 0);
      const usage = { provider: message.provider, model: message.model,
        input_tokens: u.input + u.cacheRead + u.cacheWrite,
        cached_input_tokens: u.cacheRead, cache_write_tokens: u.cacheWrite,
        output_tokens: u.output, estimated_cost_usd: priced && metered ? u.cost.total : null,
        stop_reason: message.stopReason };
      this.responses++;
      this.input += usage.input_tokens;
      this.output += usage.output_tokens;
      this.emit({ type: "usage", id: request.id, usage });
      if (["error", "aborted"].includes(message.stopReason)) {
        throw new Error(message.errorMessage || "Pi provider failed");
      }
      if (message.content.some(part => part.type === "toolCall")) {
        throw new Error("Unexpected tool call in a completion-only response");
      }
      return { type: "result", id: request.id, ok: true,
        text: message.content.filter(part => part.type === "text").map(part => part.text).join("") };
    } catch (error) {
      return { type: "result", id: request.id, ok: false, error: error.message || "Pi completion failed" };
    }
  }
}
