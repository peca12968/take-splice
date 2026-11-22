"""Pack, inspect and recover fixed 4+2 erasure-coded archives with a trusted manifest."""
import argparse
from functools import cache
import hashlib
import json
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile


BLOCK = 16 * 1024
COEFFICIENTS = (1, 2, 4, 8)
SLOTS = ("d0", "d1", "d2", "d3", "p", "q")
FORMAT = "shard-rescue-4+2-v1"


def multiply(a, b):
    result = 0
    while b:
        if b & 1:
            result ^= a
        a <<= 1
        if a & 0x100:
            a ^= 0x11D
        b >>= 1
    return result


@cache
def table(coefficient):
    return bytes(multiply(coefficient, i) for i in range(256))


def divide(block, coefficient):
    return block.translate(table(table(coefficient).index(1)))


def xor(blocks):
    value = 0
    for block in blocks:
        value ^= int.from_bytes(block, "little")
    return value.to_bytes(BLOCK, "little")


def syndromes(data):
    p = xor(block for block in data if block is not None)
    q = xor(block.translate(table(c)) for c, block in zip(COEFFICIENTS, data)
            if block is not None)
    return p, q


def recover_data(shards):
    data = shards[:4]
    missing = [index for index, block in enumerate(data) if block is None]
    if not missing:
        return data
    known_p, known_q = syndromes(data)
    p, q = shards[4:]
    i = missing[0]
    if len(missing) == 1:
        if p is not None:
            data[i] = xor((p, known_p))
        else:
            data[i] = divide(xor((q, known_q)), COEFFICIENTS[i])
    else:
        j = missing[1]
        # x + y = residual_p, a*x + b*y = residual_q in GF(256).
        residual_p = xor((p, known_p))
        residual_q = xor((q, known_q))
        a, b = COEFFICIENTS[i], COEFFICIENTS[j]
        data[i] = divide(xor((residual_q, residual_p.translate(table(b)))), a ^ b)
        data[j] = xor((residual_p, data[i]))
    return data


def digest(block):
    return hashlib.sha256(block).hexdigest()


def part(archive, stripe, slot):
    return archive / slot / f"{stripe:08d}.bin"


def pack(source, archive):
    rows = []
    size = 0
    whole = hashlib.sha256()
    with source.open("rb") as stream:
        archive.mkdir()
        for slot in SLOTS:
            (archive / slot).mkdir()
        while chunk := stream.read(4 * BLOCK):
            size += len(chunk)
            whole.update(chunk)
            padded = chunk.ljust(4 * BLOCK, b"\0")
            data = [padded[i:i + BLOCK] for i in range(0, len(padded), BLOCK)]
            row = []
            for slot, block in zip(SLOTS, [*data, *syndromes(data)], strict=True):
                part(archive, len(rows), slot).write_bytes(block)
                row.append(digest(block))
            rows.append(row)
    manifest = {"format": FORMAT, "size": size, "sha256": whole.hexdigest(), "stripes": rows}
    # Publish the manifest last, after all shard files are closed.
    with (archive / "manifest.tmp").open("x", encoding="utf-8") as out:
        json.dump(manifest, out)
    (archive / "manifest.tmp").rename(archive / "manifest.json")
    return {"bytes": size, "stripes": len(rows), "sha256": whole.hexdigest()}


def is_hash(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def load_manifest(archive):
    record = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("format") != FORMAT:
        raise ValueError("unsupported archive format")
    size = record.get("size")
    rows = record.get("stripes")
    if (type(size) is not int or size < 0 or not is_hash(record.get("sha256"))
            or not isinstance(rows, list) or len(rows) != (size + 4 * BLOCK - 1) // (4 * BLOCK)):
        raise ValueError("invalid archive metadata")
    if any(not isinstance(row, list) or len(row) != len(SLOTS)
           or not all(is_hash(item) for item in row) for row in rows):
        raise ValueError("invalid shard digests")
    return record


def inspect_shard(path, expected):
    try:
        with path.open("rb") as stream:
            block = stream.read(BLOCK + 1)
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"
    if len(block) != BLOCK:
        return None, "wrong_length"
    if digest(block) != expected:
        return None, "digest_mismatch"
    return block, None


def scan_stripe(archive, stripe, row):
    shards = []
    faults = {}
    for slot, expected in zip(SLOTS, row, strict=True):
        block, reason = inspect_shard(part(archive, stripe, slot), expected)
        shards.append(block)
        if reason:
            faults[slot] = reason
    return shards, faults


def scrub(archive):
    record = load_manifest(archive)
    issues = []
    status = "healthy"
    for index, row in enumerate(record["stripes"]):
        _, faults = scan_stripe(archive, index, row)
        if faults:
            recoverable = len(faults) <= 2
            issues.append({"stripe": index, "faults": faults, "recoverable": recoverable})
            if not recoverable:
                status = "unrecoverable"
            elif status == "healthy":
                status = "degraded"
    return {"status": status, "stripes": len(record["stripes"]), "issues": issues}


def check_destination(archive, output):
    archive_root = archive.resolve()
    if output.parent.resolve().is_relative_to(archive_root) or output.resolve().is_relative_to(archive_root):
        raise ValueError("destination must be outside the archive")


def require_recoverable(index, lost):
    if lost > 2:
        raise ValueError(f"stripe {index}: {lost} unusable shards, at most 2 recoverable")


def rebuild_volume(archive, slot, output):
    position = SLOTS.index(slot)
    check_destination(archive, output)
    rows = load_manifest(archive)["stripes"]
    output.mkdir()  # Reserve the spare exclusively, even if another rebuild is running.
    copied = rebuilt = 0
    written = []
    try:
        for index, row in enumerate(rows):
            source = part(archive, index, slot)
            block, _ = inspect_shard(source, row[position])
            if block is None:
                shards, faults = scan_stripe(archive, index, row)
                require_recoverable(index, len(faults))
                data = recover_data(shards)
                block = data[position] if position < 4 else syndromes(data)[position - 4]
                if digest(block) != row[position]:
                    raise ValueError(f"stripe {index}: reconstructed shard digest mismatch")
                rebuilt += 1
            else:
                copied += 1
            target = output / source.name
            with target.open("xb") as stream:
                written.append(target)
                stream.write(block)
    except BaseException:
        for target in written:
            target.unlink(missing_ok=True)
        output.rmdir()
        raise
    return {"slot": slot, "copied": copied, "rebuilt": rebuilt, "bytes": len(rows) * BLOCK}


def restore(archive, output):
    check_destination(archive, output)
    record = load_manifest(archive)
    whole = hashlib.sha256()
    remaining = record["size"]
    erasures = 0
    tmp = None
    try:
        with NamedTemporaryFile(dir=output.parent, delete=False) as out:
            tmp = Path(out.name)
            for index, row in enumerate(record["stripes"]):
                shards, faults = scan_stripe(archive, index, row)
                lost = len(faults)
                require_recoverable(index, lost)
                erasures += lost
                data = recover_data(shards)
                for i, block in enumerate(data):
                    if shards[i] is None and digest(block) != row[i]:
                        raise ValueError(f"stripe {index}: reconstructed shard digest mismatch")
                chunk = b"".join(data)[:remaining]
                out.write(chunk)
                whole.update(chunk)
                remaining -= len(chunk)
        if remaining or whole.hexdigest() != record["sha256"]:
            raise ValueError("restored file digest or length mismatch")
        os.replace(tmp, output)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)
    return {"bytes": record["size"], "unusable_shards": erasures, "sha256": whole.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    encode = commands.add_parser("pack")
    encode.add_argument("source", type=Path)
    encode.add_argument("archive", type=Path)
    decode = commands.add_parser("restore")
    decode.add_argument("archive", type=Path)
    decode.add_argument("output", type=Path)
    inspect = commands.add_parser("scrub", help="audit every shard against the manifest")
    inspect.add_argument("archive", type=Path)
    rebuild = commands.add_parser("rebuild-volume", help="build one replacement volume in a new directory")
    rebuild.add_argument("archive", type=Path)
    rebuild.add_argument("slot", choices=SLOTS)
    rebuild.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "pack":
            result = pack(args.source, args.archive)
        elif args.command == "restore":
            result = restore(args.archive, args.output)
        elif args.command == "rebuild-volume":
            result = rebuild_volume(args.archive, args.slot, args.output)
        else:
            result = scrub(args.archive)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{parser.prog}: {error}\n")
    print(json.dumps(result))
    if args.command == "scrub":
        return {"healthy": 0, "degraded": 2, "unrecoverable": 3}[result["status"]]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
