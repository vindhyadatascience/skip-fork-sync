"""Execute production shell functions in their real conditional caller context."""
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

import test_skipstone_artifact_check as artifact


STUB = r'''
import json, os, sys
from pathlib import Path
name=Path(sys.argv[0]).name
args=sys.argv[1:]
p=Path(os.environ["STUB_STATE"])
calls=json.loads(p.read_text()); calls.append([name]+args); p.write_text(json.dumps(calls))
fail=os.environ.get("FAIL_STAGE", "")
if name == "swift":
    stage="build" if args[:1]==["build"] else (args[1] if len(args)>1 else "")
    if stage == "build":
        root=Path(".build/plugins/outputs"); root.mkdir(parents=True,exist_ok=True)
        Path(".build/artifacts").mkdir(parents=True,exist_ok=True)
        if os.environ.get("MARKER_ONLY") == "1":
            (root/".Module.sourcehash").write_text(json.dumps({"skipstone":os.environ["VERSION"],"sourcehashes":{}}))
        else:
            (root/"Foo.kt").write_text("val result = 1\n")
    sys.exit(67 if fail == stage else 0)
if name == "find":
    print(".build/artifacts/fixture")
    sys.exit(67 if fail == "find" else 0)
if name == "python3":
    if len(args)>1 and args[1] == "manifest" and fail == "manifest":
        if os.environ.get("PARTIAL_HELPER") == "1":
            Path(args[3]).write_text("partial helper output\n")
        sys.exit(67)
    os.execv(sys.executable, [sys.executable]+args)
if name == "git" and args[0] == "clone":
    Path(args[-1]).mkdir(parents=True,exist_ok=True)
elif name == "gh" and args[:1] == ["api"] and "/releases/latest" in args[1]:
    print(os.environ["VERSION"])
else:
    sys.exit(97)
'''


class TestConditionalShellStatus(artifact.FixtureCase):
    def setUp(self):
        super().setUp()
        (self.out/"logs").mkdir()
        self.sample=self.root/"sample"
        self.sample.mkdir()
        self.env.update(WORK=str(self.root), OUT=str(self.out), SUMMARY=str(self.out/"check-summary.md"),
                        SAMPLES="sample", SAMPLE=str(self.sample))
        (self.out/"check-summary.md").write_text("fixture summary\n")
        for name in ["swift","find","python3","git","gh"]:
            p=self.bin/name
            p.write_text("#!"+sys.executable+"\n"+STUB)
            p.chmod(0o755)

    def fragment(self):
        text=artifact.BUILD_SCRIPT.read_text()
        return text[text.index("manifest() {"):text.index("\nSTATUS=0")]

    def variant(self, stage="", partial=False):
        env=dict(self.env, FAIL_STAGE=stage, PARTIAL_HELPER="1" if partial else "0")
        command=self.fragment()+'\nif ! run_variant "$SAMPLE" stock; then exit 23; fi\n'
        return subprocess.run(["/bin/bash","-e","-o","pipefail","-c",command],
                              cwd=self.root,env=env,text=True,capture_output=True)

    def old_manifests(self):
        record=json.dumps(dict(kind="file",path="./Foo.kt",sha256="0"*64))+"\n"
        for variant in ("stock","vds"):
            (self.out/"logs"/("sample-"+variant+".manifest")).write_text(record)

    def sample_loop(self, stage="", marker_only=False):
        text=artifact.BUILD_SCRIPT.read_text()
        command='log() { :; }\n'+self.fragment()+text[text.index("\nSTATUS=0"):]
        env=dict(self.env,FAIL_STAGE=stage,MARKER_ONLY="1" if marker_only else "0")
        return subprocess.run(["/bin/bash","-e","-o","pipefail","-c",command],
                              cwd=self.root,env=env,text=True,capture_output=True)

    def test_resolve_and_edit_failures_propagate_under_if_not(self):
        for stage in ("resolve","edit"):
            with self.subTest(stage=stage):
                self.old_manifests()
                result=self.variant(stage)
                self.assertEqual(result.returncode,23,result.stdout+result.stderr)
                self.assertFalse((self.out/"logs/sample-stock.manifest").exists())

    def test_build_failure_cannot_be_masked_by_successful_find(self):
        self.old_manifests()
        result=self.variant("build")
        self.assertEqual(result.returncode,23,result.stdout+result.stderr)
        self.assertFalse((self.out/"logs/sample-stock.manifest").exists())

    def test_find_failure_propagates(self):
        result=self.variant("find")
        self.assertEqual(result.returncode,23,result.stdout+result.stderr)
        self.assertFalse((self.out/"logs/sample-stock.manifest").exists())

    def test_helper_refusal_discards_stale_and_partial_manifests(self):
        self.old_manifests()
        result=self.variant("manifest",partial=True)
        self.assertEqual(result.returncode,23,result.stdout+result.stderr)
        self.assertFalse((self.out/"logs/sample-stock.manifest").exists())
        self.assertEqual(list((self.out/"logs").glob("sample-stock.manifest.tmp.*")),[])

    def test_stale_matching_manifests_cannot_make_failed_sample_green(self):
        self.old_manifests()
        result=self.sample_loop("manifest")
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        summary=(self.out/"check-summary.md").read_text()
        self.assertIn("**Result: FAIL**",summary)
        self.assertNotIn("- PASS",summary)
        self.assertFalse(any(c[:3]==["gh","release","create"] or c[:3]==["gh","api","-X"] for c in self.calls()))

    def test_marker_only_outputs_are_not_identical_code_acceptance(self):
        result=self.sample_loop(marker_only=True)
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("**Result: FAIL**",(self.out/"check-summary.md").read_text())

    def test_success_installs_a_fresh_manifest_and_accepts_kotlin(self):
        self.old_manifests()
        result=self.sample_loop()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn("**Result: PASS**",(self.out/"check-summary.md").read_text())
        records=[json.loads(line) for line in (self.out/"logs/sample-stock.manifest").read_text().splitlines()]
        self.assertEqual(records[0]["path"],"./Foo.kt")
        self.assertNotEqual(records[0]["sha256"],"0"*64)


if __name__ == "__main__":
    unittest.main()
