"""Hermetic byte/provenance regressions; build and GitHub commands are stubs."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts/skipstone_build_check.sh"
WORKFLOW = ROOT / ".github/workflows/skipstone-build.yml"
HELPER = ROOT / "scripts/skipstone_artifact_check.py"
STOCK = "1.9.17"
VDS = "1.9.17-vds.1"
SOURCE = "a" * 40
ADVANCED = "b" * 40


def workflow_step(name):
    lines = WORKFLOW.read_text().splitlines()
    start = lines.index("      - name: " + name)
    run = lines.index("        run: |", start) + 1
    end = next((i for i in range(run, len(lines)) if lines[i].startswith("      - name:")), len(lines))
    return textwrap.dedent("\n".join(lines[run:end]))


STUB = r'''
import json
import os
from pathlib import Path
import sys
name = Path(sys.argv[0]).name
args = sys.argv[1:]
p = Path(os.environ["STUB_STATE"])
state = json.loads(p.read_text())
state.append([name] + args)
p.write_text(json.dumps(state))
if name == "git":
    if args[0] == "ls-remote":
        if "--tags" not in args:
            print(os.environ["ADVANCED_SHA"] + "\trefs/heads/" + os.environ["REF"])
        else:
            mode = os.environ.get("STUB_TAG_STATE", "absent")
            if mode == "absent":
                sys.exit(2)
            if mode == "error":
                sys.exit(128)
            ref = "refs/tags/" + os.environ["VDS_TAG"]
            commit = os.environ["SOURCE_SHA"] if mode.startswith("matching") else os.environ["ADVANCED_SHA"]
            if mode.endswith("annotated"):
                print("c" * 40 + "\t" + ref)
                print(commit + "\t" + ref + "^{}")
            else:
                print(commit + "\t" + ref)
    elif args[0] == "clone" and args[-1].endswith("/skipstone"):
        root = Path(args[-1])
        (root / "Sources/SkipSyntax").mkdir(parents=True)
        (root / "Sources/SkipSyntax/Version.swift").write_text('public let skipVersion = "' + os.environ["VDS_TAG"] + '"\n')
        (root / "scripts").mkdir()
        (root / "scripts/build_macos_plugin.sh").write_text("#!/bin/bash\nexit 98\n")
    elif args == ["rev-parse", "HEAD"]:
        print(os.environ["SOURCE_SHA"])
    elif args[0] == "ls-files":
        sys.exit(1)
    else:
        sys.exit(97)
if name == "gh":
    if args[:2] == ["api", "repos/fixture/skipstone/releases"]:
        print(os.environ.get("STUB_RELEASE", ""))
    elif args[:2] == ["release", "create"]:
        print("https://example.invalid/draft")
    elif args[:2] == ["pr", "comment"]:
        pass
    elif args[:2] == ["api", "-X"] or (args[0] == "api" and "/assets" in args[1]):
        pass
    else:
        sys.exit(97)
if name == "swift":
    if args == ["--version"]:
        print("Swift fixture")
    elif args != ["package", "resolve"]:
        sys.exit(97)
if name in ("sw_vers", "xcodebuild"):
    print("fixture toolchain")
if name == "curl":
    sys.exit(97)
'''


class FixtureCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="skipstone-artifact-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.state = self.root / "calls.json"
        self.state.write_text("[]")
        for name in ["git", "gh", "swift", "xcodebuild", "sw_vers", "curl"]:
            p = self.bin / name
            p.write_text("#!" + sys.executable + "\n" + STUB)
            p.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        STUB_STATE=str(self.state), SOURCE_SHA=SOURCE, ADVANCED_SHA=ADVANCED,
                        FORK="fixture/skipstone", REF="rebase/" + STOCK, VDS_TAG=VDS,
                        RUNNER_TEMP=str(self.root), VERSION=STOCK, ARTIFACT_CHECK=str(HELPER))
        self.out = self.root / "out"
        self.out.mkdir()

    def calls(self):
        return json.loads(self.state.read_text())

    def attach(self, source=SOURCE, existing=""):
        scripts = self.root / "scripts"
        scripts.mkdir(exist_ok=True)
        if HELPER.exists():
            shutil.copyfile(HELPER, scripts / HELPER.name)
        if source is not None:
            (self.out / "source-commit.txt").write_text(source + "\n")
        for name in ["skip-macos.zip", "skip-macos.zip.sha256", "check-summary.md"]:
            (self.out / name).write_text("fixture\n")
        env = dict(self.env, STUB_RELEASE=existing)
        return subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", workflow_step("Attach to draft release")],
                              cwd=self.root, env=env, text=True, capture_output=True)


class TestDraftSourceIdentity(FixtureCase):
    def test_advanced_ref_cannot_replace_the_recorded_source(self):
        result = self.attach()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        creates = [c for c in self.calls() if c[:3] == ["gh", "release", "create"]]
        self.assertEqual(len(creates), 1)
        self.assertEqual(creates[0][creates[0].index("--target") + 1], SOURCE)
        self.assertFalse(any(c[0] == "git" and any("refs/heads/" in a for a in c) for c in self.calls()), self.calls())

    def test_existing_draft_also_uses_the_recorded_source(self):
        result = self.attach(existing="11 true")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        patches = [c for c in self.calls() if c[:4] == ["gh", "api", "-X", "PATCH"]]
        self.assertEqual(len(patches), 1)
        self.assertIn("target_commitish=" + SOURCE, patches[0])
        self.assertFalse(any(c[0] == "git" and any("refs/heads/" in a for a in c) for c in self.calls()), self.calls())

    def assert_no_release_writes(self):
        self.assertFalse(any(c[:3] == ["gh", "release", "create"] or
                             c[:3] == ["gh", "api", "-X"] for c in self.calls()), self.calls())

    def test_mismatched_existing_tag_blocks_create_and_update(self):
        for existing in ("", "11 true"):
            for mode in ("mismatching-lightweight", "mismatching-annotated"):
                with self.subTest(existing=existing, mode=mode):
                    self.state.write_text("[]")
                    self.env["STUB_TAG_STATE"] = mode
                    result = self.attach(existing=existing)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assert_no_release_writes()

    def test_matching_lightweight_and_annotated_tags_allow_recorded_source(self):
        for existing in ("", "11 true"):
            for mode in ("matching-lightweight", "matching-annotated"):
                with self.subTest(existing=existing, mode=mode):
                    self.state.write_text("[]")
                    self.env["STUB_TAG_STATE"] = mode
                    result = self.attach(existing=existing)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertTrue(any(c[0] == "git" and "--tags" in c for c in self.calls()), self.calls())
                    self.assertFalse(any(c[0] == "git" and c[1] in ("push", "tag") for c in self.calls()))

    def test_tag_lookup_failure_is_not_tag_absence(self):
        self.env["STUB_TAG_STATE"] = "error"
        result = self.attach()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_no_release_writes()

    def test_missing_source_identity_refuses_before_any_github_call(self):
        result = self.attach(source=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] in ("gh", "git") for c in self.calls()), self.calls())

    def test_unverified_prebuilt_identity_is_not_a_commit(self):
        result = self.attach(source="prebuilt-unverified")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] in ("gh", "git") for c in self.calls()), self.calls())

    def test_published_release_remains_untouched(self):
        result = self.attach(existing="11 false")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[:3] == ["gh", "release", "create"] or c[:3] == ["gh", "api", "-X"]
                             for c in self.calls()), self.calls())

    def test_attachment_failure_does_not_claim_a_published_draft_asset(self):
        (self.out / "check-summary.md").write_text("build/check fixture passed\n")
        env = dict(self.env, STATUS="0", ATTACHED="failure", PR="1", RUN_URL="https://example.invalid/run")
        result = subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", workflow_step("Comment on the PR")],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        message = (self.root / "comment.md").read_text()
        self.assertIn("draft attachment failed", message)
        self.assertNotIn("PASS, zip attached", message)

    def test_source_is_recorded_before_the_stubbed_build_stops(self):
        result = subprocess.run(["/bin/bash", str(BUILD_SCRIPT), STOCK, self.env["REF"], VDS, str(self.out)],
                                cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)  # fixture build exits before any real compilation
        identity = self.out / "source-commit.txt"
        self.assertTrue(identity.exists(), result.stdout + result.stderr)
        self.assertEqual(identity.read_text().strip(), SOURCE)
        self.assertFalse(any(c[0] in ("curl", "gh") for c in self.calls()), self.calls())

    def test_prebuilt_mode_removes_a_stale_source_identity(self):
        (self.out / "source-commit.txt").write_text(SOURCE + "\n")
        archive = self.root / "prebuilt.zip"
        archive.write_bytes(b"fixture prebuilt bytes")
        env = dict(self.env, VDS_ZIP=str(archive))
        result = subprocess.run(["/bin/bash", str(BUILD_SCRIPT), STOCK, self.env["REF"], VDS, str(self.out)],
                                cwd=self.root, env=env, text=True, capture_output=True)
        self.assertNotEqual(result.returncode, 0)  # stock clone is deliberately blocked
        self.assertFalse((self.out / "source-commit.txt").exists())


class TestGeneratedOutputManifest(FixtureCase):
    def manifest(self, name, files, links=()):
        tree = self.root / name
        tree.mkdir()
        for path, data in files.items():
            p = tree / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        for path, target in links:
            p = tree / path
            p.parent.mkdir(parents=True, exist_ok=True)
            p.symlink_to(target)
        script = BUILD_SCRIPT.read_text()
        fragment = script[script.index("manifest() {"):script.index("\nrun_variant() {")]
        output = self.root / (name + ".manifest")
        result = subprocess.run(["/bin/bash", "-e", "-o", "pipefail", "-c", fragment + '\nmanifest "$1" "$2"',
                                 "manifest-test", str(tree), str(output)],
                                cwd=self.root, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for path, data in files.items():
            self.assertEqual((tree / path).read_bytes(), data, "manifest must never rewrite generated files")
        return output.read_bytes()

    def test_only_top_level_marker_version_changes_are_normalized(self):
        stock = b'{\n "skipstone" : "1.9.17", "sourcehashes":{}, "escaped":"\\u03b1", "nested":{"skipstone":"retained"}\n}\r\n'
        vds = stock.replace(b'"1.9.17"', b'"1.9.17-vds.1"')
        self.assertEqual(self.manifest("stock", {"module/.Module.sourcehash": stock}),
                         self.manifest("vds", {"module/.Module.sourcehash": vds}))

    def test_source_bytes_that_look_like_version_metadata_must_differ(self):
        stock = b'val scientificMetadata = """{"skipstone":"1.9.17"}"""\n'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {"module/ScientificOutput.kt": stock}),
                            self.manifest("vds", {"module/ScientificOutput.kt": vds}))

    def test_nested_marker_fields_are_not_version_stamp_exceptions(self):
        stock = b'{"skipstone":"1.9.17","sourcehashes":{},"nested":{"skipstone":"1.9.17"}}'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {".Module.sourcehash": stock}),
                            self.manifest("vds", {".Module.sourcehash": vds}))

    def test_hidden_unrelated_sourcehash_without_hash_map_remains_exact(self):
        stock = b'{"skipstone":"1.9.17","data":"unrelated metadata"}'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {".data.sourcehash": stock}),
                            self.manifest("vds", {".data.sourcehash": vds}))

    def test_duplicate_sourcehashes_fields_remain_exact(self):
        stock = b'{"skipstone":"1.9.17","sourcehashes":{},"sourcehashes":{}}'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {".Module.sourcehash": stock}),
                            self.manifest("vds", {".Module.sourcehash": vds}))

    def test_sourcehashes_wrong_type_remains_exact(self):
        stock = b'{"skipstone":"1.9.17","sourcehashes":[]}'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {".Module.sourcehash": stock}),
                            self.manifest("vds", {".Module.sourcehash": vds}))

    def test_empty_module_basename_remains_exact(self):
        stock = b'{"skipstone":"1.9.17","sourcehashes":{}}'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        for index, name in enumerate([".sourcehash", "..sourcehash"]):
            with self.subTest(name=name):
                self.assertNotEqual(self.manifest("stock" + str(index), {name: stock}),
                                    self.manifest("vds" + str(index), {name: vds}))

    def test_non_marker_sourcehash_filename_remains_exact(self):
        stock = b'{"skipstone":"1.9.17"}\n'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {"scientific.sourcehash": stock}),
                            self.manifest("vds", {"scientific.sourcehash": vds}))

    def test_binary_hidden_files_and_symlink_targets_are_preserved(self):
        files = {"module/data.bin": bytes([0, 255, 128, 10]), ".hidden": b"exact\r\n"}
        a = self.manifest("a", files, [("module/link", "../data.bin")])
        b = self.manifest("b", files, [("module/link", "../other.bin")])
        self.assertNotEqual(a, b)
        self.assertIn(b".hidden", a)
        self.assertIn(b"data.bin", a)
        self.assertIn(b"link", a)

    def test_manifest_summary_counts_real_kotlin_files_not_symlinks(self):
        self.manifest("counted", {"module/Foo.kt": b"code\n", "module/data.bin": b"bytes"},
                      [("module/link.kt", "Foo.kt")])
        result = subprocess.run([sys.executable, str(HELPER), "count-kotlin", str(self.root / "counted.manifest")],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "1")

    def test_invalid_marker_bytes_are_not_normalized(self):
        stock = b'not JSON {"skipstone":"1.9.17"}\n'
        vds = stock.replace(b'1.9.17', b'1.9.17-vds.1')
        self.assertNotEqual(self.manifest("stock", {".Module.sourcehash": stock}),
                            self.manifest("vds", {".Module.sourcehash": vds}))


if __name__ == "__main__":
    unittest.main()
