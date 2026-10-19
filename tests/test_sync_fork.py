"""Unit tests for scripts/sync_fork.py against local bare repositories.

Run: python3 -m unittest discover -s tests -v
"""

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import sync_fork as sf  # noqa: E402

ENV = dict(os.environ, GIT_AUTHOR_NAME="T", GIT_AUTHOR_EMAIL="t@example.com",
           GIT_COMMITTER_NAME="T", GIT_COMMITTER_EMAIL="t@example.com", GIT_TERMINAL_PROMPT="0")
for k in list(ENV):
    if k.startswith("GIT_CONFIG_") or k == "GIT_DIR":
        del ENV[k]

VERSION_SWIFT = "Sources/SkipSyntax/Version.swift"


def git(cwd, *args, check=True):
    p = subprocess.run(["git", *args], cwd=cwd, env=ENV, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if check and p.returncode != 0:
        raise AssertionError(f"git {args} failed: {p.stderr}")
    return p.stdout.strip()


def write(root, path, text):
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(text)


def commit(root, msg, files):
    for path, text in files.items():
        write(root, path, text)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", msg)
    return git(root, "rev-parse", "HEAD")


class FakeGitHub(sf.GitHub):
    def __init__(self, root, latest=None):
        super().__init__(None)
        self.root = root
        self.latest = latest
        self.prs = []  # dicts with fork/base/head/number/state/isDraft/body/url
        self.comments = {}  # number -> list of {id, body}
        self.calls = []
        self._next_comment = 100

    def repo_url(self, slug):
        return os.path.join(self.root, slug.replace("/", "__") + ".git")

    def latest_release(self, upstream):
        self.calls.append(("latest_release", upstream))
        return self.latest

    def sync_fork_main(self, fork):
        self.calls.append(("sync", fork))
        up = self.repo_url(FORK_CFG.upstream)
        git(self.root, "--git-dir", self.repo_url(fork), "fetch", "-q", up, "+refs/heads/main:refs/heads/main")

    def find_prs(self, fork, head):
        self.calls.append(("find_prs", head))
        return [dict(p) for p in self.prs if p["head"] == head]

    def create_pr(self, fork, base, head, title, body, draft):
        self.calls.append(("create_pr", head, draft))
        n = len(self.prs) + 1
        pr = {"number": n, "url": f"https://example/pr/{n}", "state": "OPEN", "isDraft": draft,
              "body": body, "head": head, "base": base, "title": title}
        self.prs.append(pr)
        return dict(pr)

    def edit_pr_body(self, fork, number, body):
        self.calls.append(("edit_pr_body", number))
        self.prs[number - 1]["body"] = body

    def list_comments(self, fork, number):
        return [dict(c) for c in self.comments.get(number, [])]

    def create_comment(self, fork, number, body):
        self.calls.append(("create_comment", number))
        self._next_comment += 1
        self.comments.setdefault(number, []).append({"id": self._next_comment, "body": body})

    def edit_comment(self, fork, comment_id, body):
        self.calls.append(("edit_comment", comment_id))
        for cs in self.comments.values():
            for c in cs:
                if c["id"] == comment_id:
                    c["body"] = body


FORK_CFG = sf.ForkConfig("skipstone", "up/skipstone", "fork/skipstone", "-vds.", (sf.SKIPSTONE_STAMP,))


class Fixture:
    """upstream: 1.0.0 -> 1.1.0 (optionally touching a file our patch touches).
    fork: main = upstream main at 1.0.0; vds/1.0 = 1.0.0 + [code patch, stamp patch, doc patch]."""

    def __init__(self, conflict=False, upstream_has_patch=False, extra_upstream_tags=()):
        self.root = tempfile.mkdtemp(prefix="sfs-test-")
        self.gh = FakeGitHub(self.root)
        self.up_bare = self.gh.repo_url(FORK_CFG.upstream)
        self.fork_bare = self.gh.repo_url(FORK_CFG.fork)
        work = os.path.join(self.root, "work")
        os.makedirs(work)
        git(work, "init", "-q", "-b", "main")
        self.c0 = commit(work, "initial", {
            "a.txt": "line1\nline2\nline3\n",
            VERSION_SWIFT: 'public let skipVersion = "1.0.0"\n',
            "README": "upstream\n"})
        git(work, "tag", "1.0.0")
        # Our patches on vds/1.0 (two-component branch name on purpose).
        git(work, "checkout", "-q", "-b", "vds/1.0")
        self.p_code = commit(work, "VDS: code patch", {"a.txt": "line1\nPATCHED\nline3\n"})
        self.p_stamp = commit(work, "VDS: stamp skipVersion", {VERSION_SWIFT: 'public let skipVersion = "1.0.0-vds.1"\n'})
        self.p_doc = commit(work, "VDS: build doc", {"VDS-BUILD.md": "how we build\n"})
        git(work, "tag", "1.0.0-vds.1")
        git(work, "checkout", "-q", "main")
        files = {VERSION_SWIFT: 'public let skipVersion = "1.1.0"\n', "new.txt": "feature\n"}
        if conflict:
            files["a.txt"] = "line1\nUPSTREAM\nline3\n"
        if upstream_has_patch:
            files["a.txt"] = "line1\nPATCHED\nline3\n"
        self.up_change = commit(work, "upstream feature for 1.1.0", files)
        git(work, "tag", "1.1.0")
        for t in extra_upstream_tags:
            commit(work, f"upstream {t}", {"README": f"upstream {t}\n"})
            git(work, "tag", t)
        git(self.root, "init", "-q", "--bare", self.up_bare)
        git(work, "push", "-q", self.up_bare, "main", "--tags")
        git(self.root, "init", "-q", "--bare", self.fork_bare)
        git(work, "push", "-q", self.fork_bare, f"{self.c0}:refs/heads/main", "vds/1.0", "refs/tags/1.0.0", "refs/tags/1.0.0-vds.1")
        git(self.fork_bare, "symbolic-ref", "HEAD", "refs/heads/main")
        self.work = work

    def fork_refs(self):
        return git(self.fork_bare, "for-each-ref", "--format=%(refname) %(objectname)")

    def run(self, dry_run=False, latest="1.1.0", rehearse=False):
        self.gh.latest = latest
        opts = sf.Options(dry_run=dry_run, workdir=os.path.join(self.root, "scratch"), rehearse=rehearse,
                          log=lambda s: None)
        return sf.sync_fork(FORK_CFG, self.gh, opts)

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Base(unittest.TestCase):
    def make(self, **kw):
        fx = Fixture(**kw)
        self.addCleanup(fx.cleanup)
        return fx


class TestPatchDetection(Base):
    def test_patches_base_tag_and_stamp_classification(self):
        fx = self.make()
        res = fx.run(dry_run=True)
        self.assertEqual(res.action, "would-create")
        self.assertEqual(res.old_branch, "vds/1.0")
        self.assertEqual(res.base_tag, "1.0.0")  # branch named vds/1.0 maps to tag 1.0.0
        self.assertEqual([p.sha for p in res.patches], [fx.p_code, fx.p_stamp, fx.p_doc])
        self.assertEqual([p.stamp_only for p in res.patches], [False, True, False])
        self.assertEqual(res.upstream_count, 1)
        self.assertIn("upstream feature for 1.1.0", res.upstream_changes[0])
        self.assertEqual(res.compare_url, "https://github.com/up/skipstone/compare/1.0.0...1.1.0")

    def test_mixed_patch_with_stamp_line_is_not_stamp_only(self):
        fx = self.make()
        git(fx.work, "checkout", "-q", "vds/1.0")
        mixed = commit(fx.work, "mixed", {VERSION_SWIFT: 'public let skipVersion = "x"\n// note\n'})
        g = sf.Git(fx.work, ENV)
        self.assertFalse(sf.classify_patch(g, FORK_CFG, mixed, "1.1.0").stamp_only)
        self.assertTrue(sf.classify_patch(g, FORK_CFG, fx.p_stamp, "1.1.0").stamp_only)

    def test_package_pin_is_flagged(self):
        fx = self.make()
        cfg = sf.ForkConfig("skip-foundation", "u/x", "f/x", "+vds.")
        git(fx.work, "checkout", "-q", "vds/1.0")
        sha = commit(fx.work, "pin", {"Package.swift": '.package(url: "https://github.com/vindhyadatascience/skip-lib.git", exact: "1.4.4+vds.1")\n'})
        p = sf.classify_patch(sf.Git(fx.work, ENV), cfg, sha, "1.1.0")
        self.assertTrue(p.has_pins)
        self.assertFalse(p.stamp_only)

    def test_skip_binary_target_stamp(self):
        fx = self.make()
        cfg = sf.FORKS["skip"]
        git(fx.work, "checkout", "-q", "main")
        commit(fx.work, "pkg", {"Package.swift": '    package.targets += [.binaryTarget(name: "skip", url: "https://a/1/skip-macos.zip", checksum: "aa")]\n'})
        sha = commit(fx.work, "stamp", {"Package.swift": '    package.targets += [.binaryTarget(name: "skip", url: "https://b/2/skip-macos.zip", checksum: "bb")]\n'})
        p = sf.classify_patch(sf.Git(fx.work, ENV), cfg, sha, "1.9.17")
        self.assertTrue(p.stamp_only)
        self.assertIn("1.9.17-vds.N/skip-macos.zip", p.stamp_how[0])

    def test_falls_back_to_highest_semver_tag(self):
        fx = self.make(extra_upstream_tags=("1.2.0", "1.10.0"))
        res = fx.run(dry_run=True, latest=None)
        self.assertEqual(res.upstream_tag, "1.10.0")  # numeric, not lexical
        self.assertTrue(res.upstream_source.startswith("highest tag"))

    def test_parse_version(self):
        self.assertEqual(sf.parse_version("1.60"), (1, 60, 0))
        self.assertEqual(sf.parse_version("v1.9.16"), (1, 9, 16))
        self.assertIsNone(sf.parse_version("1.9.16-vds.1"))
        self.assertIsNone(sf.parse_version("1.4.7+vds.1"))


class TestCreate(Base):
    def test_dry_run_pushes_nothing(self):
        fx = self.make()
        before = fx.fork_refs()
        res = fx.run(dry_run=True)
        self.assertEqual(res.action, "would-create")
        self.assertEqual(fx.fork_refs(), before)
        self.assertEqual([c for c in fx.gh.calls if c[0] in ("sync", "create_pr", "create_comment")], [])
        self.assertEqual([p.status for p in res.patches], ["applied", "regenerate-after-build", "applied"])

    def test_clean_cherry_pick_opens_ready_pr(self):
        fx = self.make()
        res = fx.run()
        self.assertEqual(res.action, "created")
        self.assertFalse(res.draft)
        tag_commit = git(fx.up_bare, "rev-parse", "1.1.0^{commit}")
        self.assertEqual(git(fx.fork_bare, "rev-parse", "refs/heads/vds/1.1.0"), tag_commit)
        self.assertEqual(git(fx.fork_bare, "rev-parse", "refs/heads/main"), git(fx.up_bare, "rev-parse", "main"))
        log = git(fx.fork_bare, "log", "--format=%s", "vds/1.1.0..rebase/1.1.0").splitlines()
        self.assertEqual(log, ["VDS: build doc", "VDS: code patch"])  # stamp not cherry-picked
        self.assertEqual(git(fx.fork_bare, "show", f"rebase/1.1.0:{VERSION_SWIFT}"), 'public let skipVersion = "1.1.0"')
        self.assertEqual(git(fx.fork_bare, "show", "rebase/1.1.0:a.txt"), "line1\nPATCHED\nline3")
        (pr,) = fx.gh.prs
        self.assertEqual((pr["base"], pr["head"], pr["isDraft"]), ("vds/1.1.0", "rebase/1.1.0", False))
        self.assertIn("upstream feature for 1.1.0", pr["body"])
        self.assertIn("compare/1.0.0...1.1.0", pr["body"])
        self.assertIn("regenerate after build", pr["body"])
        self.assertIn('`"1.1.0-vds.N"`', pr["body"])
        self.assertEqual(len(fx.gh.comments[1]), 1)
        self.assertLess(fx.gh.calls.index(("sync", FORK_CFG.fork)), fx.gh.calls.index(("create_pr", "rebase/1.1.0", False)))

    def test_conflict_opens_draft_with_applied_patches_committed(self):
        fx = self.make(conflict=True)
        res = fx.run()
        self.assertEqual(res.action, "created")
        self.assertTrue(res.draft)
        statuses = {p.subject: p for p in res.patches}
        self.assertEqual(statuses["VDS: code patch"].status, "conflict")
        self.assertEqual(statuses["VDS: code patch"].files, ["a.txt"])
        self.assertEqual(statuses["VDS: build doc"].status, "applied")
        log = git(fx.fork_bare, "log", "--format=%s", "vds/1.1.0..rebase/1.1.0").splitlines()
        self.assertEqual(log, ["VDS: build doc"])
        (pr,) = fx.gh.prs
        self.assertTrue(pr["isDraft"])
        self.assertIn("conflicts in `a.txt`", pr["body"])
        self.assertIn("did not apply", pr["body"])

    def test_patch_already_upstream_is_dropped(self):
        fx = self.make(upstream_has_patch=True)
        res = fx.run(dry_run=True)
        self.assertEqual(res.patches[0].status, "already-upstream")
        self.assertFalse(res.draft)


class TestIdempotency(Base):
    def test_second_run_updates_comment_not_duplicates(self):
        fx = self.make()
        fx.run()
        refs = fx.fork_refs()
        res2 = fx.run()
        self.assertEqual(res2.action, "refreshed")
        self.assertEqual(len(fx.gh.prs), 1)
        self.assertEqual(len(fx.gh.comments[1]), 1)
        self.assertEqual(fx.fork_refs(), refs)  # nothing re-pushed
        self.assertEqual([p.status for p in res2.patches], ["applied", "regenerate-after-build", "applied"])
        self.assertNotIn(("edit_comment", 101), fx.gh.calls)  # unchanged text: no edit
        self.assertNotIn(("edit_pr_body", 1), fx.gh.calls)

    def test_conflict_resolved_by_hand_is_seen_and_comment_edited(self):
        fx = self.make(conflict=True)
        fx.run()
        self.assertIn("still to resolve", fx.gh.comments[1][0]["body"])
        # A human resolves the conflict on rebase/1.1.0 with cherry-pick -x.
        hand = os.path.join(fx.root, "hand")
        git(fx.root, "clone", "-q", fx.fork_bare, hand)
        git(hand, "checkout", "-q", "rebase/1.1.0")
        git(hand, "fetch", "-q", fx.fork_bare, "vds/1.0")
        git(hand, "cherry-pick", "-x", fx.p_code, check=False)
        write(hand, "a.txt", "line1\nUPSTREAM+PATCHED\nline3\n")
        git(hand, "add", "a.txt")
        git(hand, "-c", "core.editor=true", "cherry-pick", "--continue")
        git(hand, "push", "-q", "origin", "rebase/1.1.0")
        res = fx.run()
        self.assertEqual(res.action, "refreshed")
        self.assertEqual(res.patches[0].status, "applied")
        self.assertEqual(len(fx.gh.comments[1]), 1)
        self.assertIn(("edit_comment", 101), fx.gh.calls)
        self.assertIn("Every code patch is on the branch", fx.gh.comments[1][0]["body"])
        self.assertEqual(len(fx.gh.prs), 1)

    def test_unresolved_conflict_reports_files_on_refresh(self):
        fx = self.make(conflict=True)
        fx.run()
        res = fx.run(dry_run=True)
        self.assertEqual(res.action, "would-refresh")
        self.assertEqual(res.patches[0].status, "missing")
        self.assertEqual(res.patches[0].files, ["a.txt"])

    def test_merged_pr_means_up_to_date(self):
        fx = self.make()
        fx.run()
        fx.gh.prs[0]["state"] = "MERGED"
        n = len(fx.gh.calls)
        res = fx.run()
        self.assertEqual(res.action, "up-to-date")
        self.assertFalse([c for c in fx.gh.calls[n:] if c[0] in ("create_pr", "create_comment", "edit_comment")])

    def test_existing_vds_branch_without_pr_is_up_to_date(self):
        fx = self.make()
        git(fx.work, "push", "-q", fx.fork_bare, "1.1.0^{commit}:refs/heads/vds/1.1.0")
        res = fx.run()
        self.assertEqual(res.action, "up-to-date")
        self.assertEqual(fx.gh.prs, [])

    def test_resume_when_branches_pushed_but_pr_missing(self):
        fx = self.make()
        fx.run()
        fx.gh.prs.clear()
        fx.gh.comments.clear()
        res = fx.run()
        self.assertEqual(res.action, "refreshed")
        self.assertEqual(len(fx.gh.prs), 1)
        self.assertEqual(len(fx.gh.comments[1]), 1)

    def test_rehearse_ignores_existing_branch(self):
        fx = self.make()
        fx.run()
        refs = fx.fork_refs()
        res = fx.run(dry_run=True, rehearse=True)
        self.assertEqual(res.action, "would-create")
        self.assertEqual(fx.fork_refs(), refs)


class TestCli(unittest.TestCase):
    def test_rehearse_requires_dry_run(self):
        with self.assertRaises(SystemExit):
            sf.main(["--rehearse", "--fork", "skip"])

    def test_github_output_requires_one_fork(self):
        with self.assertRaises(SystemExit):
            sf.main(["--github-output", "--dry-run"])

    def test_token_header_not_in_argv(self):
        env = sf.git_env("secret-token")
        n = int(env["GIT_CONFIG_COUNT"]) - 1
        self.assertEqual(env[f"GIT_CONFIG_KEY_{n}"], "http.https://github.com/.extraheader")
        self.assertNotIn("secret-token", env[f"GIT_CONFIG_VALUE_{n}"])  # base64-encoded


if __name__ == "__main__":
    unittest.main()
