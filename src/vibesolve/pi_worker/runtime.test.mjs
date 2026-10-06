import assert from "node:assert/strict";
import { test } from "node:test";
import { PiRuntime } from "./runtime.mjs";
import { streamSimple } from "@earendil-works/pi-ai/api/openai-responses";

const schema = {type:"object",properties:{answer:{type:"string"}},required:["answer"],additionalProperties:false};
const model = {id:"fixture",name:"fixture",provider:"openai",api:"openai-responses",
  baseUrl:"https://fixture.invalid/v1",reasoning:true,input:["text"],contextWindow:128000,maxTokens:4000,
  thinkingLevelMap:{off:"none",low:"low",medium:"medium",high:"high"},
  cost:{input:.2,output:1.2,cacheRead:.02,cacheWrite:.25}};
const request = (overrides={}) => ({id:1,provider:"openai",model:"fixture",
  system:"Original system prompt. No execution overrides.",user:'{"OriginalRequest":"whole","ProjectManifest":{"files":[{"path":"A.java","content":"full source"}]}}',
  effort:"medium",result_schema:schema,seconds:600,...overrides});
const message = (overrides={}) => ({role:"assistant",api:model.api,provider:model.provider,model:model.id,
  content:[{type:"text",text:'{"answer":"done"}'}],stopReason:"stop",timestamp:1,
  usage:{input:10,cacheRead:4,cacheWrite:2,output:3,totalTokens:19,
    cost:{input:.01,output:.004,cacheRead:.002,cacheWrite:.03,total:.046}},...overrides});
function fixture(steps=[message()],chosen=model) {
  const calls=[],events=[];
  const runtime=new PiRuntime({modelRuntime:{getModel:()=>chosen},emit:e=>events.push(e),
    complete:async (model,context,options)=>{
      calls.push({model,context:structuredClone(context),options});
      assert(steps.length,"No extra model calls permitted");
      return steps.shift();
    }});
  return {runtime,calls,events};
}
test("one completion with the full context and no tools",async()=>{
  const {runtime,calls,events}=fixture();
  const input=request(),result=await runtime.run(input);
  assert.equal(result.text,'{"answer":"done"}');
  assert.equal(calls.length,1);
  assert.equal(calls[0].context.systemPrompt,input.system);
  assert.equal(calls[0].context.messages.length,1);
  assert.equal(calls[0].context.messages[0].content,input.user);
  assert(!Object.hasOwn(calls[0].context,"tools"));
  assert.equal(events.length,1);
  assert.equal(events[0].usage.input_tokens,16);
  assert.equal(events[0].usage.cache_write_tokens,2);
  assert.equal(events[0].usage.estimated_cost_usd,.046);
});
test("a later role or retry never inherits conversation history",async()=>{
  const {runtime,calls}=fixture([message(),message(),message()]);
  await runtime.run(request());
  await runtime.run(request({id:2,effort:"high",user:"accepted host snapshot"}));
  await runtime.run(request({id:3,user:"current IO input"}));
  assert(calls.every(c=>c.context.messages.length===1 && !c.context.tools));
  assert.equal(calls[1].context.messages[0].content,"accepted host snapshot");
  assert.equal(calls[1].options.reasoning,"high");
  assert.equal(calls[2].context.messages[0].content,"current IO input");
  assert(calls.every(c=>c.options.sessionId===runtime.transportSessionId));
});
test("strict schema and exact effort reach the real SDK payload without tools",async()=>{
  const {runtime,calls}=fixture();
  await runtime.run(request());
  const call=calls[0],prepare=call.options.onPayload;
  let captured;
  const options={...call.options,apiKey:"offline-fixture",
    fetch:async()=>assert.fail("No network"),onPayload:async(payload,chosen)=>{
      captured=await prepare(payload,chosen);
      throw Error("Offline payload captured");
    }};
  const reply=await streamSimple(model,call.context,options).result();
  assert.match(reply.errorMessage,/Offline payload captured/);
  assert.equal(captured.text.format.type,"json_schema");
  assert.equal(captured.text.format.strict,true);
  assert.equal(captured.text.format.schema.additionalProperties,false);
  assert.equal(captured.reasoning.effort,"medium");
  assert.equal(captured.prompt_cache_key,call.options.sessionId);
  assert(!captured.tools?.length);
});
test("other transports get prompt schema, not an agent or tool loop",async()=>{
  const {runtime,calls}=fixture([message()],{...model,api:"anthropic-messages"});
  assert((await runtime.run(request())).ok);
  assert(calls[0].context.systemPrompt.startsWith(request().system));
  assert(calls[0].context.systemPrompt.includes(JSON.stringify(schema)));
  assert(!calls[0].context.tools);
  assert.equal(calls.length,1);
});
test("tool calls are rejected",async()=>{
  const {runtime,calls,events}=fixture([message({content:[{type:"toolCall",name:"write",arguments:{path:"A.java",content:"no"}}],stopReason:"toolUse"})]);
  const result=await runtime.run(request());
  assert.equal(result.ok,false);
  assert.match(result.error,/Unexpected tool call/);
  assert.equal(calls.length,1);
  assert.equal(events.length,1);
});
test("provider failure still emits usage; zero-usage failure cost is unknown",async()=>{
  const {runtime,calls,events}=fixture([message({stopReason:"error",errorMessage:"rate limited",
    usage:{input:0,cacheRead:0,cacheWrite:0,output:0,totalTokens:0,cost:{total:0}}})]);
  const result=await runtime.run(request());
  assert.equal(result.ok,false);
  assert.match(result.error,/rate limited/);
  assert.equal(calls.length,1);
  assert.equal(events[0].usage.estimated_cost_usd,null);
});
test("empty and truncated text is returned to Python",async()=>{
  for(const m of [message({content:[]}),message({stopReason:"length"})]) {
    const {runtime}=fixture([m]);
    assert((await runtime.run(request())).ok);
  }
});
test("problem budget and unsupported effort fail before inference",async()=>{
  const {runtime,calls}=fixture([]);
  runtime.responses=70;
  assert.match((await runtime.run(request())).error,/budget/);
  runtime.responses=0;
  runtime.completionMs=1800_000;
  assert.match((await runtime.run(request())).error,/budget/);
  runtime.completionMs=0;
  assert.match((await runtime.run(request({effort:"unsupported"}))).error,/does not support effort/);
  assert.equal(calls.length,0);
});
test("the problem budget ignores time between completions",async t=>{
  const {runtime}=fixture([message()]);
  const later=Date.now()+3600_000;
  t.mock.method(Date,"now",()=>later);
  assert((await runtime.run(request())).ok);
});
test("close aborts calls and rejects later ones",async()=>{
  const runtime=new PiRuntime({modelRuntime:{getModel:()=>model},emit:()=>{},
    complete:async(_model,_context,options)=>{
      runtime.close();
      assert(options.signal.aborted);
      return message({stopReason:"aborted"});
    }});
  assert.equal((await runtime.run(request())).ok,false);
  assert.match((await runtime.run(request({id:2}))).error,/closed/);
});
