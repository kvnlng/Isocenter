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

There is one branch per minor line, not per version. `release/0.9` holds
`v0.9.8` and every `0.9.z` patch after it. A branch name and a tag name
never collide, so `git checkout v0.9.8` always means the published commit.

## Changes land on `main`

1. The architect writes the specification.
2. The developer writes the tests first, then the code, on a work branch
   off `main`. **Nothing is pushed until the full suite passes locally on
   both gate interpreters**, 3.12 and 3.14t, at the commit being pushed.
   Record the SHA in each run's log header. Run 3.14t in a `git archive`
   copy of the same commit, so the two suites do not share repo-root
   `*.db` and `*.lock` files.
3. Open a pull request into `main`.
4. An adversarial reviewer reviews the tests and the code. Changes go back
   to the developer, who consults the architect where the design is in
   question. The first review covers the whole PR. A re-review may cover
   only the delta, or the whole PR if the delta touches what an earlier
   pass relied on. **Every pass names the SHA it approved.**
5. When the reviewer passes, merge into `main` pinned to that SHA
   (`gh pr merge --match-head-commit <sha>`), and delete the work branch.

`main` has no required status checks. `tests.yml` still runs on pull
requests into `main` and on pushes to `main`, and its result is information
for the reviewer. It does not run on pull requests into a release branch.
The gate is the local suite on both interpreters and the review.

## Cutting a release

A release branch is a fully tested cut from `main`, then frozen: it takes
fixes, never features.

1. **Choose the commit** on `main`. Run the full suite on 3.12 and 3.14t at
   that SHA.
2. **Cut the branch:** `git switch -c release/X.Y <sha>`, then
   `git push -u origin release/X.Y`. For a patch to an existing line, see
   below instead.
3. **Make the release commit on the branch.** It contains exactly:
   - `isocenter/_version.py`: `__version__ = "X.Y.Z"`. This is the one place
     the number is declared; `setup.py` parses it and
     `isocenter.__version__` re-exports it.
   - `CITATION.cff`: `version` and `date-released`.
   - `CHANGELOG.md`: rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`.
     On a release branch there is no `[Unreleased]` section; the file
     describes the code on the branch.

   Run the full suite on both interpreters at this commit
   (`tests/test_version_contract.py` checks that the three files agree).
   Open it as a PR into `release/X.Y`, have it reviewed, and merge it the
   same way as any other PR.
4. **Rehearse on TestPyPI**, from the branch:
   `gh workflow run publish.yml --ref release/X.Y -f target=testpypi`.
   This runs the same build gates and the full four-version matrix while
   the real version number is still unspent. `test-supported` (3.13, 3.14)
   only reports and cannot block the upload, so a red job there is a
   decision: fix it, or delete that classifier from `setup.py` in this
   release. Rerun a red job to learn whether it is deterministic, not to
   make it go away. A rehearsal consumes the version number on TestPyPI
   only.
5. **Tag** the release commit: `git tag -a vX.Y.Z -m "Isocenter X.Y.Z"` on
   `release/X.Y`, then `git push origin vX.Y.Z`. Pushing the tag deploys
   the documentation (see below); nothing else runs.
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
8. **Bring the release record back to `main`**: a PR that copies the
   `## [X.Y.Z] - YYYY-MM-DD` section into `main`'s `CHANGELOG.md`, below
   `[Unreleased]`, with the entries it contains removed from `[Unreleased]`.
   Leave `main`'s `_version.py` and `CITATION.cff` alone; `main` moves to
   the next number when the next release is cut.

If the publish run fails before the upload job starts, nothing is spent.
Fix the release branch, delete the tag locally and on `origin`, re-tag,
and dispatch again. Once the upload has succeeded, the version is spent
forever. A defect found then is a patch release, never a re-tag.

## Patch releases

1. Branch the fix from `release/X.Y`, not from `main`. Give it a work branch
   name as usual.
2. Develop and review it exactly as a change to `main` is, with the PR
   targeting `release/X.Y`. Add its changelog entry under a new
   `## [X.Y.Z+1] - YYYY-MM-DD` heading on the branch.
3. After it merges, **merge `release/X.Y` into `main`** in a PR. On
   `CHANGELOG.md`, the release branch is authoritative for the sections it
   has released, and `main` keeps its own `[Unreleased]` on top. The
   fix's entry goes in the released section, not in `[Unreleased]`.
4. Release it from step 3 of "Cutting a release", with version `X.Y.Z+1`
   on the same `release/X.Y` branch.

A fix that applies only to `main` (already gone from the release line) is an
ordinary change to `main`.

## Documentation site

The site follows the **latest published release**, not `main`. `docs.yml`
runs on a pushed `v*` tag and on manual dispatch, and its first step after
checkout refuses anything but the highest `v*` tag by version order. As a
result:

- Pushing the tag for the newest release deploys its documentation.
- A patch tag on an older line, such as `v0.9.9` after `v0.10.0`, deploys
  nothing. The newer release's site stays.
- A manual run from a branch deploys nothing. To redeploy the current
  release, dispatch from its tag: `gh workflow run docs.yml --ref vX.Y.Z`.

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
