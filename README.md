# skip-fork-sync

Keeps Vindhya Data Science's Skip forks close to upstream. When [skiptools](https://github.com/skiptools)
releases a new version of one of the forked repositories, this repo's scheduled workflow opens a pull
request in our fork that carries our patches onto the new release. Merging, tagging, publishing and
bumping pins stay manual.

| Fork | Upstream | Our release tags |
| --- | --- | --- |
| [vindhyadatascience/skipstone](https://github.com/vindhyadatascience/skipstone) | skiptools/skipstone | `<ver>-vds.N` |
| [vindhyadatascience/skip](https://github.com/vindhyadatascience/skip) | skiptools/skip | `<ver>+vds.N` |
| [vindhyadatascience/skip-ui](https://github.com/vindhyadatascience/skip-ui) | skiptools/skip-ui | `<ver>-vds.N` |
| [vindhyadatascience/skip-foundation](https://github.com/vindhyadatascience/skip-foundation) | skiptools/skip-foundation | `<ver>+vds.N` |
| [vindhyadatascience/skip-lib](https://github.com/vindhyadatascience/skip-lib) | skiptools/skip-lib | `<ver>+vds.N` |

Each fork holds only `main` (a pure mirror of upstream) and our `vds/<upstream version>` branches.

## What it does

`.github/workflows/sync.yml` runs daily (09:23 UTC) and on demand. For each fork,
`scripts/sync_fork.py`:

1. Reads upstream's latest release (`gh api repos/skiptools/<repo>/releases/latest`; falls back to the
   highest semver tag).
2. Finds our newest `vds/*` branch and the upstream tag it is based on. Our patches are the commits on
   that branch that are not in that tag, oldest first (merge commits are skipped and counted).
3. If `vds/<new>` does not exist: syncs the fork's `main` with upstream (`gh repo sync`), creates
   `vds/<new>` at the upstream tag, cherry-picks the patches (`-x`) onto `rebase/<new>`, pushes, and opens
   a PR `rebase/<new> -> vds/<new>`. The PR body lists upstream's commits since our base with a compare
   link, and each patch with its status: applied, already upstream (dropped), CONFLICT (with files), or
   regenerate after build.
4. If any patch conflicts, the PR is a **draft**: the patches that applied are committed, the rest are
   listed with their conflicting files. Resolve them on `rebase/<new>` with `git cherry-pick -x <sha>`
   (the bot recognises the `cherry picked from` trailer).
5. Re-runs are idempotent: an existing PR gets its body refreshed and its single status comment edited
   in place; the rebase branch is never rewritten or force-pushed. A merged PR or an existing
   `vds/<new>` branch means up to date. A PR closed without merging stops the bot for that version until
   you delete `rebase/<new>` and `vds/<new>`.

If dropping version stamps and patches already upstream leaves no file changes, the result is
`manual-only` (`would-manual-only` for a dry run): no empty PR, branch push, or fork-main sync is
attempted. The output retains the patch statuses and manual version-regeneration/release steps.
Existing equal branches left by an earlier failed PR attempt are preserved on retry. If no file
changes survive but code patches still conflict or are missing, the result is `blocked` and the
command exits nonzero without remote writes; resolve the reported queue manually.

A newer release must not use an unfinished upgrade's raw `vds/<version>` base as its patch source.
The bot refuses a source whose previous rebase is closed without merging, or orphaned without an
accepted merged PR. A previous rebase that is still an open PR is **awaiting-review**: the run names
that PR, writes nothing for the fork, and exits 0 until the PR is merged. Acceptance must name that source branch as `baseRefName` and provide a
merge commit that is an ancestor of the selected source; missing provenance fails closed.
A source identical to its upstream base is also ambiguous and requires review. Finish and review that upgrade first; the bot neither substitutes a partial `rebase/*`
branch nor silently skips back to an older patch queue. Original patched VDS source branches and
completed merged upgrades remain usable.

Patches that only change version stamps are not cherry-picked, because their values would be wrong for
the new version. They are listed as **regenerate after build**:

- skipstone: `skipVersion` in `Sources/SkipSyntax/Version.swift`;
- skip: the macOS and Linux `binaryTarget` url/checksum, and the skipstone dependency's `revision:`
  (or upstream's `exact:`), in `Package.swift`.

A commit that changes stamp files and other files (skip's pin commits can also carry a README note) is
applied without its stamp files: the other changes are kept, and the stamps are regenerated. skipstone
and skip opt in (`partial_stamps`); skip-ui, skip-foundation and skip-lib still cherry-pick whole commits.

Patches that change a VDS version pin alongside code (for example skip-foundation's `Package.swift`
pointing at our skip-lib release) are applied and flagged for review.

For skipstone, when a new PR is created, `sync.yml` dispatches `.github/workflows/skipstone-build.yml`.
On `macos-latest` it builds `skip-macos.zip` from `rebase/<new>` as `VDS-BUILD.md` in the skipstone fork
describes (with `skipVersion` stamped `<new>-vds.1` in the build tree only), then runs the
identical-output check (`scripts/skipstone_build_check.sh`): sample packages (skip-lib and
skip-foundation at their latest releases) are built with the stock upstream binary of the same version
(checksum-verified against upstream skip's pin) and with ours, and the generated trees under
`.build/plugins/outputs` must be identical apart from the skipstone version string in the
`.sourcehash` markers. Only the top-level `skipstone` version field in a valid per-module
`.sourcehash` JSON marker is normalized. A marker needs a nonempty module name, unique root
`skipstone` and `sourcehashes` fields, and a dictionary hash map; unexpected or duplicate shapes
remain exact. Code, scientific outputs, binary bytes, nested fields and symlink targets remain exact. On a pass it attaches `skip-macos.zip`, `skip-macos.zip.sha256` and
`check-summary.md` to a **draft** release `<new>-vds.1` on vindhyadatascience/skipstone, and comments on
the PR. It never publishes. The source clone records its full commit in `out/source-commit.txt` before
building; draft attachment consumes that immutable identity without re-resolving the branch.
Local `VDS_ZIP` checks clear any stale source identity and cannot attach a falsely attributed draft.
Missing or malformed source identity refuses attachment. An existing lightweight or annotated
release tag must peel to that same commit before any draft/asset mutation; tag lookup errors
refuse attachment, while a confirmed absent tag is allowed. Tags are never moved. You can also run it by hand
(Actions > skipstone-build > Run workflow, or a
`repository_dispatch` of type `skipstone-rebase` with `client_payload: {version, ref, vds_tag, pr}`).

## The token: `FORK_SYNC_TOKEN`

Both workflows need a repository secret named `FORK_SYNC_TOKEN`. Without it, a scheduled or non-dry
run fails at its first step with instructions; a dry run falls back to the read-only workflow token.

Use either:

- **Fine-grained personal access token** (simplest). GitHub > Settings > Developer settings >
  Personal access tokens > Fine-grained tokens > Generate new token:
  - Resource owner: `vindhyadatascience`
  - Repository access: *Only select repositories*: `skipstone`, `skip`, `skip-ui`, `skip-foundation`,
    `skip-lib` (not this repo, not anything else)
  - Repository permissions: **Contents: Read and write**, **Pull requests: Read and write**,
    **Actions: Read**. (Metadata: Read is added automatically.) Nothing else.
  - Pick an expiry and set a reminder to rotate it. If the organization requires approval for
    fine-grained tokens, an org owner must approve it.
- **GitHub App** installed on those five repositories only, with the same permissions, minting an
  installation token. (That needs an extra token-minting step in the workflows; the PAT is enough.)

Add the secret: this repo > Settings > Secrets and variables > Actions > New repository secret, name
`FORK_SYNC_TOKEN`, value the token. Or from a terminal:

```bash
gh secret set FORK_SYNC_TOKEN --repo vindhyadatascience/skip-fork-sync   # paste the token at the prompt
```

The token is passed to git only as an HTTP header in environment variables (never in a URL or on a
command line), and to `gh` as `GH_TOKEN`. The workflows' own `GITHUB_TOKEN` is limited to
`contents: read` (plus `actions: write` in the sync job, only to dispatch the build workflow).

## Dry run

From GitHub: Actions > sync > Run workflow, choose the fork (or `all`) and tick *Dry run*. The plan and
the PR body it would use are printed in the log and the job summary; nothing is pushed and nothing is
written to GitHub.

Locally (needs Python 3.9+, git, and `gh auth login`; read-only):

```bash
python3 scripts/sync_fork.py --dry-run                        # all five forks
python3 scripts/sync_fork.py --dry-run --fork skip-ui
python3 scripts/sync_fork.py --dry-run --rehearse             # pretend vds/<latest> does not exist yet:
                                                              # replays the previous upgrade from the
                                                              # next-older vds branch, to validate the tool
```

The dry run does the cherry-picks in a scratch clone, so its conflict predictions are exact.

Every sample must contain generated Kotlin, not just markers. Resolve/edit/build/find/manifest
failures propagate explicitly through conditional shell calls. Each attempt discards its stale
manifest, and only a successful helper installs a fresh manifest by atomic rename.

Tests (local bare-repo and command-stub fixtures; no network or compiler builds):

```bash
python3 -m unittest discover -s tests -v
```

## After a rebase PR (manual)

1. Resolve conflicts on `rebase/<new>` if the PR is a draft; mark it ready.
2. Merge into `vds/<new>` with a merge commit or rebase (do not squash: one commit per patch).
3. Regenerate the stamps the PR lists:
   - skipstone: set `skipVersion` to `"<new>-vds.N"` on `vds/<new>`.
   - skip: after the skipstone `<new>-vds.N` release is published, point the macOS and Linux
     `binaryTarget`s at that release's `skip-macos.zip` and `skip-linux.zip`
     (`https://github.com/vindhyadatascience/skipstone/releases/download/<new>-vds.N/...`), set each
     `checksum` to the SHA-256 in the matching `.sha256` file, and point the skipstone dependency at the
     `<new>-vds.N` tag's commit with `revision:`.
   - any fork pinning another fork (skip-foundation -> skip-lib) : bump the pin if that fork got a
     new release.
4. Tag and release:
   - skipstone: check the draft release's binary and check summary. If the merged `vds/<new>` head
     differs from the commit the draft targets (it will, once you commit the stamp), re-run
     skipstone-build with `ref: vds/<new>` so the attached binary is built from the tagged source
     (AGPL: the tag is the corresponding source). Then publish the draft, which creates the tag
     `<new>-vds.1`.
   - others: `git tag <new>-vds.1` (skip-ui) or `git tag '<new>+vds.1'` (skip, skip-foundation,
     skip-lib) at the merged `vds/<new>` head and push it.
5. Verify each published tag peels to the reviewed source commit and record its full SHA.
   For metadata-tagged forks (`skip`, `skip-lib`, `skip-foundation`, tags `+vds.N`), use that
   immutable `.revision("<verified-commit>")` in GeneFox and consuming forks: SwiftPM compares
   metadata variants as the same version, so an exact version does not select the variant.
   Never clear fingerprints to bypass a revision mismatch. `skip-ui` keeps its exact
   prerelease-version pin (`-vds.N`). Update the Homebrew tap for skipstone and run the full gate.
6. Once GeneFox is on the new version, delete the superseded `vds/<old>` branch (its tags stay).
