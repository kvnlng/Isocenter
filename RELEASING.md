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

`main` is the development branch. Work meant for a minor later than the next
unreleased line may merge at any time, but its PR carries that minor's
milestone, so whoever cuts the line can leave it out ("Later releases on an
existing line").

1. The architect writes the specification.
2. The developer writes the tests first, then the code, on a work branch
   off `main`.
3. Before pushing, the developer rebases onto the current tip of the branch
   the PR targets (`main`; `release/X.Y` for a patch) and runs, on **both
   3.12 and 3.14t** at the commit being pushed, the new tests and the tests
   that cover what the change touched: `pytest -v --changed` on each
   interpreter (a patch: `--changed-base=release/X.Y`, with the `=`). It
   prints the rule behind each part of its selection before it runs.
   With a map (`.test-map.json`; "Cutting a release", step 1 builds one),
   a changed function selects the tests that ran it, plus, for code a
   spawned worker runs, the tests of the functions that hand that worker
   to a pool (every `run_parallel(...)` and pool call in `isocenter/`),
   and its module's row as well when no test ran it outside a worker --
   and whatever the map cannot speak for is added back from the module's
   row. Without a map, and for everything a map does not cover, it
   applies exactly these rules (#707):
   - a changed `isocenter/**/*.py`: that module's row in
     `scripts/mutation_probe.py`'s `TARGETS`. A module with no row (it is in
     `NOT_PROBED`) means the whole suite.
   - a changed `tests/test_*.py`: that file, and the test files whose text
     names it (its name without `.py`: test files import from test files).
   - `tests/conftest.py`, anything under `tests/support/`, `setup.py`,
     `pytest.ini`, `.coveragerc`, `pyproject.toml`, `MANIFEST.in`, or any
     file under `isocenter/` that is not Python: the whole suite.
   - any other path: the test files whose text names it --
     `grep -l <basename> tests/test_*.py`, and for a `.py` file its name
     without the suffix as well. If none does: for a path under `docs/` or
     any `*.md`, only the tests that read it by glob or walk (next rule);
     the whole suite for anything else (`scripts/`, `.github/`, root files).
   - whatever else it selected, every changed path also selects the test
     files that read every file of its kind by glob or walk: a
     `tests/test_*.py` that calls `glob`, `iglob`, `rglob`, `os.walk`,
     `os.listdir`, `os.scandir` or `.iterdir()`, and holds either a
     `*.ext` pattern matching the path's name or the path's suffix as a
     whole string (`".py"`, as in `name.endswith(".py")`). That is how a
     page no test names reaches the tests that walk `docs/**/*.md`, and a
     module reaches the ones that read `isocenter/**/*.py` as text
     (`test_source_citations.py`, `test_documented_env_vars.py`, and
     `test_api_coherence.py`, which walks the package with `os.walk`,
     #744). Where the walk goes and what the literal is for are not
     read, so this over-selects; it never narrows. A tree read with
     none of those calls, or kept by no suffix, is not seen.
   - a selected test in a file with a module-, class-, package- or
     session-scoped fixture brings its whole file.

   Both interpreters must pass. A selection that is the whole suite may be
   run as shards, `pytest -v --changed --shard=I/N` for I in 1..N, so each
   run is short enough to watch; record every shard's line. Each test runs in its own
   directory (#707), so the two runs may overlap in one checkout -- unless
   both include `tests/test_packaging_contract.py`, which builds the
   distributions in the repository root (setuptools' `build/` and
   `isocenter.egg-info/`); run those one after the other. That is nearly
   every run: the packaging test reads every `*.py`, so any change to a
   `.py` file anywhere in the repository selects it (#744). Plan on running
   the two interpreters one after the other unless the change touches no
   Python file. Paste each
   run's command, its SHA, its last line and its exit status
   (`…; echo "exit=$?"`) into the PR body, and keep the body current: it
   describes the SHA to be merged, not the first one pushed. A run that
   wrote into the repository root exits 1 and ends with the guard's line
   naming what it wrote. The guard sees new entries and rewritten files at
   the root's top level only: not a write below it (into `tests/`, say),
   and not a deletion. A change that selects no tests at all is recorded the same way,
   as "nothing selected", with the rule that produced it (`pytest
   --changed` exits 5 then, which is that result and not a failure).

   **The full suite is not a merge requirement.** It runs before a merge
   only when the rules above select it.

   **A change that alters exported output updates the output fingerprint
   in the same PR.** `fingerprint/output.json` records what the golden
   cohort exports (`scripts/output_fingerprint.py` says what it covers
   and what it deliberately leaves out). If the change is meant to alter
   what `export()` writes, or might, then after the tests above:
   - **Check**, on 3.12:
     `python -m scripts.output_fingerprint check --jobs 4 --report fp-3.12.txt; echo "exit=$?"`.
     Exit 0 is no difference, 1 is a difference, 2 means nothing was
     measured and is never a pass. Every difference it reports is either
     a defect in the change, fixed before going on, or intended.
   - **Retake**, for an intended difference, on 3.12:
     `python -m scripts.output_fingerprint take --out fingerprint/output.json --jobs 4`.
   - **Confirm** on 3.14t:
     `python -m scripts.output_fingerprint check --jobs 4; echo "exit=$?"`
     must report **no difference** (exit 0). The two interpreters are two
     observations of one recording, never two recordings.
   - **Name it.** Commit the file, and name every group the check's
     report lists in the change's `CHANGELOG.md` entry, in a line
     beginning `**Output:**` that says what changed and why. A difference
     caused only by a dependency upgrade is named the same way, with the
     dependency and its version.
   - **Paste** the check's grouped sections, and both runs' command, SHA,
     last line and exit status, into the PR body.

   If the change affects output that no cohort member reaches, add a
   member (`scripts/golden_cohort.py`) in the same PR, so the change is
   visible from then on. A member is never removed, and its committed
   bytes never rebuilt, to make a difference go away. `take` and `check`
   need pydicom's external test data, fetched once per machine:
   `python -c "import pydicom.data; pydicom.data.fetch_data_files()"`.
   Each takes about ten minutes at `--jobs 4` on a 14-core machine. To
   run one in parts, `--members 'pydicom:*'`, `--members 'pydicom-data:*'`
   and `--members 'synthetic:*'` together cover every member: three
   `check`s, or three `take`s joined by
   `python -m scripts.output_fingerprint merge --out fingerprint/output.json PART...`,
   which refuses parts that are not exactly the whole cohort.

   `fingerprint/output.json` is generated. **Never resolve a conflict in
   it by hand:** rebase, take it again, and check again. The reviewer
   checks that the file at the SHA under review is what `take` produces
   at that SHA, and that every group in the report is named by an
   `**Output:**` line.
4. Open a pull request into the target branch.
5. An adversarial reviewer reviews the tests and the code **as rebased on
   the current tip of the target branch**. A conflict with work merged since the branch was cut,
   textual or semantic, is part of this review: two changes that each pass
   alone and break together are the reviewer's finding. Changes go back to
   the developer, who consults the architect where the design is in
   question. The reviewer checks that the PR body carries step 3's two runs
   at the SHA under review. The first review covers the whole PR. A
   re-review covers the delta, and the whole PR when the delta touches what
   an earlier pass relied on. **Every pass names the SHA it approved.** If
   the target branch moves before the merge, the developer rebases and
   repeats step 3, and the reviewer re-reviews the delta.
6. When the reviewer passes, merge into the target branch pinned to that SHA
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
`publish.yml` calls it at release. A dispatched run is not part of this
procedure and no step waits on one. The gate is the local tests on both
interpreters and the review.

## Cutting a release

A release branch is a fully tested cut from `main`, then frozen: it takes
fixes, never features.

1. **Choose the commit** on `main`. It must hold no work for a later minor
   (see "Changes land on `main`"): choose the last commit before any. X.Y's
   commits merged after it then reach the new branch by picks, before the
   release commit, as "Later releases on an existing line" describes (its
   first paragraph says what changes for a first cut). Run the full suite on
   3.12 and 3.14t at that SHA. **This is the integration test**, and the
   first time this code meets the whole suite. A failure is fixed on `main`
   by the procedure above, and step 1 starts again at the new commit. (A
   patch release skips this step; its integration test is step 3's run.)

   **On 3.14t the full run is the map build** (#707):
   `PYTHON_GIL=0 python -m scripts.test_map build; echo "exit=$?"` in a
   clean checkout at that SHA (it needs the `dev` extra for `coverage`).
   It runs the whole suite under per-test coverage and exits with the
   suite's status. It also leaves `.test-map.json`, which every
   `pytest --changed` after it reads. Measured on 2026-09-21: 1899 s,
   against about 1450 s for the same suite without coverage, so 1.3x.
   Record its last test line and `exit=` as you would a plain run. The map
   is gitignored and never edited. Rebuild it on demand, by the same
   command, when `--changed` keeps falling back to `TARGETS` rows. An old
   map selects more, toward those rows. What it misses is a call path
   added *across* modules since the build, which is the rows' own bound
   and this step's to find.

   **While #796 is open**, the map build can hang in the dead-worker
   tests (`test_a_dead_ingest_worker_costs_the_file_it_was_reading.py`).
   When a pool breaks, the parent SIGTERMs the other workers, and
   coverage's own SIGTERM handler can deadlock one of them, which
   `.coveragerc` already concedes. It hung twice at the v1.0.0rc1 cut, and
   the owner ruled that step 1's 3.14t run may then be plain `pytest`,
   without coverage, split into shards: `PYTHON_GIL=0 python -m pytest -v
   --shard=I/N; echo "exit=$?"` for each I from 1 to N, recording every
   shard's last test line and `exit=`. Rebuild the map afterwards,
   outside the release path. A stall shows as the conftest watchdog's
   `ISOCENTER STALL WATCHDOG: nothing has happened for Ns (#250)` banner,
   repeated every 120 s. It never ends the run, so kill only that run's
   processes. faulthandler's `Timeout (0:05:00)!` dump is printed once
   per arming, so its absence after the first does not mean progress.

   Also run `python -m scripts.output_fingerprint check --jobs 4 --report
   fp-X.Y.Z-<interpreter>.txt; echo "exit=$?"` on **3.12 and 3.14t** at
   that SHA. Both must report **no difference**. A difference is a change
   that reached `main` without updating the fingerprint: it is fixed on
   `main` by the procedure above -- the output is put back, or the
   fingerprint update and its `**Output:**` line are added in a PR -- and
   step 1 starts again, in full, at the new commit. Exit 2 measured
   nothing; it is not a pass. `check` needs pydicom's external test data:
   `python -c "import pydicom.data; pydicom.data.fetch_data_files()"`
   once per machine.

   Then compare the tracked fingerprint with the previous release's.
   First `git fetch --tags origin`: `previous-tag` refuses when origin has
   a newer `v*` tag than the clone. Then
   `python -m scripts.output_fingerprint compare --base vP.Q.R --report fp-since-vP.Q.R.txt`,
   where vP.Q.R is what `python -m scripts.output_fingerprint previous-tag`
   prints: the highest `v*` tag in the repository by version order, **a
   pre-release tag included** (`v1.0.0` compares with `v1.0.0rc1`, so an
   output fix made after the candidate is named). By version order, not
   by what the commit reaches: release tags sit on `release/X.Y`, and
   `main` reaches none of them. **Every group it reports must be named by
   an `**Output:**` line under `[Unreleased]`.** A group no line names
   stops the release until one does (a `CHANGELOG.md` PR into `main`;
   step 1 starts again). The Toolchain section needs no entry, and
   neither does the Cohort section: a changed input, configuration or
   recorder is a changed measuring stick, not changed output. Output
   groups that coincide with a changed measuring stick (the report's
   header says so) still need a line, which may say they are the
   stick's. This comparison does not apply only when vP.Q.R is below
   `v1.0.0rc1`, the first release to carry `fingerprint/output.json`
   (`compare --base` exits 2 saying "does not apply"). Any other exit 2
   -- a tag this clone does not have, or a release from `v1.0.0rc1` on
   without the file -- stops the release until it is resolved.
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

     **The section is the release's notes** (step 7 copies it to the
     GitHub Release), and it says what changed since the previous release.
     Its entries do that for a candidate, a patch, or a final that had no
     candidates. A final that followed candidates, whose entries sit in
     the candidates' sections, opens with a summary of the changes since
     the previous final release, candidates included. A major release
     (`X.0.0`) opens instead with highlights of all its changes and
     capabilities, written for a reader upgrading from the previous major.
     A final's section is never empty (#826).

   Run the full suite on both interpreters at this commit
   (`tests/test_version_contract.py` checks that the three files agree).
   For a patch release this run is the integration test, **and the two
   fingerprint comparisons of step 1 are made at this commit**:
   `python -m scripts.output_fingerprint check` on both interpreters, then
   `compare --base` the line's previous tag, which
   `python -m scripts.output_fingerprint previous-tag --line X.Y` prints.
   The fingerprint is not rewritten at release:
   `git show vX.Y.Z:fingerprint/output.json` is the release's fingerprint.
   A failure here, before the release-commit PR merges, is fixed on
   `release/X.Y` by the patch procedure below -- and forward-ported --
   and the release commit is made again on top of the fix.
   Open it as a PR into `release/X.Y`, have it reviewed, and merge it the
   same way as any other PR.
   `release/*` is branch-protected with **Lock branch** on, so GitHub
   refuses a normal merge into it. Merge an approved PR with
   `gh pr merge N --squash --admin --match-head-commit <sha>` (an admin
   step; `enforce_admins` is off). The lock stays on for everything else.
   A failure found after the release-commit PR has merged, such as a red
   step 4 rehearsal, is fixed by a PR into `release/X.Y` developed and
   forward-ported as in "Patch releases", with one difference: the
   version is not spent yet, so its changelog entry goes into the
   existing `[X.Y.Z]` section, not a new `[Unreleased]`. The version files
   are already right, so there is no second release commit, and step 5
   tags the last such fix's merge commit (v1.0.0rc1, #798). Rehearse
   again at that commit.
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
5. **Tag** the release commit, or the last fix merged after it (step 3)
   (an admin step): `git tag -a vX.Y.Z -m "Isocenter X.Y.Z"` on
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
   targeting `release/X.Y`: step 3's rebase and its tests are against
   `release/X.Y`, not `main` (`pytest -v --changed
   --changed-base=release/X.Y`). Keep the fix and its tests in their own commits,
   and put the changelog entry in a separate commit, under
   `## [Unreleased]` at the top of the branch's `CHANGELOG.md`. The first
   fix of a patch adds that heading back.
3. **Forward-port the fix to `main` by cherry-pick**, in an ordinary PR
   into `main`: `git cherry-pick -x <fix commits>` onto a work branch off
   `main`. Leave out the changelog commit. A release-branch PR merges by
   squash (step 3), so on `release/X.Y` the fix and its changelog entry
   are one commit: cherry-pick that commit with `-x`, so the line names a
   commit the release branch keeps, then take `main`'s `CHANGELOG.md`
   back. A clean pick merges the entry in silently: run `git checkout
   HEAD~ -- CHANGELOG.md` and `git commit --amend --no-edit`. A pick
   that conflicts there: run `git checkout --ours CHANGELOG.md`, then
   `git add CHANGELOG.md` (without it the conflict stays unresolved),
   then `git cherry-pick --continue`. Resolve any other conflict as the
   code on `main` requires, and have the PR reviewed like any other. If the
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

## Later releases on an existing line

This applies when `release/X.Y` already exists and the line's next
release must carry X.Y's work merged to `main` since the cut: **the next
candidate (`X.Y.Zrc<N+1>`), X.Y.Z final, or the line's first release when
the cut (step 1) had to be made before some of X.Y's commits.** For a
first release the previous-candidate prerequisite in step 1 below does not
apply, and step 3's integration run at the release commit does. A patch to
a released line is "Patch releases" instead.

`main` is the development branch and can hold work for a later minor, so
**the release carries the commits on `main` that belong to X.Y,**
features included, and no others. That is the exception to "it takes
fixes, never features": the line is not released yet. The work reaches
the branch by cherry-pick, never by moving the branch: `release/X.Y` is
locked, and it holds the commit a published tag points to. v1.0.0rc2 was
cut this way (#818, #821, #822).

1. **Choose the commit** on `main`. The previous candidate's step 8 (its
   record back to `main`) must already be merged.
2. **Pick X.Y's work onto the branch** in a PR into `release/X.Y`.
   - **The range is every squash commit on `main` after the commit the
     line was cut from**, or after the last commit already picked onto
     the line. It runs up to the chosen commit, in order. Leave out three
     kinds of commit:
     - the record-back commits (step 8), which carry the version files;
     - forward-ports of release-branch fixes, which the branch already
       has. The `-x` line of one names a release-branch commit, or the
       fix PR's commit, which may exist only on that PR's deleted work
       branch. So also compare patches: a `main` commit whose `patch-id`
       (the command below) equals one already on the branch is a
       forward-port. A pick that comes out empty is one too:
       `git cherry-pick --skip` it;
     - work for a later minor, which its PR's milestone names.

     **The line's left-out list** is every commit left out as later-minor
     work since the line was cut, in this PR and in every earlier pick PR
     on the line. Each pick PR's body carries the whole list forward, with
     each commit's milestone, and the rules below read the whole list, not
     only this PR's additions. **The line's branch-only fixes** are carried
     forward the same way: every fix step 3 made on the branch that `main`
     does not have in the same form (one `main` does not need, or one
     forward-ported adapted), each with its PR.
   - On a work branch off `release/X.Y`, run `git cherry-pick -x` on each
     commit that remains:
     - A conflict in `CHANGELOG.md` (the branch has no `[Unreleased]`
       section, or at a first release only the cut's), or in a file a
       record-back changed (`RELEASING.md` at rc2), is resolved with `git checkout --ours <file>` (ours is the
       branch), `git add <file>`, then `git cherry-pick --continue`. The
       last commit deals with both.
     - **Never resolve `fingerprint/output.json` by hand.** Keep the
       branch's copy (`--ours`) and retake it, as below.
     - Any other conflict, which a left-out commit upstream of a pick can
       cause, is resolved as the code on the branch requires, and the PR
       body says so.
   - In a last commit:
     - give the branch an `[Unreleased]` section holding exactly the
       picked commits' changelog entries, in the wording `main` has at
       the chosen commit (a later commit may have amended an entry). At a
       first release it also keeps the entries the cut brought, which are
       X.Y's work up to the cut;
     - take back from `main` the files the record-back commits changed,
       apart from the version files (`RELEASING.md` at rc2);
     - **if the line's left-out list is not empty and any pick touched
       `fingerprint/output.json`,** or any commit on the list or any
       branch-only fix did, retake it on 3.12
       (`python -m scripts.output_fingerprint take --out fingerprint/output.json --jobs 4`),
       then `check` on 3.14t, which must report no difference. Add an
       `**Output:**` line to `[Unreleased]` only for a difference the
       picked entries do not already name.
   - **When the line's left-out list is empty and it has no branch-only
     fixes,** the PR head's tree must equal the chosen commit's (`git diff
     --stat <chosen sha> HEAD` prints nothing), and `[Unreleased]` is `main`'s whole. Any file that still
     differs means a pick is missing. Pick it; never copy it into the last
     commit. **Otherwise** `git diff HEAD <chosen sha> -- .
     ':!CHANGELOG.md' ':!fingerprint/output.json'` is the combined change
     of every commit on the list, plus the difference each branch-only fix
     leaves between the branch and `main` (the whole fix reversed for one
     `main` does not need; `main`'s adapted form against the branch's for
     one forward-ported adapted), apart from any hand-resolved conflict.
   - **Tests.** A PR whose picks all applied cleanly, or whose conflicts
     touched only the files above, runs no tests of its own: the picks
     were each tested on `main`, and step 3's integration run covers the
     branch's tree. A PR in which any other conflict was resolved by hand
     has put code on the branch that no tree has tested, so it runs the
     patch procedure's tests first: `pytest -v --changed
     --changed-base=release/X.Y` on 3.12 and 3.14t, with the SHA in each
     log, recorded in the PR body.
   - The reviewer checks, on the PR branch before the squash merge
     removes the picks:
     - the range: every commit in it is picked, or on the line's
       left-out list with its milestone, and the list carries forward
       every earlier PR's entries;
     - that each pick's patch equals its source's apart from the files
       resolved with `--ours` (`git show <sha> -- . ':!CHANGELOG.md'
       ':!fingerprint/output.json' ':!RELEASING.md' | git patch-id
       --stable` gives the same id for the pick and its `main` commit;
       exclude any other record-back file the same way); a pick with a
       hand-resolved conflict is read instead;
     - tree identity, or the left-out difference, as above;
     - that `[Unreleased]` holds exactly the picked commits' entries,
       plus, at a first release, the cut's own;
     - that the branch-only fixes carry forward every earlier PR's
       entries;
     - that nothing else rides along.

     Merge it with `gh pr merge N --squash --admin --match-head-commit
     <sha>`.
3. **Cut the release** by following "Cutting a release" from step 3,
   with version `X.Y.Zrc<N+1>` for a candidate or `X.Y.Z` for final.
   These things differ.
   - **Step 1's integration run is made at the release commit, not at
     `main`:**
     - the full suite on 3.12 and 3.14t;
     - `output_fingerprint check` on both interpreters;
     - `compare --base` the previous tag on the line, which
       `previous-tag` prints because pre-release tags count (so final
       compares with the last candidate).

     The `**Output:**` lines that `compare` requires are under the
     renamed section, not under `[Unreleased]`.
   - **A failure before the release-commit PR merges is fixed on `main`
     when `main` still needs the fix,** and picked onto `release/X.Y` in
     its own PR, as in step 2. This overrides step 3's "fixed on
     `release/X.Y` by the patch procedure". When `main` does not need it
     (the code has moved on for a later minor), the fix is made on the
     branch by the patch procedure, and its PR says why `main` does not
     need it. Where `main` needs it in another form, it is forward-ported,
     adapted. Both kinds go on the line's branch-only fixes. Either way,
     make the release commit again on top, and repeat the integration run
     in full.
   - **A failure after the release-commit PR has merged** (a red step 4
     rehearsal, say) is handled as step 3 already says: fixed on the
     branch and forward-ported to `main`, with its entry in the existing
     section. The forward-port is then one of the commits the next
     release's range leaves out. A fix `main` does not need at all
     ("Patch releases" step 3), or one forward-ported adapted, goes on the
     line's branch-only fixes.
   - **Final's section opens with the summary that step 3's
     `CHANGELOG.md` item requires,** above any picked entries. When step
     2 had nothing to pick, the branch has no `[Unreleased]` to rename, so
     the release commit adds the `## [X.Y.Z] - YYYY-MM-DD` heading with
     the summary under it.
   - Step 8 copies the new section to `main` as usual, and removes only
     that section's entries from `main`'s `[Unreleased]`; entries for
     left-out work stay there.

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
