"""Plan one-layer Base64 recovery; apply only after validating every source."""
import argparse
import ast
import base64
import binascii
import csv
import gzip
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zlib
from pathlib import Path

VERSION = 1
FORMATS = ("csv.gz", "py", "json", "csv", "txt")

def reject_constant(value: str):
    raise ValueError(f"non-finite JSON number: {value}")


def finite_float(value: str) -> float:
    number = float(value)
    return number if math.isfinite(number) else reject_constant(value)


def file_format(path: Path) -> str:
    return next((fmt for fmt in FORMATS if path.name.endswith("." + fmt)), "")


def valid_format(data: bytes, fmt: str) -> bool:
    try:
        if fmt == "csv.gz":
            data = gzip.decompress(data)
        text = data.decode("utf-8-sig")
        if not text.strip():
            return False
        if fmt == "py":
            ast.parse(text)
        elif fmt == "json":
            json.loads(text, parse_constant=reject_constant, parse_float=finite_float)
        elif fmt in ("csv", "csv.gz", "txt"):
            if re.search(r"^#\s*code\b", text, re.MULTILINE):
                return True
            for delimiter in (",", "|"):
                rows = csv.reader(io.StringIO(text), delimiter=delimiter, strict=True)
                if max((len(row) if all(cell.strip() for cell in row) else 0 for row in rows), default=0) > 1:
                    return True
            return False
        else:
            return False
        return True
    except (ValueError, SyntaxError, UnicodeError, OSError, EOFError, csv.Error, zlib.error):
        return False


def decode_candidate(data: bytes, fmt: str) -> bytes:
    if valid_format(data, fmt):
        raise ValueError("already valid original format")
    try:
        compact = b"".join(data.decode("ascii").encode("ascii").split())
        decoded = base64.b64decode(compact, validate=True)
    except (UnicodeError, binascii.Error) as exc:
        raise ValueError("not strict ASCII Base64") from exc
    if not compact or base64.b64encode(decoded) != compact or not valid_format(decoded, fmt):
        raise ValueError("noncanonical Base64 or unknown decoded format")
    return decoded


def canonical_root(root: Path) -> Path:
    root = Path(root).absolute()
    if root.resolve(strict=True) != root or not root.is_dir():
        raise ValueError("root must be a canonical directory without symlinks")
    return root

def safe_path(root: Path, relative: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid relative path")
    parts = relative.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError("unsafe relative path")
    path = root
    for part in parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("symlink is not allowed")
    if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
        raise ValueError("source must be a regular file inside root")
    return path


def metadata(relative: str, source: bytes, decoded: bytes, fmt: str) -> dict:
    return {"relative_path": relative, "source_sha256": hashlib.sha256(source).hexdigest(),
            "decoded_sha256": hashlib.sha256(decoded).hexdigest(),
            "original_size": len(source), "decoded_size": len(decoded), "format": fmt}


def build_plan(root: Path) -> dict:
    root = canonical_root(root)
    plan = {"version": VERSION, "root": str(root), "items": [], "unknown": []}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                plan["unknown"].append({"relative_path": relative, "reason": "symlink"})
                continue
            fmt = file_format(path)
            if not path.is_file() or not fmt:
                continue
            source = safe_path(root, relative).read_bytes()
            if valid_format(source, fmt):
                continue
            try:
                decoded = decode_candidate(source, fmt)
            except ValueError as exc:
                plan["unknown"].append({"relative_path": relative, "reason": str(exc)})
                continue
            plan["items"].append(metadata(relative, source, decoded, fmt))
        dirs[:] = sorted(name for name in dirs if not (Path(directory) / name).is_symlink())
    return plan

def checked_item(root: Path, item: dict) -> tuple[Path, bytes]:
    path = safe_path(root, item["relative_path"])
    source = path.read_bytes()
    decoded = decode_candidate(source, file_format(path))
    if item != metadata(item["relative_path"], source, decoded, file_format(path)):
        raise ValueError("source hash, decoded hash, size or format mismatch")
    return path, decoded

def apply_plan(plan: dict, root: Path | None = None) -> None:
    if not isinstance(plan, dict) or type(plan.get("version")) is not int or plan["version"] != VERSION:
        raise ValueError("unsupported plan version")
    bound = plan.get("root")
    if not isinstance(bound, str) or not Path(bound).is_absolute():
        raise ValueError("plan root must be absolute")
    target = canonical_root(Path(bound))
    if str(target) != bound or (root is not None and canonical_root(root) != target):
        raise ValueError("plan root mismatch")
    if not isinstance(plan.get("items"), list) or not isinstance(plan.get("unknown"), list):
        raise ValueError("invalid plan schema")
    seen = set()
    for item in plan["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str):
            raise ValueError("invalid plan item")
        relative = item["relative_path"]
        if relative in seen:
            raise ValueError("duplicate path")
        seen.add(relative)
        checked_item(target, item)
    staged = []
    try:
        for item in plan["items"]:
            path, decoded = checked_item(target, item)
            fd, temporary = tempfile.mkstemp(prefix=".restore-", dir=path.parent)
            staged.append((path, Path(temporary)))
            with os.fdopen(fd, "wb") as stream:
                stream.write(decoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, path.stat().st_mode & 0o777)
        for item in plan["items"]:
            checked_item(target, item)
        for path, temporary in staged:
            os.replace(temporary, path)
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", type=Path)
    mode.add_argument("--apply", type=Path)
    args = parser.parse_args()
    try:
        if args.apply:
            plan = json.loads(args.apply.read_text(encoding="utf-8"), parse_constant=reject_constant)
            apply_plan(plan, args.root)
        else:
            if args.root is None:
                parser.error("--plan requires --root")
            plan = build_plan(args.root)
            with args.plan.open("x", encoding="utf-8") as stream:
                json.dump(plan, stream, indent=2, allow_nan=False)
        print(f"planned={len(plan['items'])} unknown={len(plan['unknown'])}")
        return 2 if plan["unknown"] else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"recovery refused: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
