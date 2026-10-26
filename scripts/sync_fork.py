#!/usr/bin/env python3
"""Open rebase PRs in the vindhyadatascience Skip forks when upstream releases.

For each fork (skipstone, skip, skip-ui, skip-foundation, skip-lib, skip-unit):

1. Read upstream's latest release (``gh api repos/skiptools/<repo>/releases/latest``),
   falling back to the highest semver tag.
2. Find our newest ``vds/*`` branch and the upstream tag it is based on. Our patches are
   the commits on that branch that are not in the base tag, oldest first.
3. If no ``vds/<new>`` branch exists: sync the fork's ``main`` with upstream, create
   ``vds/<new>`` at the upstream tag, cherry-pick our patches onto ``rebase/<new>``, push,
   and open a PR ``rebase/<new> -> vds/<new>``. A conflict makes the PR a draft: the patches
   that applied are committed, the conflicting ones are listed with their files.
4. If the PR / branches already exist, the status comment on the PR is updated in place
   (never duplicated) and the PR body refreshed.

Patches that only change version stamps (skipstone's ``skipVersion``, skip's macOS
``binaryTarget`` url/checksum) are not cherry-picked: their values are wrong for a new
version, so the PR lists them under "regenerate after build".

``--dry-run`` does all the local git work in a scratch clone (so conflicts are predicted
exactly) and prints the plan; it pushes nothing and makes no write calls to GitHub.

Only the Python 3 standard library, ``git`` and the ``gh`` CLI are used. Authentication:
``FORK_SYNC_TOKEN`` (preferred) or ``GH_TOKEN`` from the environment, used for both git and
gh; without either, git clones anonymously and gh uses its own login.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

STATUS_MARKER = "<!-- skip-fork-sync:status -->"
BODY_MARKER = "<!-- skip-fork-sync:pr -->"
BOT_NAME = "skip-fork-sync"
BOT_EMAIL = "skip-fork-sync@users.noreply.github.com"
MAX_UPSTREAM_LINES = 150

VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)(?:\.(\d+))?$")


# --------------------------------------------------------------------------- config


@dataclass(frozen=True)
class StampRule:
    """A changed line in ``path`` matching ``pattern`` is a version stamp."""

    path: str
    pattern: str
    how: str  # instruction; ``{new}`` and ``{sep}`` are substituted


@dataclass(frozen=True)
class ForkConfig:
    name: str
    upstream: str  # owner/repo
    fork: str  # owner/repo
    # Release tags are SemVer prereleases "<upstream>-vds.N". Never "+vds.N": SwiftPM ignores build
    # metadata, so a second "+vds.N" on one version silently re-points existing pins.
    tag_sep: str
    stamp_rules: Tuple[StampRule, ...] = ()
    # Changed lines in a manifest that pin one of our forks; flagged for review, still applied.
    pin_pattern: str = r"vindhyadatascience/|[-+]vds\.\d"
    pin_files: Tuple[str, ...] = ("Package.swift",)


SKIPSTONE_STAMP = StampRule(
    path="Sources/SkipSyntax/Version.swift",
    pattern=r"^\s*public\s+let\s+skipVersion\s*=",
    how=(
        "set `skipVersion` to `\"{new}{sep}N\"` on `vds/{new}` before tagging. The "
        "skipstone-build workflow stamps `{new}{sep}1` in its build tree only, so the "
        "draft-release binary already reports that version."
    ),
)
SKIP_STAMP = StampRule(
    path="Package.swift",
    pattern=r"binaryTarget\(\s*name:\s*\"skip\"\s*,\s*url:",
    how=(
        "once the skipstone `{new}-vds.N` release is published, point the macOS `binaryTarget` at "
        "`https://github.com/vindhyadatascience/skipstone/releases/download/{new}-vds.N/skip-macos.zip` "
        "and the Linux one at `.../{new}-vds.N/skip-linux.zip`, and set each `checksum` to that "
        "asset's SHA-256 (its `.sha256` file on the release), then commit on `vds/{new}`."
    ),
)

# The skip wrapper also pins the skipstone source it was built from (a revision dependency used by
# platforms that build the compiler from source). Like the binaryTarget, its value is wrong for a
# new version, so a commit that only moves these pins is regenerated after the build, never applied.
SKIP_SOURCE_PIN = StampRule(
    path="Package.swift",
    pattern=r"\.package\(\s*url:\s*\"https://github\.com/(?:vindhyadatascience|skiptools)/skipstone(?:\.git)?\"\s*,\s*(?:revision|exact|from):",
    how=(
        "point the skipstone source dependency at the commit the published `{new}{sep}N` release "
        "was built from (`.package(url: \"https://github.com/vindhyadatascience/skipstone.git\", "
        "revision: \"<that commit>\")`), then commit on `vds/{new}`."
    ),
)

FORKS: Dict[str, ForkConfig] = {
    "skipstone": ForkConfig("skipstone", "skiptools/skipstone", "vindhyadatascience/skipstone", "-vds.", (SKIPSTONE_STAMP,)),
    "skip": ForkConfig("skip", "skiptools/skip", "vindhyadatascience/skip", "-vds.", (SKIP_STAMP, SKIP_SOURCE_PIN)),
    "skip-ui": ForkConfig("skip-ui", "skiptools/skip-ui", "vindhyadatascience/skip-ui", "-vds."),
    "skip-foundation": ForkConfig("skip-foundation", "skiptools/skip-foundation", "vindhyadatascience/skip-foundation", "-vds."),
    "skip-lib": ForkConfig("skip-lib", "skiptools/skip-lib", "vindhyadatascience/skip-lib", "-vds."),
    "skip-unit": ForkConfig("skip-unit", "skiptools/skip-unit", "vindhyadatascience/skip-unit", "-vds."),
}


# --------------------------------------------------------------------------- helpers


class SyncError(Exception):
    pass


def parse_version(s: str) -> Optional[Tuple[int, int, int]]:
    m = VERSION_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


def token_from_env() -> Optional[str]:
    return os.environ.get("FORK_SYNC_TOKEN") or os.environ.get("GH_TOKEN") or None


def git_env(token: Optional[str]) -> Dict[str, str]:
    """Environment for git: no prompts, and an auth header for github.com when a token is set.

    The token travels in GIT_CONFIG_* variables, never on the command line or in a remote URL.
    """
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env.pop("GIT_DIR", None)
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        n = int(env.get("GIT_CONFIG_COUNT", "0") or 0)
        env[f"GIT_CONFIG_KEY_{n}"] = "http.https://github.com/.extraheader"
        env[f"GIT_CONFIG_VALUE_{n}"] = f"AUTHORIZATION: basic {basic}"
        env["GIT_CONFIG_COUNT"] = str(n + 1)
    return env


class Git:
    def __init__(self, cwd: str, env: Optional[Dict[str, str]] = None):
        self.cwd = cwd
        self.env = env if env is not None else git_env(None)

    def run(self, *args: str, check: bool = True, input: Optional[str] = None) -> subprocess.CompletedProcess:
        p = subprocess.run(["git", *args], cwd=self.cwd, env=self.env, input=input, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and p.returncode != 0:
            raise SyncError(f"git {' '.join(args)} failed ({p.returncode}): {p.stderr.strip()}")
        return p

    def out(self, *args: str) -> str:
        return self.run(*args).stdout.strip()

    def lines(self, *args: str) -> List[str]:
        return [ln for ln in self.out(*args).splitlines() if ln]

    def ref_exists(self, ref: str) -> bool:
        return self.run("rev-parse", "--verify", "--quiet", ref + "^{commit}", check=False).returncode == 0

    def is_ancestor(self, a: str, b: str) -> bool:
        return self.run("merge-base", "--is-ancestor", a, b, check=False).returncode == 0


# --------------------------------------------------------------------------- GitHub layer


class GitHub:
    """Everything that talks to the GitHub API, through ``gh``. Replaced by a fake in tests."""

    def __init__(self, token: Optional[str] = None):
        self.env = dict(os.environ)
        if token:
            self.env["GH_TOKEN"] = token
        self.env["GH_PROMPT_DISABLED"] = "1"

    def _gh(self, *args: str, check: bool = True, input: Optional[str] = None) -> subprocess.CompletedProcess:
        p = subprocess.run(["gh", *args], env=self.env, input=input, text=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and p.returncode != 0:
            raise SyncError(f"gh {' '.join(args[:3])}... failed ({p.returncode}): {p.stderr.strip()}")
        return p

    def repo_url(self, slug: str) -> str:
        return f"https://github.com/{slug}.git"

    def latest_release(self, upstream: str) -> Optional[str]:
        p = self._gh("api", f"repos/{upstream}/releases/latest", "--jq", ".tag_name", check=False)
        if p.returncode != 0:
            if "Not Found" in p.stderr or "404" in p.stderr:
                return None
            raise SyncError(f"reading the latest release of {upstream}: {p.stderr.strip()}")
        return p.stdout.strip() or None

    def sync_fork_main(self, fork: str) -> None:
        self._gh("repo", "sync", fork, "--branch", "main")

    def find_prs(self, fork: str, head: str) -> List[dict]:
        p = self._gh("pr", "list", "--repo", fork, "--head", head, "--state", "all",
                     "--json", "number,state,isDraft,url,body,baseRefName,mergeCommit")
        return json.loads(p.stdout or "[]")

    def create_pr(self, fork: str, base: str, head: str, title: str, body: str, draft: bool) -> dict:
        args = ["pr", "create", "--repo", fork, "--base", base, "--head", head, "--title", title, "--body-file", "-"]
        if draft:
            args.append("--draft")
        url = self._gh(*args, input=body).stdout.strip().splitlines()[-1]
        number = int(url.rstrip("/").rsplit("/", 1)[-1])
        return {"number": number, "url": url, "state": "OPEN", "isDraft": draft, "body": body}

    def edit_pr_body(self, fork: str, number: int, body: str) -> None:
        self._gh("pr", "edit", str(number), "--repo", fork, "--body-file", "-", input=body)

    def list_comments(self, fork: str, number: int) -> List[dict]:
        p = self._gh("api", "--paginate", f"repos/{fork}/issues/{number}/comments",
                     "--jq", ".[] | {id: .id, body: .body}")
        return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]

    def create_comment(self, fork: str, number: int, body: str) -> None:
        self._gh("api", "-X", "POST", f"repos/{fork}/issues/{number}/comments", "--input", "-",
                 input=json.dumps({"body": body}))

    def edit_comment(self, fork: str, comment_id: int, body: str) -> None:
        self._gh("api", "-X", "PATCH", f"repos/{fork}/issues/comments/{comment_id}", "--input", "-",
                 input=json.dumps({"body": body}))


# --------------------------------------------------------------------------- model


@dataclass
class Patch:
    sha: str
    subject: str
    stamp_only: bool = False
    stamp_how: List[str] = field(default_factory=list)
    has_pins: bool = False
    # Files whose every changed line is a stamp/pin, in a patch that also changes other files:
    # the rest of the patch is applied and these files are left to regeneration.
    stamp_files: List[str] = field(default_factory=list)
    status: str = "pending"  # applied | already-upstream | conflict | regenerate-after-build | missing
    files: List[str] = field(default_factory=list)  # conflicting files
    note: str = ""


@dataclass
class Result:
    fork: str
    action: str = "error"  # up-to-date | created | refreshed | closed | would-create | would-refresh
    # Also manual-only / would-manual-only (no file changes), blocked (unresolved), or error.
    upstream_tag: str = ""
    upstream_source: str = ""  # "latest release" | "highest tag"
    old_branch: str = ""
    base_tag: str = ""
    new_branch: str = ""
    rebase_branch: str = ""
    rebase_head: str = ""
    skipped_merges: int = 0
    patches: List[Patch] = field(default_factory=list)
    upstream_changes: List[str] = field(default_factory=list)
    upstream_count: int = 0
    compare_url: str = ""
    pr_number: Optional[int] = None
    pr_url: str = ""
    draft: bool = False
    message: str = ""

    @property
    def conflicts(self) -> List[Patch]:
        return [p for p in self.patches if p.status in ("conflict", "missing")]

    def outputs(self) -> Dict[str, str]:
        return {
            "action": self.action,
            "upstream_tag": self.upstream_tag,
            "new_branch": self.new_branch,
            "rebase_branch": self.rebase_branch,
            "rebase_head": self.rebase_head,
            "pr_number": str(self.pr_number or ""),
            "pr_url": self.pr_url,
            "conflicts": str(len(self.conflicts)),
        }


# --------------------------------------------------------------------------- git analysis


def changed_lines(g: Git, sha: str) -> List[Tuple[str, str]]:
    """(path, line) for every added/removed line in commit ``sha``; binary/mode-only changes
    are returned as (path, "\0") so they never count as stamps."""
    text = g.out("show", "--format=", "--unified=0", "--no-color", "--no-ext-diff", sha)
    out: List[Tuple[str, str]] = []
    path = ""
    for ln in text.splitlines():
        if ln.startswith("diff --git "):
            path = ln.split(" b/", 1)[-1]
            continue
        if ln.startswith("+++ ") or ln.startswith("--- "):
            if ln.startswith("+++ b/"):
                path = ln[6:]
            continue
        if ln.startswith("Binary files") or ln.startswith("new mode") or ln.startswith("old mode"):
            out.append((path, "\0"))
            continue
        if ln.startswith("+") or ln.startswith("-"):
            out.append((path, ln[1:]))
    files = g.lines("show", "--format=", "--name-only", sha)
    seen = {p for p, _ in out}
    for f in files:  # e.g. an empty new file
        if f not in seen:
            out.append((f, "\0"))
    return out


def classify_patch(g: Git, cfg: ForkConfig, sha: str, new: str) -> Patch:
    subject = g.out("log", "-1", "--format=%s", sha)
    p = Patch(sha=sha, subject=subject)
    lines = changed_lines(g, sha)
    rules_hit: List[StampRule] = []
    stamp_only = bool(lines) and bool(cfg.stamp_rules)
    for path, line in lines:
        hit = next((r for r in cfg.stamp_rules if r.path == path and re.search(r.pattern, line)), None)
        if hit is None:
            stamp_only = False
        elif hit not in rules_hit:
            rules_hit.append(hit)
        if path.split("/")[-1] in cfg.pin_files and re.search(cfg.pin_pattern, line):
            p.has_pins = True
    p.stamp_only = stamp_only
    if stamp_only:
        p.stamp_how = [r.how.format(new=new, sep=cfg.tag_sep) for r in rules_hit]
        p.has_pins = False
    elif rules_hit:
        by_file: Dict[str, bool] = {}
        for path, line in lines:
            hit = any(r.path == path and re.search(r.pattern, line) for r in cfg.stamp_rules)
            by_file[path] = by_file.get(path, True) and hit
        p.stamp_files = sorted(f for f, all_stamp in by_file.items() if all_stamp)
        if p.stamp_files:
            p.stamp_how = [r.how.format(new=new, sep=cfg.tag_sep) for r in rules_hit]
            p.has_pins = any(path.split("/")[-1] in cfg.pin_files and re.search(cfg.pin_pattern, line)
                             for path, line in lines if path not in p.stamp_files)
    return p


def upstream_tags(g: Git) -> Dict[str, Tuple[int, int, int]]:
    tags = {}
    for ref in g.lines("for-each-ref", "--format=%(refname)", "refs/upstream-tags/"):
        name = ref[len("refs/upstream-tags/"):]
        v = parse_version(name)
        if v is not None:
            tags[name] = v
    return tags


def vds_branches(g: Git) -> Dict[str, Tuple[int, int, int]]:
    out = {}
    for ref in g.lines("for-each-ref", "--format=%(refname)", "refs/remotes/origin/vds/"):
        name = ref[len("refs/remotes/origin/"):]
        v = parse_version(name[len("vds/"):])
        if v is not None:
            out[name] = v
    return out


def base_tag_of(g: Git, branch_ref: str, branch_version: Tuple[int, int, int],
                tags: Dict[str, Tuple[int, int, int]]) -> str:
    """The upstream tag a vds branch is based on: the tag with the branch's version if it is an
    ancestor, else the highest-semver upstream tag that is an ancestor."""
    ancestors = [t for t in tags if g.is_ancestor(f"refs/upstream-tags/{t}", branch_ref)]
    if not ancestors:
        raise SyncError(f"no upstream release tag is an ancestor of {branch_ref}")
    same = [t for t in ancestors if tags[t] == branch_version]
    if same:
        return sorted(same, key=len)[0]
    return max(ancestors, key=lambda t: (tags[t], t))


def require_reviewed_patch_source(g: Git, gh: GitHub, cfg: ForkConfig,
                                  branch: str, base_tag: str) -> None:
    """A bot-created upstream baseline is not the accepted VDS patch queue.

    Do not substitute a pending/partial rebase or silently fall back to an older branch:
    either could omit work the maintainer has not resolved yet. A completed merge, or an
    original patched VDS branch with no outstanding automation, remains a valid source.
    """
    rebase = f"rebase/{base_tag}"
    prs = gh.find_prs(cfg.fork, rebase)
    accepted = [p for p in prs if p.get("state") == "MERGED"]
    for pr in accepted:
        proof = pr.get("mergeCommit")
        oid = proof.get("oid") if isinstance(proof, dict) else None
        if pr.get("baseRefName") != branch:
            raise SyncError(f"patch source {branch} needs review: merged {rebase} targeted a different or unknown base")
        if not isinstance(oid, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid):
            raise SyncError(f"patch source {branch} needs review: accepted merge commit provenance is missing")
        if not g.is_ancestor(oid, f"refs/remotes/origin/{branch}"):
            raise SyncError(f"patch source {branch} needs review: accepted merge commit is not an ancestor of the selected source")
    merged = bool(accepted)
    if any(p.get("state") == "OPEN" for p in prs):
        raise SyncError(f"patch source {branch} still needs review: {rebase} has an open PR; finish that upgrade first")
    if any(p.get("state") == "CLOSED" for p in prs) and not merged:
        raise SyncError(f"patch source {branch} still needs review: {rebase} was closed without merging")
    if g.ref_exists(f"refs/remotes/origin/{rebase}") and not merged:
        raise SyncError(f"patch source {branch} still needs review: {rebase} exists without an accepted merged PR")
    source_tree = g.out("rev-parse", f"refs/remotes/origin/{branch}^{{tree}}")
    base_tree = g.out("rev-parse", f"refs/upstream-tags/{base_tag}^{{tree}}")
    if source_tree == base_tree:
        raise SyncError(f"patch source {branch} needs review: it is a raw upstream baseline, not an established VDS patch queue")


def patch_id(g: Git, sha: str) -> str:
    diff = g.out("show", "--format=", "--no-color", "--no-ext-diff", sha)
    if not diff:
        return ""
    out = g.run("patch-id", "--stable", input=diff + "\n").stdout.split()
    return out[0] if out else ""


def range_patch_ids(g: Git, rng: str) -> Dict[str, str]:
    ids = {}
    for sha in g.lines("rev-list", "--no-merges", rng):
        pid = patch_id(g, sha)
        if pid:
            ids[pid] = sha
    return ids


def cherry_picked_from(g: Git, rng: str) -> Dict[str, str]:
    """original sha -> commit, from ``(cherry picked from commit X)`` trailers in ``rng``."""
    out = {}
    for sha in g.lines("rev-list", "--no-merges", rng):
        for m in re.finditer(r"\(cherry picked from commit ([0-9a-f]{7,40})\)", g.out("log", "-1", "--format=%B", sha)):
            out[m.group(1)] = sha
    return out


def _pick_without_stamp_files(g: Git, p: Patch, head_before: str) -> None:
    """Apply ``p`` except its stamp-only files, which keep HEAD's content (they are regenerated
    for the new version after the build, like a stamp-only patch)."""
    r = g.run("cherry-pick", "-n", "--allow-empty-message", p.sha, check=False)
    for f in p.stamp_files:
        if g.run("cat-file", "-e", f"HEAD:{f}", check=False).returncode == 0:
            g.run("checkout", "HEAD", "--", f, check=False)
        else:
            g.run("rm", "-q", "-f", "--cached", "--ignore-unmatch", "--", f, check=False)
            g.run("clean", "-q", "-f", "--", f, check=False)
    unmerged = g.lines("diff", "--name-only", "--diff-filter=U")
    if unmerged or (r.returncode != 0 and not g.lines("diff", "--cached", "--name-only")):
        p.status = "conflict"
        p.files = unmerged or ["(see git output)"]
        if not unmerged:
            p.note = (r.stderr.strip().splitlines() or [""])[-1][:200]
        g.run("cherry-pick", "--abort", check=False)
        g.run("reset", "--hard", "-q", head_before, check=False)
        return
    if not g.lines("diff", "--cached", "--name-only"):
        g.run("cherry-pick", "--quit", check=False)
        g.run("reset", "--hard", "-q", head_before, check=False)
        p.status = "already-upstream"
        return
    message = g.out("log", "-1", "--format=%B", p.sha).rstrip("\n")
    message += f"\n\n(cherry picked from commit {p.sha}; stamp-only files left for regeneration: {', '.join(p.stamp_files)})"
    author = g.out("log", "-1", "--format=%an <%ae>", p.sha)
    date = g.out("log", "-1", "--format=%aI", p.sha)
    g.run("commit", "-q", "--allow-empty-message", f"--author={author}", f"--date={date}", "-m", message)
    g.run("cherry-pick", "--quit", check=False)
    p.status = "applied"
    p.note = "pins in " + ", ".join(p.stamp_files) + " regenerated after build"


def try_pick(g: Git, p: Patch) -> None:
    """Cherry-pick ``p`` onto HEAD. Sets p.status to applied / already-upstream / conflict."""
    head_before = g.out("rev-parse", "HEAD")
    if p.stamp_files:
        _pick_without_stamp_files(g, p, head_before)
        return
    r = g.run("cherry-pick", "-x", "--allow-empty-message", p.sha, check=False)
    if r.returncode == 0:
        p.status = "applied" if g.out("rev-parse", "HEAD") != head_before else "already-upstream"
        return
    unmerged = g.lines("diff", "--name-only", "--diff-filter=U")
    if not unmerged and g.ref_exists("CHERRY_PICK_HEAD"):
        # Applied cleanly but produced no change: upstream already has it.
        skip = g.run("cherry-pick", "--skip", check=False)
        if skip.returncode == 0:
            p.status = "already-upstream"
            return
    p.status = "conflict"
    p.files = unmerged or ["(see git output)"]
    if not unmerged:
        p.note = (r.stderr.strip().splitlines() or [""])[-1][:200]
    g.run("cherry-pick", "--abort", check=False)
    g.run("reset", "--hard", "-q", head_before, check=False)


# --------------------------------------------------------------------------- reporting


STATUS_TEXT = {
    "applied": "applied",
    "already-upstream": "already upstream (empty, dropped)",
    "conflict": "CONFLICT",
    "missing": "not on the branch yet",
    "pending": "pending",
    "regenerate-after-build": "regenerate after build (not cherry-picked)",
}


def patch_table(res: Result) -> str:
    rows = ["| # | Patch | Status | Notes |", "| --- | --- | --- | --- |"]
    for i, p in enumerate(res.patches, 1):
        notes = []
        if p.files and p.status in ("conflict", "missing"):
            notes.append("conflicts in " + ", ".join(f"`{f}`" for f in p.files))
        if p.note:
            notes.append(p.note.replace("|", "\\|"))
        if p.stamp_only:
            notes.append("version stamp only")
        if p.has_pins:
            notes.append("touches a VDS version pin in Package.swift: check it names the right fork release")
        subject = p.subject.replace("|", "\\|")
        rows.append(f"| {i} | `{p.sha[:10]}` {subject} | {STATUS_TEXT.get(p.status, p.status)} | {'; '.join(notes)} |")
    return "\n".join(rows)


def manual_steps(cfg: ForkConfig, res: Result) -> str:
    new = res.upstream_tag
    tag = f"{new}{cfg.tag_sep}1"
    manual_only = res.action in ("manual-only", "would-manual-only")
    if manual_only:
        steps = [f"1. No rebase PR is needed. Manually create or review `{res.new_branch}` from upstream `{new}`; this run writes no remote branches."]
    else:
        steps = [
            f"1. Resolve anything listed above on `{res.rebase_branch}`, then merge this PR into `{res.new_branch}` (merge commit or rebase; do not squash, so each patch stays one commit).",
        ]
    regen: List[str] = []
    for p in res.patches:
        if p.stamp_only or p.stamp_files:
            regen += [h for h in p.stamp_how if h not in regen]
    if regen:
        steps.append("2. Regenerate version stamps on `{}`:\n{}".format(
            res.new_branch, "\n".join(f"   - {h}" for h in regen)))
    else:
        steps.append("2. No version stamps to regenerate.")
    if cfg.name == "skipstone" and manual_only:
        steps.append(f"3. After reviewing and stamping the source, run skipstone-build manually and review its draft release `{tag}`; publishing stays manual.")
        steps.append(f"4. Tag `{tag}` at the reviewed source commit the draft targets, then publish manually.")
    elif cfg.name == "skipstone":
        steps.append(f"3. Review the draft release `{tag}` made by the skipstone-build workflow (binary + identical-output summary). Publishing is manual: if you rebuild after stamping, re-run that workflow first.")
        steps.append(f"4. Tag `{tag}` at the merged `{res.new_branch}` head (it must be the commit the draft release targets) and publish the release.")
    else:
        steps.append(f"3. Tag `{tag}` at the merged `{res.new_branch}` head and push the tag; create the GitHub release if this fork publishes one.")
    steps.append(f"{len(steps) + 1}. Verify the published `{tag}` tag peels to the reviewed source commit and record that full SHA. Pin that immutable `.revision(\"<verified-commit>\")` in GeneFox (root and FoxKit manifests, lockfiles and SkipForkPinContractTests together) and in any consuming fork, then run the full gate. Never clear fingerprints to bypass a revision mismatch.")
    return "\n".join(steps)


def pr_title(res: Result) -> str:
    return f"Rebase VDS patches onto upstream {res.upstream_tag}"


def pr_body(cfg: ForkConfig, res: Result) -> str:
    up = res.upstream_changes
    shown = up[:MAX_UPSTREAM_LINES]
    more = "" if len(up) <= MAX_UPSTREAM_LINES else f"\n- ... and {len(up) - MAX_UPSTREAM_LINES} more (see the compare link)"
    upstream_list = "\n".join(f"- {ln}" for ln in shown) + more if shown else "- (no non-merge commits)"
    conflicts = res.conflicts
    head = (f"**Draft: {len(conflicts)} patch(es) did not apply.** The patches that applied are committed on "
            f"`{res.rebase_branch}`; resolve the rest there (`git cherry-pick -x <sha>` keeps the "
            "provenance trailer this bot reads), then mark the PR ready."
            if conflicts else "All code patches applied cleanly.")
    merges = (f"\n\n{res.skipped_merges} merge commit(s) on `{res.old_branch}` were skipped." if res.skipped_merges else "")
    return f"""{BODY_MARKER}
Upstream [{cfg.upstream}](https://github.com/{cfg.upstream}) released **{res.upstream_tag}** ({res.upstream_source}). This PR carries our patches from `{res.old_branch}` (based on upstream `{res.base_tag}`) onto `{res.new_branch}`, which was created at the upstream `{res.upstream_tag}` tag.

{head}

## Our patches

{patch_table(res)}{merges}

## Upstream changes since our base ({res.upstream_count} commits)

Compare: {res.compare_url}

{upstream_list}

## After merging (manual)

{manual_steps(cfg, res)}

_Opened by [skip-fork-sync](https://github.com/vindhyadatascience/skip-fork-sync). Re-runs update the status comment below; they never force-push this branch._
"""


def status_comment(res: Result) -> str:
    pending = res.conflicts
    if pending:
        tail = f"{len(pending)} patch(es) still to resolve on `{res.rebase_branch}`."
    else:
        tail = "Every code patch is on the branch. If this PR is a draft, it can be marked ready."
    return f"""{STATUS_MARKER}
**Patch status** for `{res.rebase_branch}` at `{res.rebase_head[:12]}` (upstream `{res.upstream_tag}`, patches from `{res.old_branch}`):

{patch_table(res)}

{tail}
"""


def plan_text(cfg: ForkConfig, res: Result) -> str:
    lines = [f"== {cfg.name} ({cfg.fork}): {res.action}"]
    if res.message:
        lines.append(f"   {res.message}")
    if res.upstream_tag:
        lines.append(f"   upstream {res.upstream_source}: {res.upstream_tag}")
    if res.old_branch and res.base_tag:
        lines.append(f"   patches from: {res.old_branch} (base {res.base_tag}), {len(res.patches)} patch(es)")
    elif res.old_branch:
        lines.append(f"   our newest branch: {res.old_branch}")
    if res.action in ("would-create", "created", "refreshed", "would-refresh",
                      "manual-only", "would-manual-only", "blocked"):
        lines.append(f"   upstream changes since base: {res.upstream_count} commits ({res.compare_url})")
        lines.append(f"   branches: {res.new_branch} @ {res.upstream_tag}, {res.rebase_branch}")
        for i, p in enumerate(res.patches, 1):
            extra = f" [{', '.join(p.files)}]" if p.files and p.status in ("conflict", "missing") else ""
            pins = " [pins]" if p.has_pins else ""
            lines.append(f"   {i}. {p.sha[:10]} {STATUS_TEXT.get(p.status, p.status)}{extra}{pins}: {p.subject}")
        if res.action in ("manual-only", "would-manual-only", "blocked"):
            lines.append("   no rebase PR or remote branches written by this run")
        else:
            lines.append(f"   PR: {'DRAFT ' if res.conflicts else ''}{res.rebase_branch} -> {res.new_branch}"
                         + (f" {res.pr_url}" if res.pr_url else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- main flow


@dataclass
class Options:
    dry_run: bool = False
    workdir: Optional[str] = None
    token: Optional[str] = None
    rehearse: bool = False  # dry-run only: plan as if vds/<new> and rebase/<new> did not exist
    log: Callable[[str], None] = lambda s: print(s, file=sys.stderr)


def clone(cfg: ForkConfig, gh: GitHub, dest: str, env: Dict[str, str]) -> Git:
    base = Git(os.path.dirname(dest), env)
    base.run("clone", "--quiet", "--no-tags", gh.repo_url(cfg.fork), dest)
    g = Git(dest, env)
    g.run("config", "user.name", BOT_NAME)
    g.run("config", "user.email", BOT_EMAIL)
    g.run("config", "advice.detachedHead", "false")
    g.run("remote", "add", "upstream", gh.repo_url(cfg.upstream))
    g.run("fetch", "--quiet", "--no-tags", "upstream",
          "+refs/tags/*:refs/upstream-tags/*", "+refs/heads/main:refs/remotes/upstream/main")
    return g


def sync_fork(cfg: ForkConfig, gh: GitHub, opts: Options) -> Result:
    res = Result(fork=cfg.name)
    own_tmp = opts.workdir is None
    root = opts.workdir or tempfile.mkdtemp(prefix="skip-fork-sync-")
    dest = os.path.join(root, cfg.name)
    if os.path.exists(dest):
        shutil.rmtree(dest)
    os.makedirs(root, exist_ok=True)
    try:
        g = clone(cfg, gh, dest, git_env(opts.token))
        _sync(cfg, gh, opts, g, res)
    finally:
        if own_tmp:
            shutil.rmtree(root, ignore_errors=True)
    return res


def _sync(cfg: ForkConfig, gh: GitHub, opts: Options, g: Git, res: Result) -> None:
    tags = upstream_tags(g)
    if not tags:
        raise SyncError(f"{cfg.upstream} has no semver tags")
    latest = gh.latest_release(cfg.upstream)
    if latest and latest in tags:
        res.upstream_tag, res.upstream_source = latest, "latest release"
    else:
        res.upstream_tag = max(tags, key=lambda t: (tags[t], t))
        res.upstream_source = "highest tag" + (f" (latest release {latest!r} is not a semver tag)" if latest else "")
    new = res.upstream_tag
    new_v = tags[new]
    new_commit = g.out("rev-parse", f"refs/upstream-tags/{new}^{{commit}}")

    branches = vds_branches(g)
    if not branches:
        raise SyncError(f"{cfg.fork} has no vds/<version> branch to take patches from")
    same = [b for b, v in branches.items() if v == new_v]
    res.new_branch = sorted(same, key=len)[-1] if same else f"vds/{new}"
    res.rebase_branch = f"rebase/{new}"
    newer = [b for b, v in branches.items() if v > new_v]
    if newer:
        res.action = "up-to-date"
        res.message = f"{', '.join(sorted(newer))} is already newer than upstream {new}"
        return

    rehearse = opts.dry_run and opts.rehearse
    rebase_exists = g.ref_exists(f"refs/remotes/origin/{res.rebase_branch}") and not rehearse
    new_exists = bool(same) and not rehearse
    prs = [] if rehearse else gh.find_prs(cfg.fork, res.rebase_branch)
    open_pr = next((p for p in prs if p.get("state") == "OPEN"), None)
    merged_pr = next((p for p in prs if p.get("state") == "MERGED"), None)
    closed_pr = next((p for p in prs if p.get("state") == "CLOSED"), None)
    if rehearse:
        res.message = (f"REHEARSAL: existing {res.new_branch}, {res.rebase_branch} and PRs ignored; "
                       "local only, nothing is pushed")
    elif not open_pr and (new_exists or rebase_exists) and not (rebase_exists and not merged_pr and not closed_pr):
        res.old_branch = res.new_branch if new_exists else ""
        if merged_pr:
            res.action, res.pr_number, res.pr_url = "up-to-date", merged_pr["number"], merged_pr.get("url", "")
            res.message = f"PR #{merged_pr['number']} ({res.rebase_branch}) is merged"
        elif closed_pr:
            res.action, res.pr_number, res.pr_url = "closed", closed_pr["number"], closed_pr.get("url", "")
            res.message = (f"PR #{closed_pr['number']} was closed without merging; delete {res.rebase_branch} "
                           f"and {res.new_branch} in {cfg.fork} to let the bot try again")
        else:
            res.action = "up-to-date"
            res.message = f"{res.new_branch} already exists"
        return

    older = {b: v for b, v in branches.items() if v < new_v}
    if not older:
        res.action = "up-to-date"
        res.old_branch = res.new_branch
        res.message = f"{res.new_branch} is our only vds branch; there is nothing older to take patches from"
        return
    res.old_branch = max(older, key=lambda b: (older[b], b))
    old_ref = f"refs/remotes/origin/{res.old_branch}"
    res.base_tag = base_tag_of(g, old_ref, older[res.old_branch], tags)
    require_reviewed_patch_source(g, gh, cfg, res.old_branch, res.base_tag)
    base_ref = f"refs/upstream-tags/{res.base_tag}"

    res.skipped_merges = len(g.lines("rev-list", "--merges", f"{base_ref}..{old_ref}"))
    for sha in g.lines("rev-list", "--reverse", "--no-merges", "--topo-order", f"{base_ref}..{old_ref}"):
        res.patches.append(classify_patch(g, cfg, sha, new))

    res.upstream_changes = g.lines("log", "--no-merges", "--format=%h %s", f"{base_ref}..refs/upstream-tags/{new}")
    res.upstream_count = len(res.upstream_changes)
    res.compare_url = f"https://github.com/{cfg.upstream}/compare/{res.base_tag}...{new}"

    if open_pr or rebase_exists:
        _refresh(cfg, gh, opts, g, res, open_pr, base_ref)
    else:
        _create(cfg, gh, opts, g, res, new_commit)


def _apply_all(g: Git, res: Result, start: str) -> None:
    g.run("checkout", "-q", "-B", res.rebase_branch, start)
    for p in res.patches:
        if p.stamp_only:
            p.status = "regenerate-after-build"
            continue
        try_pick(g, p)
    res.rebase_head = g.out("rev-parse", "HEAD")


def no_code_rebase(g: Git, opts: Options, res: Result, base: str) -> bool:
    """No empty PRs, including safe retries after a prior run pushed equal refs.

    Leave all remote refs alone. Regeneration is a manual follow-up, while a queue with
    unresolved code must stay visibly blocked rather than masquerading as up to date.
    """
    if g.out("rev-parse", res.rebase_head + "^{tree}") != g.out("rev-parse", base + "^{tree}"):
        return False
    if res.conflicts:
        res.action = "blocked"
        res.message = "No file changes survived and code patches remain unresolved; review the listed patches manually. Remote refs retained."
    else:
        res.action = "would-manual-only" if opts.dry_run else "manual-only"
        res.message = "No file changes remain after dropping upstream/stamp-only patches; no empty PR is created. Version regeneration and release remain manual."
    return True


def _create(cfg: ForkConfig, gh: GitHub, opts: Options, g: Git, res: Result, new_commit: str) -> None:
    _apply_all(g, res, new_commit)
    res.draft = bool(res.conflicts)
    if no_code_rebase(g, opts, res, new_commit):
        return
    if opts.dry_run:
        res.action = "would-create"
        return
    opts.log(f"[{cfg.name}] syncing {cfg.fork} main with upstream")
    gh.sync_fork_main(cfg.fork)
    g.run("push", "--quiet", "--atomic", "origin",
          f"{new_commit}:refs/heads/{res.new_branch}",
          f"refs/heads/{res.rebase_branch}:refs/heads/{res.rebase_branch}")
    pr = gh.create_pr(cfg.fork, res.new_branch, res.rebase_branch, pr_title(res), pr_body(cfg, res), res.draft)
    res.pr_number, res.pr_url = pr["number"], pr.get("url", "")
    _upsert_comment(cfg, gh, res)
    res.action = "created"


def _refresh(cfg: ForkConfig, gh: GitHub, opts: Options, g: Git, res: Result, open_pr: Optional[dict],
             base_ref: str) -> None:
    """The rebase branch exists: report each patch's state on it. Never rewrites the branch."""
    rebase_ref = f"refs/remotes/origin/{res.rebase_branch}"
    new_ref = f"refs/upstream-tags/{res.upstream_tag}"
    res.rebase_head = g.out("rev-parse", rebase_ref)
    on_branch = range_patch_ids(g, f"{new_ref}..{rebase_ref}")
    trailers = cherry_picked_from(g, f"{new_ref}..{rebase_ref}")
    upstream_ids = range_patch_ids(g, f"{base_ref}..{new_ref}")
    unresolved: List[Patch] = []
    for p in res.patches:
        pid = patch_id(g, p.sha)
        if p.stamp_only:
            p.status = "regenerate-after-build"
        elif (pid and pid in on_branch) or any(p.sha.startswith(k) or k.startswith(p.sha) for k in trailers):
            p.status = "applied"
        elif pid and pid in upstream_ids:
            p.status = "already-upstream"
        else:
            p.status = "missing"
            unresolved.append(p)
    if unresolved:
        # Trial-apply on a scratch branch to name the files that still conflict.
        g.run("checkout", "-q", "-B", "skip-fork-sync-trial", rebase_ref)
        for p in unresolved:
            trial = Patch(sha=p.sha, subject=p.subject)
            try_pick(g, trial)
            if trial.status == "conflict":
                p.files = trial.files
                p.note = "still conflicts on the current branch head"
            elif trial.status == "applied":
                p.note = "applies cleanly on the current branch head: `git cherry-pick -x " + p.sha[:12] + "`"
    res.draft = bool(res.conflicts)
    if open_pr is None and no_code_rebase(g, opts, res, new_ref):
        return
    if opts.dry_run:
        res.action = "would-refresh"
        if open_pr:
            res.pr_number, res.pr_url = open_pr["number"], open_pr.get("url", "")
        return
    if open_pr is None:
        if not g.ref_exists(f"refs/remotes/origin/{res.new_branch}"):
            g.run("push", "--quiet", "origin",
                  f"{g.out('rev-parse', new_ref + '^{commit}')}:refs/heads/{res.new_branch}")
        open_pr = gh.create_pr(cfg.fork, res.new_branch, res.rebase_branch, pr_title(res), pr_body(cfg, res), res.draft)
    else:
        body = pr_body(cfg, res)
        if (open_pr.get("body") or "").replace("\r\n", "\n").strip() != body.strip():
            gh.edit_pr_body(cfg.fork, open_pr["number"], body)
    res.pr_number, res.pr_url = open_pr["number"], open_pr.get("url", "")
    _upsert_comment(cfg, gh, res)
    res.action = "refreshed"


def _upsert_comment(cfg: ForkConfig, gh: GitHub, res: Result) -> None:
    body = status_comment(res)
    existing = [c for c in gh.list_comments(cfg.fork, res.pr_number) if STATUS_MARKER in (c.get("body") or "")]
    if not existing:
        gh.create_comment(cfg.fork, res.pr_number, body)
    elif (existing[0].get("body") or "").replace("\r\n", "\n").strip() != body.strip():
        gh.edit_comment(cfg.fork, existing[0]["id"], body)


# --------------------------------------------------------------------------- CLI


def write_github_output(res: Result) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        for k, v in res.outputs().items():
            f.write(f"{k}={v}\n")


def write_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("```\n" + text + "\n```\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--fork", action="append", choices=sorted(FORKS) + ["all"],
                    help="fork to sync (repeatable; default: all six)")
    ap.add_argument("--dry-run", action="store_true", help="print the plan; push nothing, write nothing to GitHub")
    ap.add_argument("--rehearse", action="store_true",
                    help="with --dry-run: ignore an existing vds/<latest> branch and rebase PR and rehearse the "
                         "cherry-picks from the next-older vds branch (validates the tool against real history)")
    ap.add_argument("--workdir", help="scratch directory for clones (default: a temp dir, removed afterwards)")
    ap.add_argument("--json", dest="json_out", help="write the results as JSON to this file")
    ap.add_argument("--github-output", action="store_true",
                    help="write step outputs to $GITHUB_OUTPUT (requires exactly one --fork)")
    args = ap.parse_args(argv)

    names = args.fork or ["all"]
    names = sorted(FORKS, key=list(FORKS).index) if "all" in names else list(dict.fromkeys(names))
    if args.github_output and len(names) != 1:
        ap.error("--github-output needs exactly one --fork")
    token = token_from_env()
    if not args.dry_run and not token:
        print("warning: neither FORK_SYNC_TOKEN nor GH_TOKEN is set; using gh's own login and git's credentials",
              file=sys.stderr)
    gh = GitHub(token)
    if args.rehearse and not args.dry_run:
        ap.error("--rehearse requires --dry-run")
    opts = Options(dry_run=args.dry_run, workdir=args.workdir, token=token, rehearse=args.rehearse)

    failed = False
    results = []
    for name in names:
        cfg = FORKS[name]
        try:
            res = sync_fork(cfg, gh, opts)
        except SyncError as e:
            res = Result(fork=name, action="error", message=str(e))
            failed = True
        if res.action == "blocked":
            failed = True
        text = plan_text(cfg, res)
        if res.action in ("manual-only", "would-manual-only"):
            text += "\n\n--- Manual follow-up ---\n" + manual_steps(cfg, res)
        if args.dry_run and res.action in ("would-create", "would-refresh"):
            text += "\n\n--- PR body that would be used ---\n" + pr_body(cfg, res)
        print(text + "\n", flush=True)
        write_step_summary(text)
        if args.github_output:
            write_github_output(res)
        results.append(res)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump([{**r.outputs(), "fork": r.fork, "message": r.message,
                        "patches": [{"sha": p.sha, "subject": p.subject, "status": p.status, "files": p.files,
                                     "stamp_only": p.stamp_only, "has_pins": p.has_pins} for p in r.patches]}
                       for r in results], f, indent=2)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
