import assert from "node:assert/strict";
import { test } from "node:test";
import { ModelRuntime } from "@earendil-works/pi-coding-agent";
import { getSupportedThinkingLevels } from "@earendil-works/pi-ai";
import { BedrockRuntimeClient } from "@aws-sdk/client-bedrock-runtime";
import { streamSimple as anthropic } from "@earendil-works/pi-ai/api/anthropic-messages";
import { streamSimple as bedrock } from "@earendil-works/pi-ai/api/bedrock-converse-stream";
import { streamSimple as google } from "@earendil-works/pi-ai/api/google-generative-ai";
import { streamSimple as vertex } from "@earendil-works/pi-ai/api/google-vertex";
import { streamSimple as mistral } from "@earendil-works/pi-ai/api/mistral-conversations";
import { streamSimple as compatible } from "@earendil-works/pi-ai/api/openai-completions";
import { PiRuntime } from "./runtime.mjs";

const catalog=await ModelRuntime.create({allowModelNetwork:false});
const schema={type:"object",properties:{answer:{type:"string"}},required:["answer"]};
function request(model,id=1) {
  const levels=getSupportedThinkingLevels(model);
  return {id,provider:model.provider,model:model.id,
    effort:levels.includes("off")?"none":levels[0],
    system:"Stable role instructions",user:`Current accepted input ${id}`,
    result_schema:schema,seconds:5};
}
function guardNetwork(t) {
  t.mock.method(globalThis,"fetch",async()=>assert.fail("No network permitted"));
  t.mock.method(BedrockRuntimeClient.prototype,"send",async()=>assert.fail("No AWS calls permitted"));
}
for(const [provider,id,adapter,verify] of [
  ["anthropic","claude-sonnet-4-6",anthropic,(p,long)=>{
    const marker={type:"ephemeral",...(long?{ttl:"1h"}:{})};
    assert.deepEqual(p.system.at(-1).cache_control,marker);
    assert.deepEqual(p.messages.at(-1).content.at(-1).cache_control,marker);
  }],
  ["amazon-bedrock","anthropic.claude-sonnet-4-6",bedrock,(p,long)=>{
    const cachePoint={type:"default",...(long?{ttl:"1h"}:{})};
    assert.deepEqual(p.system.at(-1),{cachePoint});
    assert.deepEqual(p.messages.at(-1).content.at(-1),{cachePoint});
  }],
  ["openrouter","anthropic/claude-sonnet-4.6",compatible,(p,long)=>{
    const marker={type:"ephemeral",...(long?{ttl:"1h"}:{})};
    assert.deepEqual(p.messages[0].content.at(-1).cache_control,marker);
    assert.deepEqual(p.messages.at(-1).content.at(-1).cache_control,marker);
  }],
  // Google caching stays service-controlled; Pi creates no explicit cache resources.
  ...[["google","gemini-3.7-flash",google],["google-vertex","gemini-3.7-flash",vertex]].map(([provider,id,adapter])=>[
      provider,id,adapter,p=>assert(!/"(?:cache_control|cachePoint|cachedContent|prompt_cache_key)"/.test(JSON.stringify(p))),
    ]),
  ["deepseek","deepseek-v4-flash",compatible,(p,long,sessionId)=>{
    assert.equal(p.prompt_cache_key,long?sessionId:undefined);
    assert.equal(p.prompt_cache_retention,long?"24h":undefined);
    assert(!JSON.stringify(p).includes("cache_control"));
  }],
]) {
  test(`${provider}: shared session/cache policy reaches the real Pi adapter`,async t=>{
    guardNetwork(t);
    const model=catalog.getModel(provider,id);
    assert(model);
    const calls=[],payloads=[];
    const runtime=new PiRuntime({modelRuntime:catalog,emit:()=>{},complete:(chosen,context,options)=>{
      calls.push({context,options});
      return adapter(chosen,context,{...options,apiKey:"offline-fixture",
        env:{PI_CACHE_RETENTION:calls.length===1?"short":"long"},
        onPayload:async(payload,selected)=>{
          payloads.push(await options.onPayload?.(payload,selected)??payload);
          throw new Error("Offline payload captured");
        }}).result();
    }});
    t.after(()=>runtime.close());
    for(const id of [1,2]) assert.match((await runtime.run(request(model,id))).error,/Offline payload captured/);
    assert.equal(payloads.length,2);
    for(const [index,{context,options}] of calls.entries()) {
      assert.equal(options.sessionId,runtime.transportSessionId);
      assert(!Object.hasOwn(options,"cacheRetention"),"Pi owns retention preferences");
      assert.equal(options.transport,"websocket");
      assert.equal(context.messages.length,1);
      assert.equal(context.messages[0].content,request(model,index+1).user);
      assert(!context.tools);
      assert(context.systemPrompt.includes(JSON.stringify(schema)));
      verify(payloads[index],index===1,runtime.transportSessionId);
    }
    assert.equal(calls[0].context.systemPrompt,calls[1].context.systemPrompt);
  });
}

test("Mistral: native cache key, affinity header and cached usage survive repeated calls",async t=>{
  guardNetwork(t);
  const model=catalog.getModel("mistral","codestral-latest"),wire=[],usage=[];
  assert(model);
  const runtime=new PiRuntime({modelRuntime:catalog,emit:event=>usage.push(event.usage),
    complete:(chosen,context,options)=>mistral(chosen,context,{...options,apiKey:"offline-fixture",
      fetch:async(_url,init)=>{
        wire.push({body:JSON.parse(init.body),headers:new Headers(init.headers)});
        const chunk={id:"fixture",choices:[{index:0,delta:{content:'{"answer":"done"}'},finish_reason:"stop"}],
          usage:{prompt_tokens:100,completion_tokens:5,total_tokens:105,prompt_tokens_details:{cached_tokens:80}}};
        return new Response("data: "+JSON.stringify(chunk)+"\n\ndata: [DONE]\n\n",
          {headers:{"content-type":"text/event-stream"}});
      }}).result()});
  t.after(()=>runtime.close());
  for(const id of [1,2]) assert((await runtime.run(request(model,id))).ok);
  assert.equal(wire.length,2);
  for(const [index,{body,headers}] of wire.entries()) {
    assert.equal(body.prompt_cache_key,runtime.transportSessionId);
    assert.equal(headers.get("x-affinity"),runtime.transportSessionId);
    assert.equal(body.messages.length,2);
    assert.equal(body.messages[1].content,request(model,index+1).user);
    assert(!body.tools);
    assert.equal(usage[index].input_tokens,100);
    assert.equal(usage[index].cached_input_tokens,80);
  }
});
