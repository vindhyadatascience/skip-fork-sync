#!/usr/bin/env python3
"""Byte-preserving generated-output manifests and immutable build-source identity."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def source_commit(path):
    commit = Path(path).read_text(encoding="ascii").strip()
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("source identity must contain a full cloned commit SHA; prebuilt provenance is unverified")
    return commit


def record_source(path, commit):
    if not COMMIT_RE.fullmatch(commit):
        raise ValueError("cannot record an invalid cloned commit SHA")
    Path(path).write_text(commit + "\n", encoding="ascii")


def verify_tag_listing(text, ref, source):
    """Check an existing lightweight/annotated tag without trusting target_commitish."""
    records = {}
    for line in text.splitlines():
        fields = line.split("\t")
        if (len(fields) != 2 or fields[1] not in (ref, ref + "^{}")
                or not COMMIT_RE.fullmatch(fields[0]) or fields[1] in records):
            raise ValueError("unexpected or duplicate release-tag lookup response")
        records[fields[1]] = fields[0]
    if ref not in records:
        raise ValueError("successful tag lookup did not prove an existing tag")
    if records.get(ref + "^{}", records[ref]) != source:
        raise ValueError("release tag does not peel to the recorded cloned source; refusing to move or reuse it")


def normalized_marker(data, stock_version, vds_version):
    """Normalize the version in an observed marker shape, retaining every other byte.

    Invalid/duplicate marker fields are left exact, so unexpected producer formats cannot
    acquire a broad exception. Decode offsets select a string span; the object is never
    reserialized (whitespace, escaping, order and line endings remain unchanged).
    """
    try:
        text = data.decode("utf-8")
        decoder = json.JSONDecoder()

        def skip_space(index):
            while index < len(text) and text[index] in " \t\r\n":
                index += 1
            return index

        start = skip_space(0)
        value, end = decoder.raw_decode(text, start)
        if not isinstance(value, dict) or text[end:].strip():
            return data
        index = skip_space(start + 1)
        spans = []
        fields = {}
        while text[index] != "}":
            key, index = decoder.raw_decode(text, index)
            index = skip_space(index)
            if text[index] != ":":
                return data
            field_start = skip_space(index + 1)
            field, index = decoder.raw_decode(text, field_start)
            if key in fields:
                return data
            fields[key] = field
            if key == "skipstone":
                spans.append((field_start, index, field))
            index = skip_space(index)
            if text[index] == ",":
                index = skip_space(index + 1)
            elif text[index] != "}":
                return data
        if (len(spans) != 1 or spans[0][2] != vds_version
                or not isinstance(fields.get("sourcehashes"), dict)):
            return data
        first, last, _ = spans[0]
        return (text[:first] + json.dumps(stock_version) + text[last:]).encode("utf-8")
    except (UnicodeError, ValueError, IndexError):
        return data


def generated_manifest(root, stock_version, vds_version):
    root = Path(root)
    if not root.is_dir():
        raise ValueError("generated output directory is missing")
    paths = []

    def walk_failed(error):
        raise error

    for parent, directories, files in os.walk(root, followlinks=False, onerror=walk_failed):
        retained = []
        for name in directories:
            if name in (".build", ".gradle"):
                continue  # Existing build-state exclusions, never source-output exceptions.
            path = Path(parent) / name
            if path.is_symlink():
                paths.append(path)
            else:
                retained.append(name)
        directories[:] = retained
        for name in files:
            if name not in (".build", ".gradle"):
                paths.append(Path(parent) / name)

    records = []
    for path in sorted(paths, key=lambda p: os.fsencode(p.relative_to(root))):
        relative = "./" + path.relative_to(root).as_posix()
        if path.is_symlink():
            record = dict(kind="symlink", path=relative, target=os.readlink(path))
        elif path.is_file():
            digest = hashlib.sha256()
            marker_name = path.name
            if (marker_name.startswith(".") and marker_name.endswith(".sourcehash")
                    and marker_name[1:-len(".sourcehash")]):
                digest.update(normalized_marker(path.read_bytes(), stock_version, vds_version))
            else:
                with path.open("rb") as content:
                    for chunk in iter(lambda: content.read(1024 * 1024), b""):
                        digest.update(chunk)
            record = dict(kind="file", path=relative, sha256=digest.hexdigest())
        else:
            raise ValueError("unsupported or vanished generated file: " + relative)
        records.append(json.dumps(record, sort_keys=True, ensure_ascii=True, separators=(",", ":")))
    return "".join(line + "\n" for line in records)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record-source")
    record.add_argument("path")
    record.add_argument("commit")
    read = sub.add_parser("source-commit")
    read.add_argument("path")
    tag = sub.add_parser("verify-tag")
    tag.add_argument("ref")
    tag.add_argument("source")
    manifest = sub.add_parser("manifest")
    manifest.add_argument("root")
    manifest.add_argument("output")
    manifest.add_argument("stock_version")
    manifest.add_argument("vds_version")
    count = sub.add_parser("count-kotlin")
    count.add_argument("manifest")
    args = parser.parse_args(argv)
    try:
        if args.command == "record-source":
            record_source(args.path, args.commit)
        elif args.command == "source-commit":
            print(source_commit(args.path))
        elif args.command == "verify-tag":
            verify_tag_listing(sys.stdin.read(), args.ref, args.source)
        elif args.command == "manifest":
            text = generated_manifest(args.root, args.stock_version, args.vds_version)
            Path(args.output).write_text(text, encoding="ascii")
        elif args.command == "count-kotlin":
            with Path(args.manifest).open(encoding="ascii") as content:
                records = [json.loads(line) for line in content]
            print(sum(r["kind"] == "file" and r["path"].endswith(".kt") for r in records))
    except (OSError, UnicodeError, ValueError, KeyError) as error:
        print("artifact verification refused: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
