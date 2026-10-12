import assert from "node:assert/strict";
import { test } from "node:test";
import { cleanupSessionResources } from "@earendil-works/pi-ai";
import { PiRuntime } from "./runtime.mjs";
import { streamSimple, getOpenAICodexWebSocketDebugStats, resetOpenAICodexWebSocketDebugStats }
  from "@earendil-works/pi-ai/api/openai-codex-responses";

const model={id:"fixture",name:"fixture",provider:"openai-codex",api:"openai-codex-responses",
  baseUrl:"https://fixture.invalid",reasoning:true,input:["text"],contextWindow:128000,maxTokens:4000,
  thinkingLevelMap:{off:"none",low:"low",medium:"medium",high:"high"},
  cost:{input:1,output:2,cacheRead:.1,cacheWrite:0}};
const request=(overrides={})=>({id:1,provider:model.provider,model:model.id,
  system:"Stable instructions",user:"Current problem",effort:"medium",
  result_schema:{type:"object",properties:{answer:{type:"string"}},required:["answer"]},
  seconds:5,...overrides});
// Synthetic token and server events: exercise the installed SDK without credentials or inference.
const token="offline."+Buffer.from(JSON.stringify({
  "https://api.openai.com/auth":{chatgpt_account_id:"offline-account"},
})).toString("base64url")+".fixture";
function responseEvents() {
  const item={type:"message",role:"assistant",id:"msg_fixture",
    content:[{type:"output_text",text:'{"answer":"done"}',annotations:[]}]};
  return [
    {type:"response.created",response:{id:"resp_fixture"}},
    {type:"response.output_item.done",output_index:0,item},
    {type:"response.completed",response:{id:"resp_fixture",status:"completed",output:[item],
      usage:{input_tokens:100,output_tokens:5,total_tokens:105,input_tokens_details:{cached_tokens:80}}}},
  ];
}
function fixture(t) {
  const sockets=[],runtimes=[],usage=[];
  const original=globalThis.WebSocket;
  globalThis.WebSocket=class extends EventTarget {
    constructor(_url,options) {
      super();
      this.headers=options.headers;
      this.readyState=0;
      this.payloads=[];
      sockets.push(this);
      setImmediate(()=>{
        if(this.readyState===3) return;
        this.readyState=1;
        this.dispatchEvent(new Event("open"));
      });
    }
    send(data) {
      this.payloads.push(JSON.parse(data));
      setImmediate(()=>{
        for(const event of responseEvents()) {
          this.dispatchEvent(new MessageEvent("message",{data:JSON.stringify(event)}));
        }
      });
    }
    close() {
      this.readyState=3;
      this.dispatchEvent(new Event("close"));
    }
  };
  t.mock.method(globalThis,"fetch",async()=>assert.fail("Unexpected network request"));
  function create() {
    const runtime=new PiRuntime({modelRuntime:{getModel:(_provider,id)=>({...model,id})},
      emit:event=>usage.push(event.usage),complete:(chosen,context,options)=>streamSimple(chosen,context,{
        ...options,apiKey:token,fetch:async()=>assert.fail("Unexpected HTTP fallback"),
      }).result()});
    runtimes.push(runtime);
    return runtime;
  }
  t.after(()=>{
    for(const runtime of runtimes) {
      runtime.close();
      cleanupSessionResources(runtime.transportSessionId);
      resetOpenAICodexWebSocketDebugStats(runtime.transportSessionId);
    }
    for(const socket of sockets) socket.close();
    globalThis.WebSocket=original;
  });
  return {create,sockets,usage};
}
test("real Codex SDK reuses a connection across roles, models and retries without history",async t=>{
  const {create,sockets,usage}=fixture(t),runtime=create();
  const requests=[request(),request({id:2,model:"other",system:"Integrator instructions",
    user:"Only accepted files"}),request({id:3,model:"other",system:"Integrator instructions",
    user:"Only accepted files"})];
  for(const input of requests) assert((await runtime.run(input)).ok);
  assert.equal(sockets.length,1);
  for(const [index,body] of sockets[0].payloads.entries()) {
    assert.equal(body.model,requests[index].model);
    assert.equal(body.instructions,requests[index].system);
    assert.equal(body.input.length,1);
    assert.equal(body.input[0].role,"user");
    assert.equal(body.input[0].content[0].text,requests[index].user);
    assert.equal(body.store,false);
    assert(!body.previous_response_id);
    assert(!body.tools?.length);
    assert.equal(body.text.format.strict,true);
    assert.equal(body.reasoning.effort,"medium");
  }
  assert.equal(sockets[0].payloads[1].prompt_cache_key,sockets[0].payloads[2].prompt_cache_key);
  const stats=getOpenAICodexWebSocketDebugStats(runtime.transportSessionId);
  assert.equal(stats.connectionsCreated,1);
  assert.equal(stats.connectionsReused,2);
  assert.equal(stats.fullContextRequests,3);
  assert.equal(stats.cachedContextRequests,0);
  assert.equal(stats.deltaRequests,0);
  // Synthetic usage proves accounting only, not actual provider cache hits.
  assert(usage.every(u=>u.input_tokens===100 && u.cached_input_tokens===80));
});
test("concurrent problems own separate sockets and session affinity",async t=>{
  const {create,sockets}=fixture(t),first=create(),second=create();
  const results=await Promise.all([first.run(request()),second.run(request({user:"Other problem"}))]);
  assert(results.every(result=>result.ok));
  assert.equal(sockets.length,2);
  assert.notEqual(first.transportSessionId,second.transportSessionId);
  assert.equal(sockets[0].payloads[0].prompt_cache_key,first.transportSessionId);
  assert.equal(sockets[1].payloads[0].prompt_cache_key,second.transportSessionId);
});
