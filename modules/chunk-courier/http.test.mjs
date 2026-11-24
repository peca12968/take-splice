import assert from "node:assert/strict";
import {spawn} from "node:child_process";
import {createHash} from "node:crypto";
import {once} from "node:events";
import {mkdtemp, rm, writeFile} from "node:fs/promises";
import {createServer} from "node:http";
import {uploadFile} from "./upload.mjs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import {createInterface} from "node:readline";
import {fileURLToPath} from "node:url";
import test from "node:test";

const digest = bytes => createHash("sha256").update(bytes).digest("hex");

async function start(db) {
  const child = spawn(process.execPath, [fileURLToPath(new URL("server.mjs", import.meta.url))], {
    env: {...process.env, PORT:"0", COURIER_DB:db}, stdio:["ignore","pipe","inherit"]
  });
  const exit = once(child,"exit");
  const output = createInterface({input:child.stdout});
  const [url] = await Promise.race([once(output,"line"), exit.then(() => {throw new Error("server exited at startup");})]);
  return {url, stop: async () => {child.kill(); await exit; output.close();}};
}

test("two independent servers replay the same chunk and serve identical sealed bytes after restart", async () => {
  const dir = await mkdtemp(join(tmpdir(),"courier-http-"));
  const db = join(dir,"uploads.sqlite");
  const servers = [];
  try {
    servers.push(await start(db));
    servers.push(await start(db));
    const first = Buffer.from([0,1,2,255]), tail = Buffer.from("收據"), bytes = Buffer.concat([first,tail]);
    const path = "/uploads/receipt";
    const manifest = {size:bytes.length, sha256:digest(bytes)};
    const request = (url, offset, chunk) => fetch(url + path, {method:"PATCH", body:chunk,
      headers:{"upload-offset":String(offset),"chunk-sha256":digest(chunk)}});
    const created = await fetch(servers[0].url + path, {method:"PUT",body:JSON.stringify(manifest)});
    assert.equal(created.status,201);
    const attempts = await Promise.all(Array.from({length:12}, (_,i) => request(servers[i%2].url,0,first)));
    assert.equal(attempts.filter(r => r.status === 201).length,1);
    assert.equal(attempts.filter(r => r.status === 200).length,11);
    await servers.pop().stop();
    await servers.pop().stop();
    servers.push(await start(db));
    const url = servers[0].url;
    assert.equal((await (await fetch(url + path)).json()).offset,first.length);
    assert.equal((await fetch(url + path + "/content")).status,409);
    assert.equal((await request(url,first.length,tail)).status,201);
    assert.equal((await fetch(url + path + "/seal",{method:"POST"})).status,201);
    const file = await fetch(url + path + "/content");
    assert.equal(file.status,200);
    assert.deepEqual(Buffer.from(await file.arrayBuffer()),bytes);
    for (const [range, start, end] of [["bytes=2-5",2,6],["bytes=4-",4,bytes.length],["bytes=-3",bytes.length-3,bytes.length]]) {
      const part = await fetch(url + path + "/content",{headers:{range,"if-range":file.headers.get("etag")}});
      assert.equal(part.status,206);
      assert.equal(part.headers.get("content-range"),`bytes ${start}-${end-1}/${bytes.length}`);
      assert.deepEqual(Buffer.from(await part.arrayBuffer()),bytes.subarray(start,end));
    }
    const changed = await fetch(url + path + "/content",{headers:{range:"bytes=4-","if-range":'"other"'}});
    assert.equal(changed.status,200);
    assert.deepEqual(Buffer.from(await changed.arrayBuffer()),bytes);
    const outside = await fetch(url + path + "/content",{headers:{range:"bytes=999-"}});
    assert.equal(outside.status,416);
    assert.equal(outside.headers.get("content-range"),"bytes */" + bytes.length);
  } finally {
    for (const server of servers) await server.stop();
    await rm(dir,{recursive:true,force:true});
  }
});

test("a lost HTTP acknowledgement resumes at the confirmed offset without resending the committed chunk", async () => {
  const dir = await mkdtemp(join(tmpdir(),"courier-loss-"));
  const service = await start(join(dir,"uploads.sqlite"));
  const offsets = [];
  const proxy = createServer(async (req,res) => {
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const headers = {};
      for (const key of ["upload-offset","chunk-sha256","content-type"]) {
        if (req.headers[key]) headers[key] = req.headers[key];
      }
      const reply = await fetch(service.url + req.url,{method:req.method, headers,
        body:chunks.length ? Buffer.concat(chunks) : undefined});
      const bytes = Buffer.from(await reply.arrayBuffer());
      if (req.method === "PATCH") {
        offsets.push(Number(req.headers["upload-offset"]));
        if (offsets.length === 1 && reply.status === 201) return res.destroy();
      }
      res.writeHead(reply.status);
      res.end(bytes);
    } catch (error) {res.destroy(error);}
  });
  try {
    proxy.listen(0,"127.0.0.1");
    await once(proxy,"listening");
    const base = "http://127.0.0.1:" + proxy.address().port;
    const bytes = Buffer.alloc(2**20 + 73,37);
    const path = join(dir,"fixture.bin");
    await writeFile(path, bytes);
    const result = await uploadFile(base,"sample",path);
    assert.equal(result.sha256,digest(bytes));
    assert.equal(result.reconciliations,1);
    assert.deepEqual(offsets,[0,2**20]);
    assert.deepEqual(Buffer.from(await (await fetch(service.url + "/uploads/sample/content")).arrayBuffer()), bytes);
    assert.equal((await uploadFile(base,"sample",path)).resumedAt,bytes.length);
    assert.deepEqual(offsets,[0,2**20]);
  } finally {
    proxy.closeAllConnections();
    await new Promise(resolve => proxy.close(resolve));
    await service.stop();
    await rm(dir,{recursive:true,force:true});
  }
});
