import assert from "node:assert/strict";
import {createHash} from "node:crypto";
import {mkdtempSync, rmSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
import test from "node:test";
import {UploadStore} from "./upload-store.mjs";

const digest = data => createHash("sha256").update(data).digest("hex");
const put = (store, start, data) => store.append("demo", start, Buffer.from(data), digest(data));

test("resume after restart, replay a lost acknowledgement and seal exactly once", () => {
  const dir = mkdtempSync(join(tmpdir(), "courier-"));
  const path = join(dir, "chunks.sqlite");
  let store = new UploadStore(path);
  try {
    const manifest = ["demo", 7, digest("ABCDEFG")];
    assert.equal(store.create(...manifest).created, true);
    assert.equal(put(store, 0, "ABC").upload.offset, 3);
    store.close();
    store = new UploadStore(path);
    assert.equal(store.create(...manifest).upload.offset, 3);
    assert.equal(put(store, 0, "ABC").created, false);
    for (const [offset, data] of [[0,"XYZ"],[1,"B"],[4,"E"]]) {
      assert.throws(() => put(store, offset, data), {status:409, offset:3});
    }
    assert.throws(() => store.seal("demo"), {status:409, offset:3});
    assert.equal(put(store, 3, "DEFG").upload.offset, 7);
    assert.equal(store.seal("demo").created, true);
    assert.equal(store.seal("demo").created, false);
    assert.equal(put(store, 0, "ABC").upload.sealed, true);
  } finally {
    store.close();
    rmSync(dir, {recursive:true,force:true});
  }
});

test("rejects mismatched chunk and whole-file checksums, handles empty files", () => {
  const store = new UploadStore(":memory:");
  try {
    store.create("demo",3,digest("ABC"));
    assert.throws(() => store.append("demo",0,Buffer.from("XYZ"),digest("ABC")), {status:422});
    assert.equal(store.status("demo").offset,0);
    put(store,0,"XYZ");
    assert.throws(() => store.seal("demo"), {status:422});
    assert.equal(store.status("demo").sealed,false);
    store.create("empty",0,digest(""));
    assert.equal(store.seal("empty").upload.sealed,true);
  } finally {
    store.close();
  }
});
