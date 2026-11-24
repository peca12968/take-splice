"""Pack eight moving parcel frames and one blank frame, then replay the atlas."""
import argparse
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

from PIL import Image, ImageDraw


def demo(executable, directory, size=48, dedupe=False):
    directory.mkdir()
    sources = []
    for i in range(9):
        frame = Image.new("RGBA", (64, 64))
        if i < 8:
            x, y = 3 + i * 6, 22 + abs(4-i)
            draw = ImageDraw.Draw(frame)
            draw.rectangle((x, y, x+8, y+8), fill=(188, 118, 54, 255))
            draw.line((x+4, y, x+4, y+4), fill=(244, 211, 141, 255))
            draw.line((x+1, y+10, x+7, y+10), fill=(28, 38, 45, 96))
        path = directory / f"parcel-{i}.png"
        frame.save(path)
        sources.append(path)
    output = directory / "parcel.zip"
    flags = ["-dedupe"] if dedupe else []
    subprocess.run([executable, *flags, "-size", str(size), "-out", output, *sources],
                   check=True, stdout=subprocess.PIPE)
    strip = Image.new("RGBA", (64 * len(sources), 64))
    with ZipFile(output) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        for i, frame in enumerate(manifest["frames"]):
            restored = Image.new("RGBA", tuple(frame["source_size"]))
            if frame["page"] >= 0:
                x, y, w, h = frame["atlas"]
                with archive.open(manifest["pages"][frame["page"]]) as data, Image.open(data) as page:
                    restored.paste(page.convert("RGBA").crop((x,y,x+w,y+h)), tuple(frame["offset"]))
            with Image.open(directory / frame["name"]) as source:
                if restored.tobytes() != source.tobytes():
                    raise ValueError(f"{frame['name']} differs from source")
            strip.paste(restored, (64*i, 0))
    strip.save(directory / "replayed.png")
    return {"frames": len(sources), "source_pixels": len(sources)*64*64,
            "pages": len(manifest["pages"]), "atlas_pixels": len(manifest["pages"])*size*size}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("new_directory", type=Path)
    parser.add_argument("--size", type=int, default=48)
    parser.add_argument("--dedupe", action="store_true")
    args = parser.parse_args()
    print(json.dumps(demo(args.executable.resolve(), args.new_directory, args.size, args.dedupe)))
