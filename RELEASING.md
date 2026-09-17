# Releasing Isocenter

`main` is the development branch. Releases are cut from it onto a release
branch, frozen there, tagged, and published by hand. Nothing publishes
itself: no push, tag or GitHub Release uploads to PyPI or deploys the
documentation site on its own, except that pushing the newest release tag
deploys that release's documentation.

## Names

| What | Name | Example |
|---|---|---|
| Development | `main` | |
| Work on a change | `fix/…`, `feat/…`, `ci/…`, `docs/…` | `fix/691-ambiguous-vr-at-read` |
| A release line | `release/X.Y` | `release/0.9` |
| A published version | tag `vX.Y.Z` | `v0.9.8` |

`v*` tags are admin-only. The repository ruleset "Protect release tags"
covers `refs/tags/v*` and restricts creating, updating and deleting them
and non-fast-forward pushes; only the repository admin role bypasses it.
Every step below that creates, moves or deletes a `v*` tag is an admin's
step. Pushing one also deploys the documentation (see "Documentation
site"), so the restriction covers that too.

There is one branch per minor line, not per version. `release/0.9` holds
`v0.9.8` and every `0.9.z` patch after it. A branch name and a tag name
never collide, so `git checkout v0.9.8` always means the published commit.

## Changes land on `main`

1. The architect writes the specification.
2. The developer writes the tests first, then the code, on a work branch
   off `main`.
3. Before pushing, the developer rebases onto current `main` and runs, on
   **both 3.12 and 3.14t** at the commit being pushed: the new tests, and
   the tests that cover what the change touched. Until `pytest --changed`
   exists (#707) that is each touched module's row in
   `scripts/mutation_probe.py`'s `TARGETS`; afterwards it is what
   `pytest --changed` selects. Both interpreters must pass. Record the SHA
   in each run's log header. Run the two one after the other, or 3.14t in a
   `git archive` copy of the same commit, so they do not share repo-root
   `*.db` and `*.lock` files. **The full suite is not run.**
4. Open a pull request into `main`.
5. An adversarial reviewer reviews the tests and the code **as rebased on
   current `main`**. A conflict with work merged since the branch was cut,
   textual or semantic, is part of this review: two changes that each pass
   alone and break together are the reviewer's finding. Changes go back to
   the developer, who consults the architect where the design is in
   question. The first review covers the whole PR. A re-review may cover
   only the delta, or the whole PR if the delta touches what an earlier
   pass relied on. **Every pass names the SHA it approved.** If `main`
   moves before the merge, the developer rebases and repeats step 3, and
   the reviewer re-reviews the delta.
6. When the reviewer passes, merge into `main` pinned to that SHA
   (`gh pr merge --match-head-commit <sha>`), and delete the work branch.

**Code on `main` has been tested locally and reviewed. It has not been run
against the full suite** (owner's ruling, 2026-09-17). The full suite is the
integration test, and it runs when a release is about to happen: see
"Cutting a release". A regression found there is fixed on `main` like any
other change, and the release is cut again. Nothing between a merge and a
release runs the full suite, and no step here depends on how work happens
to be grouped into bunches or waves.

Until 2026-09-17 step 3 required the full suite on both interpreters before
every push. It cost about forty minutes a push and made the local tier and
the release tier the same thing.

`main` has no required status checks, and no CI runs on a push or a pull
request, into `main` or into a release branch. `tests.yml` runs only when
dispatched by hand (`gh workflow run tests.yml --ref <branch>`) or when
`publish.yml` calls it at release. A dispatched run is information for the
reviewer, not a gate. The gate is the local tests on both interpreters and
the review.

## Cutting a release

A release branch is a fully tested cut from `main`, then frozen: it takes
fixes, never features.

1. **Choose the commit** on `main`. Run the full suite on 3.12 and 3.14t at
   that SHA. **This is the integration test**, and the first time this code
   meets the whole suite. A failure is fixed on `main` by the procedure
   above, and step 1 starts again at the new commit.
2. **Cut the branch:** `git switch -c release/X.Y <sha>`, then
   `git push -u origin release/X.Y`. For a patch to an existing line, see
   below instead.
3. **Make the release commit on the branch.** It contains exactly:
   - `isocenter/_version.py`: `__version__ = "X.Y.Z"`. This is the one place
     the number is declared; `setup.py` parses it and
     `isocenter.__version__` re-exports it.
   - `CITATION.cff`: `version` and `date-released`.
   - `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
     Right after a release commit a release branch has no `[Unreleased]`
     section; the file describes the code on the branch. A patch's first
     fix adds one back (see "Patch releases").

   Run the full suite on both interpreters at this commit
   (`tests/test_version_contract.py` checks that the three files agree).
   Open it as a PR into `release/X.Y`, have it reviewed, and merge it the
   same way as any other PR.
4. **Rehearse on TestPyPI**, from the branch, and **do not tag until the
   rehearsal passes**:
   `gh workflow run publish.yml --ref release/X.Y -f target=testpypi`.
   This runs the same build gates and the full four-version matrix while
   the real version number is still unspent. `test-supported` (3.13, 3.14)
   only reports and cannot block the upload, so a red job there is a
   decision: fix it, or delete that classifier from `setup.py` in this
   release. Rerun a red job to learn whether it is deterministic, not to
   make it go away. A rehearsal consumes the version number on TestPyPI
   only, so a second rehearsal of the same version cannot upload; its
   build gates and test matrix still run.
5. **Tag** the release commit (an admin step): `git tag -a vX.Y.Z -m "Isocenter X.Y.Z"` on
   `release/X.Y`, then `git push origin vX.Y.Z`. **Pushing the tag deploys
   the documentation** for vX.Y.Z (see below), before the version is on
   PyPI; nothing else runs. That window is why step 4 must pass first.
6. **Publish** from the tag:
   `gh workflow run publish.yml --ref vX.Y.Z -f target=pypi`. The build job
   refuses the run unless the ref is a `v*` tag and the tag, the built
   wheel and `isocenter/_version.py` all name the same version. It then
   checks the wheel carries its own resources, runs the 3.12 and 3.14t
   floor (which blocks the upload) and 3.13 and 3.14 (which only report),
   and uploads by Trusted Publishing.
7. **Create the GitHub Release** for `vX.Y.Z`, with the `[X.Y.Z]` changelog
   section as its notes. This does not publish anything. Zenodo archives it
   and mints the version DOI.
8. **Bring the release record back to `main`** in an ordinary PR into
   `main`, made of ordinary commits, never a merge of the release branch
   (see "Never merge a release branch into `main`"). It:
   - copies the `## [X.Y.Z] - YYYY-MM-DD` section into `main`'s
     `CHANGELOG.md`, below `[Unreleased]`, and removes the entries it
     contains from `[Unreleased]`;
   - sets `main`'s `isocenter/_version.py` and `CITATION.cff` to `X.Y.Z`, if
     X.Y is the newest release line. Between releases, `main` declares the
     newest version released from it, and `[Unreleased]` above that
     section holds everything since. A patch to an older line leaves
     `main`'s version alone.

If the publish run fails before the upload job starts, nothing is spent.
Fix the release branch, delete the tag locally and on `origin`, re-tag,
and dispatch again (deleting and re-pushing a `v*` tag needs the admin
bypass, see "Names"). The docs deployed from the first tag stay live until
the new tag's push redeploys them. Once the upload has succeeded, the
version is spent forever. A defect found then is a patch release, never
a re-tag.

## Patch releases

1. Branch the fix from `release/X.Y`, not from `main`. Give it a work branch
   name as usual.
2. Develop and review it exactly as a change to `main` is, with the PR
   targeting `release/X.Y`. Keep the fix and its tests in their own commits,
   and put the changelog entry in a separate commit, under
   `## [Unreleased]` at the top of the branch's `CHANGELOG.md`. The first
   fix of a patch adds that heading back.
3. **Forward-port the fix to `main` by cherry-pick**, in an ordinary PR
   into `main`: `git cherry-pick -x <fix commits>` onto a work branch off
   `main`. Leave out the changelog commit. Resolve any conflict as the code
   on `main` requires, and have the PR reviewed like any other. If the
   fix does not apply to `main` (the code is gone there), say so in the
   release-branch PR instead. The changelog entry reaches `main` with the
   released section in step 8, once the patch ships.
4. Release it by following "Cutting a release" from step 3, with version
   `X.Y.Z+1`. The branch already exists, so steps 1 and 2 do not apply.
   Step 3 renames the branch's `[Unreleased]` to `[X.Y.Z+1]`, and step 8
   copies that section to `main`.

A fix that applies only to `main` (already gone from the release line) is an
ordinary change to `main`.

### Never merge a release branch into `main`

A merge of `release/X.Y` into `main` carries the release commits across
with the fixes, and git merges them without a conflict:
- `main`'s `isocenter/_version.py` and `CITATION.cff` silently take the
  release branch's number;
- `main`'s `[Unreleased]` heading is replaced by the release's dated
  heading, filing `main`'s unreleased work under a version that never
  contained it.

The three version files still agree with each other afterwards, so the
version contract tests stay green. That is why fixes travel by
cherry-pick and the release record by an ordinary copy commit (step 8).
`tests/test_version_contract.py::test_the_changelog_opens_with_unreleased_or_the_declared_release`
catches the common shape: a top heading naming a version `_version.py`
does not. It cannot catch a merge that also carried `_version.py` to the
same number.

## Documentation site

The site follows the **latest release tag**, not `main`. `docs.yml` runs on
a pushed `v*` tag and on manual dispatch. Its `guard` job refuses anything
but the highest `v*` tag by version order, and the `deploy` job runs only
after the guard passes. As a result:

- Pushing the tag for the newest release deploys its documentation. That
  happens when the tag is pushed, before the version is published (step 5).
- A patch tag on an older line, such as `v0.9.9` after `v0.10.0`, deploys
  nothing. A refused run is outside the deploy's concurrency group, so it
  cannot cancel a deploy in progress either. The newer release's site
  stays.
- A manual run from a branch deploys nothing. To redeploy the current
  release, dispatch from its tag: `gh workflow run docs.yml --ref vX.Y.Z`.
- Pre-releases: `a`, `b` and `rc` suffixes sort below their release
  (`v1.0.0rc1` < `v1.0.0`), so a candidate's tag does not block its
  release. A pre-release of a **later** version does outrank the current
  release (`v1.0.1rc1` > `v1.0.0`) and blocks the current release's deploy
  until the later version is released or the pre-release tag is deleted.

The deploy pushes the built site to the `gh-pages` branch. GitHub Pages
serves it from there, under the `github-pages` environment, whose
deployment-branch policy admits only `gh-pages`.

To preview unreleased documentation, run `mkdocs serve` locally (it needs
the `docs` extra).

## What cannot be undone

- A version uploaded to PyPI or TestPyPI can never be uploaded again, even
  after deleting it.
- A DOI Zenodo mints for a GitHub Release is permanent. The record's
  metadata can be corrected later; the DOI cannot be reissued.

Trusted Publishing matches on this repository, the workflow **filename**
(`publish.yml`) and the environment names (`pypi`, `testpypi`). Renaming
any of them breaks publishing until the publisher configuration on PyPI is
updated to match.
