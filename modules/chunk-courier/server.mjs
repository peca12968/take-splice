import {createServer} from "node:http";
import {mkdir} from "node:fs/promises";
import {dirname} from "node:path";
import {Readable} from "node:stream";
import {pipeline} from "node:stream/promises";
import {UploadError, UploadStore, maxChunkBytes} from "./upload-store.mjs";

const database = process.env.COURIER_DB ?? ".courier/uploads.sqlite";
await mkdir(dirname(database), {recursive:true});
const store = new UploadStore(database);

function singleRange(header, size) {
  const match = /^bytes=(\d*)-(\d*)$/i.exec(header);
  if (!match || (!match[1] && !match[2])) return null;
  const total = BigInt(size);
  const last = match[2] ? BigInt(match[2]) : null;
  const first = match[1] ? BigInt(match[1]) : null;
  if (first !== null && last !== null && first > last) return null;
  if (total === 0n || (first !== null && first >= total) || (first === null && last === 0n)) {
    throw new UploadError(416, "range outside sealed content");
  }
  if (first === null) return [last >= total ? 0 : Number(total-last),size];
  return [Number(first), last === null || last >= total ? size : Number(last+1n)];
}

function json(response, status, body) {
  response.writeHead(status, {"content-type":"application/json; charset=utf-8", "cache-control":"no-store"});
  response.end(JSON.stringify(body));
}

async function body(request, limit) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request.iterator({destroyOnReturn:false})) {
    size += chunk.length;
    if (size > limit) {
      request.resume();
      throw new UploadError(413, "request body too large");
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks, size);
}

const server = createServer(async (request, response) => {
  try {
    const match = /^\/uploads\/([A-Za-z0-9_-]{1,80})(?:\/(seal|content))?$/.exec(request.url);
    if (!match) throw new UploadError(404, "unknown resource");
    const [,id,subresource] = match;
    if (request.method === "GET" && subresource === "content") {
      const upload = store.sealedUpload(id);
      const etag = `"${upload.sha256}"`;
      const validator = request.headers["if-range"];
      let range = null;
      if (request.headers.range && (validator === undefined || validator === etag)) {
        try { range = singleRange(request.headers.range, upload.size); }
        catch (error) {response.setHeader("content-range", "bytes */" + upload.size); throw error;}
      }
      const [start,end] = range ?? [0,upload.size];
      if (range) response.setHeader("content-range", `bytes ${start}-${end-1}/${upload.size}`);
      response.writeHead(range ? 206 : 200, {"content-type":"application/octet-stream", "content-length":end-start,
        "content-disposition":"attachment", "accept-ranges":"bytes", etag});
      await pipeline(Readable.from(store.parts(id,start,end), {objectMode:false}), response);
      return;
    }
    if (request.method === "GET" && !subresource) return json(response, 200, store.status(id));
    let result;
    if (request.method === "PUT" && !subresource) {
      const bytes = await body(request, 4096);
      let manifest;
      try { manifest = JSON.parse(bytes.toString("utf8")); }
      catch { throw new UploadError(400, "invalid JSON manifest"); }
      result = store.create(id, manifest?.size, manifest?.sha256);
    } else if (request.method === "PATCH" && !subresource) {
      const offset = request.headers["upload-offset"];
      if (typeof offset !== "string" || !/^(0|[1-9][0-9]*)$/.test(offset)) throw new UploadError(400, "invalid Upload-Offset");
      result = store.append(id, Number(offset), await body(request, maxChunkBytes), request.headers["chunk-sha256"]);
    } else if (request.method === "POST" && subresource === "seal") {
      result = store.seal(id);
    } else {
      response.setHeader("allow", subresource === "seal" ? "POST" : subresource === "content" ? "GET" : "GET, PUT, PATCH");
      throw new UploadError(405, "method not allowed");
    }
    json(response, result.created ? 201 : 200, result.upload);
  } catch (error) {
    if (response.headersSent || response.destroyed) return response.destroy();
    if (error instanceof UploadError) return json(response, error.status, {error:error.message, offset:error.offset});
    console.error(error);
    json(response, 500, {error:"upload store unavailable"});
  }
});

server.listen(Number(process.env.PORT ?? 4188), "127.0.0.1", () => {
  console.log(`http://127.0.0.1:${server.address().port}`);
});
