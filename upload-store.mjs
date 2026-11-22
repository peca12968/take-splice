import {createHash} from "node:crypto";
import {DatabaseSync} from "node:sqlite";

export const maxChunkBytes = 1024 * 1024;
const isDigest = value => typeof value === "string" && /^[0-9a-f]{64}$/.test(value);
const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");

export class UploadError extends Error {
  constructor(status, message, offset) {
    super(message);
    this.status = status;
    this.offset = offset;
  }
}

function checkId(id) {
  if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,80}$/.test(id)) {
    throw new UploadError(400, "invalid upload id");
  }
}

export class UploadStore {
  constructor(path) {
    this.db = new DatabaseSync(path);
    this.db.exec(`
      PRAGMA busy_timeout=5000;
      PRAGMA foreign_keys=ON;
      PRAGMA journal_mode=WAL;
      CREATE TABLE IF NOT EXISTS uploads (
        id TEXT NOT NULL PRIMARY KEY, size INTEGER NOT NULL CHECK(size>=0), sha256 TEXT NOT NULL,
        offset INTEGER NOT NULL DEFAULT 0 CHECK(offset BETWEEN 0 AND size),
        sealed INTEGER NOT NULL DEFAULT 0 CHECK(sealed IN (0,1) AND (sealed=0 OR offset=size))
      ) STRICT;
      CREATE TABLE IF NOT EXISTS chunks (
        upload_id TEXT NOT NULL REFERENCES uploads(id), start INTEGER NOT NULL CHECK(start>=0),
        sha256 TEXT NOT NULL, bytes BLOB NOT NULL CHECK(length(bytes)>0),
        PRIMARY KEY(upload_id,start)
      ) STRICT;
    `);
  }

  close() { this.db.close(); }

  status(id) {
    checkId(id);
    const row = this.db.prepare("SELECT * FROM uploads WHERE id=?").get(id);
    if (!row) throw new UploadError(404, "unknown upload");
    return {...row, sealed: Boolean(row.sealed)};
  }

  transaction(operation) {
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const result = operation();
      this.db.exec("COMMIT");
      return result;
    } catch (error) {
      this.db.exec("ROLLBACK");
      throw error;
    }
  }

  create(id, size, digest) {
    checkId(id);
    if (!Number.isSafeInteger(size) || size < 0 || !isDigest(digest)) {
      throw new UploadError(400, "expected a nonnegative size and lowercase SHA-256");
    }
    return this.transaction(() => {
      const {changes} = this.db.prepare("INSERT INTO uploads(id,size,sha256) VALUES (?,?,?) ON CONFLICT(id) DO NOTHING")
        .run(id, size, digest);
      const upload = this.status(id);
      if (upload.size !== size || upload.sha256 !== digest) throw new UploadError(409, "upload id already in use", upload.offset);
      return {created: Boolean(changes), upload};
    });
  }

  append(id, start, bytes, digest) {
    checkId(id);
    if (!Number.isSafeInteger(start) || start < 0 || !Buffer.isBuffer(bytes) || !bytes.length || !isDigest(digest)) {
      throw new UploadError(400, "invalid offset, empty body or invalid chunk digest");
    }
    if (bytes.length > maxChunkBytes) throw new UploadError(413, "chunk exceeds one MiB");
    if (sha256(bytes) !== digest) throw new UploadError(422, "chunk checksum mismatch");
    return this.transaction(() => {
      const upload = this.status(id);
      if (start < upload.offset) {
        const prior = this.db.prepare("SELECT sha256,length(bytes) AS size FROM chunks WHERE upload_id=? AND start=?").get(id, start);
        if (prior?.sha256 === digest && prior.size === bytes.length) return {created: false, upload};
        throw new UploadError(409, "previously committed bytes differ or start is inside a chunk", upload.offset);
      }
      if (upload.sealed || start !== upload.offset || bytes.length > upload.size - start) {
        throw new UploadError(409, "chunk exceeds file size or does not start at the next offset", upload.offset);
      }
      this.db.prepare("INSERT INTO chunks VALUES (?,?,?,?)").run(id, start, digest, bytes);
      this.db.prepare("UPDATE uploads SET offset=offset+? WHERE id=?").run(bytes.length, id);
      return {created: true, upload: this.status(id)};
    });
  }

  seal(id) {
    const upload = this.status(id);
    if (upload.sealed) return {created: false, upload};
    if (upload.offset !== upload.size) throw new UploadError(409, "upload is incomplete", upload.offset);
    // Accepted chunks are immutable. At full length, no new chunk can be appended.
    const hash = createHash("sha256");
    for (const {bytes} of this.db.prepare("SELECT bytes FROM chunks WHERE upload_id=? ORDER BY start").iterate(id)) {
      hash.update(bytes);
    }
    if (hash.digest("hex") !== upload.sha256) throw new UploadError(422, "whole-file checksum mismatch", upload.offset);
    const {changes} = this.db.prepare("UPDATE uploads SET sealed=1 WHERE id=? AND sealed=0").run(id);
    return {created: Boolean(changes), upload: this.status(id)};
  }

  sealedUpload(id) {
    const upload = this.status(id);
    if (!upload.sealed) throw new UploadError(409, "seal the upload before reading", upload.offset);
    return upload;
  }

  *parts(id, start, end) {
    const rows = this.db.prepare(`
      SELECT start,bytes FROM chunks WHERE upload_id=? AND start >=
        (SELECT MAX(start) FROM chunks WHERE upload_id=? AND start<=?) AND start < ? ORDER BY start
    `).iterate(id,id,start,end);
    for (const row of rows) {
      yield row.bytes.subarray(Math.max(0,start-row.start),end-row.start);
    }
  }
}
