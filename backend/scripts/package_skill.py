import argparse
from io import BytesIO
from pathlib import Path
import sys
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.skill_validation import inspect_archive, MAX_EXPANDED


def main():
    parser = argparse.ArgumentParser(description="Package and validate a custom transformation skill")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_dir(): parser.error("Source must be a skill directory")
    output, count, total = BytesIO(), 0, 0
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(source.rglob("*")):
            if path.is_symlink(): raise ValueError("Symlinks are not allowed")
            if not path.is_file(): continue
            count += 1; total += path.stat().st_size
            if count > 100 or total > MAX_EXPANDED: raise ValueError("Package size/file limit exceeded")
            archive.write(path, path.relative_to(source).as_posix())
    payload = output.getvalue()
    manifest, _, digest = inspect_archive(payload)
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    with args.destination.open("xb") as file: file.write(payload)
    print(f"Packaged {manifest.name} {manifest.version}: sha256={digest}")


if __name__ == "__main__": main()
