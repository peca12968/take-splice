import {createHash} from "node:crypto";
import {open} from "node:fs/promises";
import {pathToFileURL} from "node:url";

const blockSize = 1024 * 1024;

async function readBlock(file, offset, length) {
  const block = Buffer.allocUnsafe(length);
  let filled = 0;
  while (filled < length) {
    const {bytesRead} = await file.read(block, filled, length - filled, offset + filled);
    if (!bytesRead) throw new Error("source file changed during upload");
    filled += bytesRead;
  }
  return block;
}

class HttpError extends Error {
  constructor(status, message) {
    super(`HTTP ${status}: ${message}`);
    this.status = status;
  }
}

async function state(url, options) {
  const reply = await fetch(url, {...options, redirect:"error", signal:AbortSignal.timeout(30000)});
  if (!reply.ok) throw new HttpError(reply.status, await reply.text());
  return reply.json();
}

function transient(error) {
  return error instanceof TypeError || error.name === "TimeoutError" || [502,503,504].includes(error.status);
}

async function retry(operation) {
  for (let attempt = 0; ; attempt++) {
    try { return await operation(); }
    catch (error) {
      if (!transient(error) || attempt >= 2) throw error;
    }
  }
}

export async function uploadFile(base, id, path) {
  if (!/^[A-Za-z0-9_-]{1,80}$/.test(id)) throw new Error("invalid upload id");
  const url = new URL("/uploads/" + id, base);
  const file = await open(path, "r");
  try {
    const {size} = await file.stat();
    const hash = createHash("sha256");
    for (let at = 0; at < size; at += blockSize) {
      hash.update(await readBlock(file, at, Math.min(blockSize, size-at)));
    }
    const digest = hash.digest("hex");
    function check(row) {
      if (row?.id !== id || row.size !== size || row.sha256 !== digest ||
          !Number.isSafeInteger(row.offset) || row.offset < 0 || row.offset > size ||
          typeof row.sealed !== "boolean" || (row.sealed && row.offset !== size)) {
        throw new Error("server returned inconsistent upload state");
      }
      return row;
    }
    let row = check(await retry(() => state(url, {method:"PUT", headers:{"content-type":"application/json"},
      body:JSON.stringify({size,sha256:digest})})));
    const resumedAt = row.offset;
    let reconciliations = 0;
    let stalled = 0;
    while (row.offset < size) {
      const start = row.offset;
      const bytes = await readBlock(file, start, Math.min(blockSize, size-start));
      try {
        row = check(await state(url, {method:"PATCH", headers:{"upload-offset":String(start),
          "chunk-sha256":createHash("sha256").update(bytes).digest("hex")}, body:bytes}));
        if (row.offset < start + bytes.length) throw new Error("server did not commit the complete chunk");
      } catch (error) {
        if (!transient(error) && error.status !== 409) throw error;
        row = check(await retry(() => state(url)));
        reconciliations++;
        if (row.offset < start || (row.offset === start && ++stalled >= 3)) throw error;
      }
      if (row.offset > start) stalled = 0;
    }
    if (!row.sealed) row = check(await retry(() => state(url + "/seal", {method:"POST"})));
    if (!row.sealed) throw new Error("server did not seal the file");
    return {id, size, sha256:digest, resumedAt, reconciliations, sealed:true};
  } finally {
    await file.close();
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const args = process.argv.slice(2);
    if (args.length !== 3) throw new Error("usage: node upload.mjs BASE_URL UPLOAD_ID FILE");
    console.log(JSON.stringify(await uploadFile(...args)));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
