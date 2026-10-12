import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { test } from "node:test";

// Load the real worker with a credential-free model registry.
const preload="data:text/javascript,"+encodeURIComponent(`
  import { ModelRuntime } from ${JSON.stringify(import.meta.resolve("@earendil-works/pi-coding-agent"))};
  ModelRuntime.create = async () => ({getModel: () => undefined, getProvider: () => undefined,
    completeSimple: () => { throw new Error("No inference permitted"); }});
`);
function start(t,env={}) {
  const child=spawn(process.execPath,["--import",preload,new URL("./worker.mjs",import.meta.url).pathname],
    {env:{...process.env,VIBESOLVE_API_KEY:"",...env},stdio:["pipe","pipe","pipe"]});
  t.after(()=>{ if(child.exitCode===null && child.signalCode===null) child.kill("SIGKILL"); });
  let stderr="";
  child.stderr.setEncoding("utf8").on("data",data=>{stderr+=data;});
  const closed=once(child,"close");
  const ready=once(child.stdout,"data");
  child.stdin.write(JSON.stringify({id:1,provider:"offline",model:"missing",system:"s",user:"u",
    result_schema:{type:"object"},seconds:1})+"\n");
  const result=Promise.race([ready,closed.then(()=>{throw new Error(stderr);})])
    .then(output=>JSON.parse(output[0].toString()));
  return {child,closed,result};
}
for(const [ending,code] of [["EOF",0],["malformed",1],["SIGTERM",143],["SIGINT",130]]) {
  test(`worker exits on ${ending}`,{timeout:10000},async t=>{
    const {child,closed,result}=start(t);
    assert.equal((await result).ok,false);
    if(ending==="EOF") child.stdin.end();
    else if(ending==="malformed") child.stdin.end("not-json\n");
    else child.kill(ending);
    assert.deepEqual(await closed,[code,null]);
  });
}
test("an unsupported generic key is reported as a result",{timeout:10000},async t=>{
  const {child,closed,result}=start(t,{VIBESOLVE_API_KEY:"offline-key"});
  const reply=await result;
  assert.equal(reply.ok,false);
  assert.match(reply.error,/VIBESOLVE_API_KEY is not supported by offline/);
  child.stdin.end();
  assert.deepEqual(await closed,[0,null]);
});
