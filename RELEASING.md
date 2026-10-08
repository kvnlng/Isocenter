# Releasing Isocenter

`main` is the development branch. Releases are cut from it onto a release
branch, frozen there, tagged, and published by hand. Nothing publishes
itself: no push, tag or GitHub Release uploads to PyPI or deploys the
documentation site on its own, except that pushing a release tag deploys
that release's documentation into its line's folder (see "Documentation
site").

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

The documentation has two ways in that are **not** admin-gated: anyone with
write access can dispatch `docs.yml` from `release/X.Y`, which rewrites the
live `X.Y/` folder (and `latest/`, when X.Y is the line `latest` copies), or
from `main`, which rewrites the hidden `dev/` folder. The content is still
reviewed content, since `release/*` is locked and `main` is reviewed; what
is open is who decides when it goes live. This was accepted (#866, Q8).

There is one branch per minor line, not per version. `release/0.9` holds
`v0.9.8` and every `0.9.z` patch after it. A branch name and a tag name
never collide, so `git checkout v0.9.8` always means the published commit.

## Changes land on `main`

`main` is the development branch. Work meant for a minor later than the next
unreleased line may merge at any time, but its PR carries that minor's
milestone, so whoever cuts the line can leave it out ("Later releases on an
existing line"). A PR with no milestone takes the milestones of the issues
it closes (GitHub's closing links: `Fixes`, `Closes`, `Resolves`; a `Refs`
link does not count). If any of them names a later minor, the PR is that
minor's work. A PR with neither is the next unreleased line's work.

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
   - a changed `isocenter/**/*.py` also selects the test files that read
     that one module's source: a `tests/test_*.py` whose text holds the
     module's file name (`wfdb.py`), or that holds `getsource`,
     `getsourcelines`, `ast.parse(` or `.__file__` and the module's name
     as a whole word (#779). This applies to a function the map covers
     too, which otherwise selects only the tests that ran it. The word
     and the spelling may sit anywhere in the file, so this over-selects;
     a reader that reaches the source through a helper in another file,
     or through `importlib`, `pkgutil` or `inspect.getfile`, is not seen.
   - a selected test in a file with a module-, class-, package- or
     session-scoped fixture brings its whole file.

   Both interpreters must pass. A selection that is the whole suite may be
   run as shards, `pytest -v --changed --shard=I/N` for I in 1..N, so each
   run is short enough to watch; record every shard's line.

   Runs at different SHAs each get their own worktree: `git worktree add
   --detach <dir> <sha>`, then `cp .test-map.json <dir>/` if the checkout
   has a map (it is gitignored, and without it `--changed` falls back to
   `TARGETS` rows). **Any run in a worktree**, made for this or because the branch
   lives in one, is run from `<dir>` as `PYTHONPATH=<dir> python -m
   pytest …`, never the `pytest` script, after `PYTHONPATH=<dir> python
   -c 'import isocenter; print(isocenter.__file__)'`, which must print a
   path under `<dir>`. The package is installed editable from the main
   checkout, and the script imports it from there, so the run would test
   the main checkout's code under the worktree's SHA. Paste each
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

   **Whenever `main` is taken into the branch, by rebase or by merge, check
   where the branch's `CHANGELOG.md` entries landed** (#956). Git places a
   hunk by its context and raises no conflict when the context has moved:
   once a record-back ("Cutting a release", step 8) has put a released
   heading above the entries a hunk was anchored on, the branch's entries
   follow them under that heading. **`git fetch origin` comes first:**
   both checks below read `origin/main`, and against a stale one the
   diff is taken with the wrong file and the test skips the misplaced
   section instead of failing it. Then:
   - `git diff --unified=0 origin/main -- CHANGELOG.md` shows the branch's
     own entries and nothing else, and every hunk starts above `main`'s
     first released heading: the `a` of each `@@ -a,b +c,d @@` is less
     than the line number that
     `git show origin/main:CHANGELOG.md | grep -n -m1 '^## \[[0-9]'`
     prints. A hunk at or below that line is an entry under a released
     heading, unless it rewrites a line of a released section in place,
     on purpose (the test allows that, #927). Move a misplaced entry up
     into `[Unreleased]` by hand, in the merge commit or the next one.
   - `tests/test_released_changelog_sections_stay_as_released.py` passes.
     `pytest --changed` selects it for any change to `CHANGELOG.md`, so
     the runs above include it. It compares each released section with
     `origin/main`'s as well as with its tag's, because a clone seldom
     has the newest tag: release tags sit on `release/X.Y`, and `git
     fetch origin main` does not bring them. On a branch that adds no
     released section, a skip naming `origin/main` means the ref is
     missing or stale: fetch, and run it again. `pytest -v` prints only
     `SKIPPED`; `-rs` prints which section and why.

   The merge of the 1.0.0rc13 record-back into #958's branch did this on
   2026-10-07: three entries, 37 lines, at the end of `[1.0.0rc13]`, with
   no conflict. The developer moved them by hand. The rc10 and rc11
   record-backs did it before (#927).

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
     observations of one recording, never two recordings. A row that
     differs between them because it quotes an exception this library
     did not raise (CPython rewords its own messages between versions) is
     given a sentence of this library's own at the raise, as #747's was;
     the fingerprint is never retaken on 3.14t to make the two agree
     (#942).
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
   3.12 and 3.14t at that SHA; shards that run at once, on either
   interpreter, each get their own worktree ("Changes land on `main`",
   step 3). **This is the integration test**, and the
   first time this code meets the whole suite. A failure is fixed on `main`
   by the procedure above, and step 1 starts again at the new commit. (A
   patch release skips this step; its integration test is step 3's run.)

   **One failure or one hang in this run, or in step 3's, is rerun
   before anything is fixed.**
   - **What counts:** a failed or errored test, a shard that crashes or
     exits non-zero with every test passed, or a hang. A **hang** is a
     shard whose stall banner, `ISOCENTER STALL WATCHDOG: nothing has
     happened for Ns (#250)`, names the same test with N at 600 or more:
     10 minutes with no new test starting (owner ruling on #856,
     2026-09-29). The banner is printed once 120 s pass without a new
     test starting, and every 120 s after that, so a banner alone is not
     a hang: in a loaded run a healthy test stalled the watchdog for more
     than 5 minutes, drawing two banners, and passed.
   - **What to do** (owner ruling on #845, 2026-09-29): kill only a hung
     shard's processes; let a shard with a failure finish, so that a
     second failure in it is seen. Rerun that shard in full, once, at the
     same SHA. An unsharded run is one shard. If the rerun is green, go
     on, and record both runs -- the failure or hang, and the rerun -- in
     the release-commit PR, with the flake filed as an issue. v1.0.0rc4's
     hung shard (#843, #844) was the first, killed at about 8.5 minutes,
     before this rule set 10.
   - **The ruling covers one.** If the rerun is red, or a second failure
     or hang turns up in the run, on either interpreter, it is a failure
     (a plain sharded run that replaces a hung map build counts its own,
     below), handled as the procedure you are following says for that
     run: step 1's is fixed on `main` and step 1 starts again; step 3's as
     step 3 says; a later release's as "Later releases on an existing
     line" says.
   - **A hang of the 3.14t map build** is the fallback case below (#796),
     not this one (owner ruling on #856, 2026-09-29): the plain sharded
     run replaces the build and has its own allowance of one, apart from
     the rest of the run's, and the build's hang counts toward neither.

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

   **If the map build hangs**, step 1's 3.14t run is plain `pytest`,
   without coverage, split into shards (owner ruling on #796,
   2026-09-24): `PYTHON_GIL=0 python -m pytest -v --shard=I/N; echo
   "exit=$?"` for each I from 1 to N, recording every shard's last test
   line and `exit=`. Rebuild the map afterwards, outside the release
   path. The integration runs of v1.0.0rc1 to v1.0.0rc5 were this
   fallback: the build hung in the dead-worker tests
   (`test_a_dead_ingest_worker_costs_the_file_it_was_reading.py`) at the
   rc1 cut and again after the rc5 cut. When a pool worker died, CPython
   sent the other workers SIGTERM and waited for each with no timeout,
   and coverage's own SIGTERM handler could keep one of them running for
   good. Since #796 was fixed, a worker still running 10 s after its pool
   is found broken is sent SIGKILL, and since #886 `.coveragerc` no longer
   installs that handler; this fallback stays for whatever else hangs a
   build. A stall shows as the conftest watchdog's
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

   Then check the configuration behaviour pins (#782) in
   `tests/test_config_behaviour_is_versioned.py`, which the full run has
   just run. `BEHAVIOUR_BY_VERSION` holds the digest of what each
   `CONFIG_VERSION` does to fixed input, and `SHIPPED_BEHAVIOUR` a literal
   copy of each row a release has shipped. Read both: every key of
   `SHIPPED_BEHAVIOUR` must hold the same hex in `BEHAVIOUR_BY_VERSION`,
   and the row for the `CONFIG_VERSION` at this SHA must be in both. A
   row in `BEHAVIOUR_BY_VERSION` that a release is about to ship for the
   first time is copied into `SHIPPED_BEHAVIOUR` by hand, as a literal,
   in a PR into `main`, and step 1 starts again. What enforces a shipped
   row is `test_no_row_the_previous_final_release_shipped_has_moved`: it
   reads `BEHAVIOUR_BY_VERSION` from this file at the previous **final**
   release tag (`git show <tag>:tests/test_config_behaviour_is_versioned.py`,
   the newest `vX.Y.Z` with no `a`/`b`/`rc` suffix at or before this
   version, in `git tag --sort=-version:refname` order) and fails if any
   row it held is missing or different in either table here. Final tags
   only (owner ruling Q9): an rc freezes nothing. So once a final tag
   carries the file, a row never changes: a change that moves the digest
   is a new minor with a new row, and a red test here is a change that
   reached `main` without one. It is fixed on `main`, never by editing a
   shipped row. The test skips, naming why, with no git checkout, no
   tags, no final tag, or a final that does not carry the file (every
   final before v1.0.0); in the full run at the cut, a skip for no git
   checkout or no tags stops the release until the tags are fetched
   (`git fetch --tags origin`).

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

     **Both dates are UTC** (owner ruling on #856, 2026-09-29):
     `date-released` and the heading's date are the UTC date of
     step 6's PyPI upload, which is how PyPI records it. 00:00 UTC is
     20:00 EDT and 19:00 EST; `date -u` prints the UTC date. v1.0.0rc5
     was uploaded at 23:51 EDT on 2026-09-28, which was 2026-09-29 in
     UTC, and carries 2026-09-29.
     - **Write the UTC date the upload will happen on,** allowing for
       everything between this commit and the upload: this step's run
       and review, step 4's rehearsal, and step 6's own run, which
       uploads at its end. v1.0.0rc5's release-commit PR was opened at
       02:33 UTC and the upload came at 03:51; its rehearsal and its
       publish run took about 20 minutes each.
     - **At step 5, before tagging,** check that step 6's upload, about
       20 minutes after its dispatch, will fall on the written date when
       step 6 is dispatched at once. If it would come before that date,
       wait. If the date has passed, correct both dates first; the
       version is not spent.
     - **A correction** is a PR into `release/X.Y` that changes only the
       two dates. It takes no changelog entry and no forward-port, because
       step 8 carries the dates to `main`. It is gated, reviewed and
       merged like any other PR into the branch (below). Step 5 tags its
       merge commit without a second rehearsal (owner ruling on #856,
       2026-09-29): TestPyPI refuses the files a green rehearsal already
       uploaded, and step 6's run tests the tag before it uploads. A slip
       found before the release-commit PR merges is corrected the same
       way, after it merges, so that the SHA its integration run was made
       at stands.

     **The section is the release's notes** (step 7 copies it to the
     GitHub Release), and it says what changed since the previous release.
     Its entries do that for a candidate, a patch, or a final that had no
     candidates. A final that followed candidates, whose entries sit in
     the candidates' sections, opens with a summary of the changes since
     the highest final version below it, candidates included. A major
     release's final (`X.0.0`, whether or not candidates preceded it; not
     the candidates themselves) opens with highlights of all its changes
     and capabilities in place of that summary, written for a reader
     upgrading from the previous major line (0.9.x for 1.0.0). A final's
     section is never empty (#826).

   Run the full suite on both interpreters at this commit
   (`tests/test_version_contract.py` checks that the three files agree);
   shards that run at once each get their own worktree, as in step 1.
   For a patch release this run is the integration test, **and the two
   fingerprint comparisons of step 1 are made at this commit**:
   `python -m scripts.output_fingerprint check` on both interpreters, then
   `compare --base` the line's previous tag, which
   `python -m scripts.output_fingerprint previous-tag --line X.Y` prints.
   The fingerprint is not rewritten at release:
   `git show vX.Y.Z:fingerprint/output.json` is the release's fingerprint.
   One failure or one hang in this run first goes through step 1's rerun
   rule. A failure here, before the release-commit PR merges, is fixed on
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
   build gates and test matrix still run. A date correction (step 3,
   "Both dates are UTC") is not rehearsed again.
5. **Tag** the release commit, or the last fix merged after it (step 3)
   (an admin step). First make step 3's date check ("Both dates are
   UTC"): tag only if step 6, dispatched at once, will upload on the
   written UTC date. A date correction's merge commit is tagged without
   a second rehearsal. Then: `git tag -a vX.Y.Z -m "Isocenter X.Y.Z"` on
   `release/X.Y`, then `git push origin vX.Y.Z`. **Pushing the tag deploys
   the documentation** for vX.Y.Z into `X.Y/` (see "Documentation site"),
   before the version is on PyPI; nothing else runs. That window is why
   step 4 must pass first. A final that is the highest final release also
   moves `latest` to `X.Y/`. A candidate deploys only while X.Y has no
   final, so a patch candidate (`vX.Y.Z+1rc1`) deploys nothing.
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
     contains from `[Unreleased]`. A branch that takes this by merge can
     find its own new entries under the released heading with no
     conflict; `tests/test_released_changelog_sections_stay_as_released.py`
     fails a released section whose lines are not those of the newest
     tag that holds it, or of `origin/main`, other than a line rewritten
     in place ("Changes land on `main`", step 3, has the check to run
     after such a merge);
   - sets `main`'s `isocenter/_version.py` and `CITATION.cff` to `X.Y.Z`, if
     X.Y is the newest release line. Between releases, `main` declares the
     newest version released from it, and `[Unreleased]` above that
     section holds everything since. A patch to an older line leaves
     `main`'s version alone.

   **Then the shard timings, in a PR of their own, never in the
   record-back** (#935). `tests/shard_timings.json` is what the CI shards
   are cut from, and step 6's publish run measured every test file on a
   runner. Refresh it only when both of these hold; otherwise leave the
   file alone and say which did not hold in the record-back PR's body:
   - **X.Y is the newest release line.** The merge replaces the file, so
     a run of an older line would take the number away from every test
     file `main` has and that line lacks.
   - **The run uploaded the timings.** `gh run download <run id>
     --pattern 'shard-timings-*' --dir <dir>`, with `<dir>` a new folder
     outside the checkout. A release cut from a branch that does not yet
     hold `tests.yml`'s upload step (every 1.0 candidate up to rc14) has
     no such artifact, and there is nothing to refresh. (`merge` over a
     folder that is missing or holds no recording refuses in a sentence
     and writes nothing.)

   With both, on a work branch off `main`: `python -m
   scripts.shard_timings merge <dir> --out "$PWD/tests/shard_timings.json"`.
   It takes, for each test file, the median of the versions. It refuses,
   and writes nothing, when any version lacks a shard (one killed at its
   timeout uploads nothing): leave the file alone then too. Never refresh
   it from a local run, whose seconds are another unit. Commit the file
   alone and run `tests/test_shards_partition_the_suite.py`:
   - green: open the PR with that one commit;
   - `test_the_heaviest_shard_fits_the_step_with_room` red: the suite has
     outgrown its shards. The same PR adds shards to `tests.yml` (the
     matrix list, the run line and the summary line move together) or
     raises its `Run Tests` cap with the job cap, so that it is green
     before it merges, and it merges before the next release is cut.

If the publish run fails before the upload job starts, nothing is spent.
Fix the release branch, delete the tag locally and on `origin`, re-tag,
and dispatch again (deleting and re-pushing a `v*` tag needs the admin
bypass, see "Names"). The `X.Y/` folder deployed from the first tag stays
live until the new tag's push redeploys it. Once the upload has succeeded, the
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
   code on `main` requires, and have the PR reviewed like any other.
   **A fix that retook `fingerprint/output.json`** brings the release
   line's recording, not what `main` exports: take `main`'s copy back as
   for `CHANGELOG.md` (never resolve a conflict in it by hand), then on
   `main` check on 3.12, take and check on 3.14t, as "Changes land on
   `main`", step 3, says, with the `**Output:**` lines it needs. **A fix
   that bumped `CONFIG_VERSION`** takes `main`'s value back and bumps
   `main`'s own minor, with its `SCHEMA_BY_VERSION` row in
   `tests/test_config_schema_version.py` and the literals the bump moves.
   If the fix does not apply to `main` (the code is gone there), say so in
   the release-branch PR instead. The changelog entry reaches `main` with
   the released section in step 8, once the patch ships.
4. Release it by following "Cutting a release" from step 3, with version
   `X.Y.Z+1`. The branch already exists, so steps 1 and 2 do not apply.
   Step 3 renames the branch's `[Unreleased]` to `[X.Y.Z+1]`, and step 8
   copies that section to `main`. The patch's tag rewrites `X.Y/`, and moves
   `latest` only if X.Y is the newest released line; a patch candidate
   (`vX.Y.Z+1rcN`) deploys no documentation.

A fix that applies only to `main` (already gone from the release line) is an
ordinary change to `main`.

A security fix kept private until its release does not follow this
section: see "Security fixes under embargo".

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
same number. `test_this_checkout_is_not_a_release_merged_forward`, beside
it, asks git about that one (#931): of a tree that opens its changelog
with its own version, whether `vX.Y.Z` is in its history where the clone
has the tag, and whether `_version.py` was last set on its own
first-parent line. That catches a merge commit, and a squash of a tagged
release. It does not catch a squash of a release commit not yet tagged, a
merge whose resolution of `_version.py` matches neither side, or anything
in a clone with no tags and no history. It rests on a release branch
being a line: a release commit taken into `release/X.Y` by a merge commit
(step 3 prescribes a squash) turns it red on that branch.

## Later releases on an existing line

This applies when `release/X.Y` already exists and the line's next
release must carry X.Y's work merged to `main` since the cut: **the next
candidate (`X.Y.Zrc<N+1>`), X.Y.Z final, or the line's first release when
the cut (step 1) had to be made before some of X.Y's commits.** For a
first release the previous-candidate prerequisite in step 1 below does not
apply, and step 3's integration run at the release commit does. A patch to
a released line is "Patch releases" instead. A final that follows
candidates is cut by this section even when nothing new belongs to X.Y;
step 2 is then skipped.

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
   When the range holds nothing to pick, skip this step: open no PR and
   add no `[Unreleased]`.
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
     - work for a later minor, which its PR's milestone names, or, when
       the PR has none, the milestone of any issue it closes by a closing
       link (`Fixes`/`Closes`/`Resolves`, never `Refs`); one later-minor
       issue is enough. List them with
       `gh pr view N --json milestone,closingIssuesReferences`, not by eye
       (GitHub's list counts every closing keyword and an issue linked by
       hand, and leaves out a `Refs`), then read each issue's milestone with
       `gh issue view M --json milestone`. A PR with neither belongs to the next
       unreleased line (the rc7 picks review, #875).

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
     - **A pick whose `CHANGELOG.md` hunk applies with no conflict has
       still put its entry in the wrong place,** and is left as it is.
       The branch has no `[Unreleased]` heading, until a pick resolved
       as the next rule says brings one, so git places the hunk by its
       context, under a released one. Whatever a pick leaves in
       `CHANGELOG.md`, with a conflict or without, is provisional: the
       last commit writes the file again from `main`'s (below). Three of
       the four picks for 1.0.0rc14 did this (#967, 2026-10-07): before
       the last commit the file differed from `main`'s by 7 insertions
       and 56 deletions, and nothing had conflicted.
     - **A pick whose whole diff is `CHANGELOG.md`** and that conflicts
       would come out empty under `--ours`, and an empty pick reads as a
       forward-port (above). Resolve it to `main`'s file at that commit
       instead: `git checkout <its sha> -- CHANGELOG.md`, `git add
       CHANGELOG.md`, then `git cherry-pick --continue`. The pick stays
       on the branch with its `-x` line, and the last commit writes the
       file like any other pick's (#956, owner ruling 2026-10-07). The
       one such pick before the rule, #954's at rc13 (#955), was
       resolved this way; rc12's picks (#928) took `main`'s file too,
       in picks that also carried code. That puts `main`'s whole file
       at that commit on the branch, so **the pick is followed by its
       proof**, pasted into the PR body with the pick's SHA:
       `git diff --stat <its sha> HEAD -- . ':!isocenter/_version.py' ':!CITATION.cff'`,
       run right after the pick, prints nothing: the picked tree is
       `main`'s at that commit, apart from the version files. Where it
       cannot print nothing, it prints exactly the files of the line's
       left-out list and branch-only fixes, and any file an earlier
       pick kept the branch's copy of with `--ours`; the PR body names
       each, and `CHANGELOG.md` is never among them. The reviewer runs
       the same command at the pick's commit on the PR branch, before
       the squash merge removes it, and checks the output against the
       PR body.
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
   - **Final's section opens with the summary, or a major's highlights,
     that step 3's `CHANGELOG.md` item requires,** above any picked
     entries. When the branch has no `[Unreleased]` (nothing belonged to
     X.Y, so step 2 opened no PR, and no fix has added one), the release
     commit adds the `## [X.Y.Z] - YYYY-MM-DD` heading with the summary
     under it, together with any `**Output:**` line `compare` requires.
   - Step 8 copies the new section to `main` as usual, and removes only
     that section's entries from `main`'s `[Unreleased]`; entries for
     left-out work stay there.
   - **Documentation.** A candidate of a line with no final deploys into
     `X.Y/` when its tag is pushed; before 1.0.0 that folder is also
     `latest`. Final needs no docs step beyond its tag, which moves
     `latest` when it is the highest final release.

## Security fixes under embargo

`.github/SECURITY.md` promises that a confirmed security problem "is fixed
in a release, and a GitHub Security Advisory is published with that
release". Every other path in this file puts a change on a public branch,
in a public PR, before any release carries it, and so discloses the
problem before its fix can be installed. This section is the path that
does not (#838). It is a patch release ("Patch releases") with four
differences: the work is private until the moment of release, the
release's integration run is made before anything is public, the
TestPyPI rehearsal is replaced by local runs, and the fix and the release
commit reach `release/X.Y` as one commit.

The GitHub facts it rests on are GitHub's documentation as read on
2026-09-30, cited by page:
- *Managing privately reported vulnerabilities* (docs.github.com,
  code-security): a private report arrives as an advisory with status
  `Triage`; **Accept and open as draft** "doesn't make the report
  public"; comments on it are "visible only to the reporter and to any
  collaborators on the advisory"; **Close security advisory** rejects it.
- *Collaborating in a temporary private fork*: "integrations, including
  CI, cannot access temporary private forks"; "status checks do not run
  on pull requests in temporary private forks"; at **Merge pull
  request(s)** GitHub "won't enforce any of the protection rules" of the
  target branch, merges every open PR of the fork at once, and allows
  only one into `main`.
- *Publishing a repository security advisory*: "Publishing a security
  advisory deletes the temporary private fork"; add a fixed version
  before publishing, or Dependabot alerts users "without offering any
  safe version to update to"; with "Request CVE ID later" chosen, the
  **Publish advisory** button is replaced by **Request CVE**.
- *About repository security advisories*: GitHub is a CVE Numbering
  Authority; it "usually reviews the request within 72 hours", and
  "requesting a CVE identification number doesn't make your security
  advisory public". After publication anyone sees the advisory, and
  collaborators see its conversation history.

**The tag must be in this repository.** `publish.yml` cannot run from the
private fork: no workflow runs there (the first citation above), and
Trusted Publishing matches this repository by name ("What cannot be
undone"), so a run anywhere else could not upload. Its build job also
refuses a `pypi` run from anything but a `v*` tag. So the fix is public
from the moment its branch is pushed to `origin` until the upload ends.
That window is kept to the few minutes of landing and tagging
("Disclosure", steps 2 to 4) plus one `publish.yml` run of about 20
minutes: about 25 minutes in all.
GHSA-phg9-vcvc-j4r7 (0.9.7), fixed before this section existed, was
public for 41 minutes: squashed onto `main` from its fork at 01:28 UTC
(1c41e5e3, "Merge commit from fork"), uploaded at 02:09, advisory
published at 02:10.

### Intake and triage

1. **A report arrives** by private vulnerability reporting, as an
   advisory in `Triage`, or by email to the address SECURITY.md names.
   For an email, or a problem the maintainers find themselves, the owner
   creates a draft advisory (**Security → Advisories → New draft security
   advisory**). Reply to the reporter: SECURITY.md promises a reply.
2. **Decide whether it is a security problem**, by SECURITY.md's "What
   counts" and "What does not". If it is not, comment on the advisory to
   say why and which public issue to open (by shape, never by content),
   then close it.
3. **The owner decides embargo or in the open.** In the open, it is an
   ordinary change ("Patch releases", or "Changes land on `main`"), and
   an advisory is still published with the release that fixes it, as
   SECURITY.md promises. #840 was fixed in the open by the owner's
   decision, because the maintainers found it and no report was
   outstanding. Under embargo, accept the report as a draft, and go on.
4. **Fill the draft now:** package `isocenter` (pip), the affected range,
   the patched version this release will have, severity, weaknesses, and
   credit for the reporter unless they ask not to be named. **Request a
   CVE now,** for every embargoed advisory (owner ruling on #867,
   2026-09-30): leave the draft's CVE identifier at "Request CVE ID
   later", then press **Request CVE** at the bottom of the draft form.
   GitHub's review can take three days, and the request does not make
   the advisory public. Once it is requested, **Publish advisory** is
   the button Disclosure step 6 finds there.
5. **Start a temporary private fork** from the advisory, and add as
   collaborators whoever will develop or review.

**Nothing about the problem goes anywhere public until "Disclosure"
below:** no issue, no discussion, no PR, no branch or commit on `origin`,
and no mention in any other PR, commit message or changelog entry. The
advisory's GHSA ID stands where an issue number would. The work branch
lives in a local worktree, and its upstream is the fork: `git remote add
ghsa <fork URL>` (remotes belong to the repository, so `origin` stays
beside it, and the steps below fetch from `origin`), then `git push -u
ghsa <branch>`, and check that `git config branch.<branch>.remote`
prints `ghsa` before any bare `git push`. Never push the branch to
`origin` before Disclosure step 2.

### The private fix

1. **Choose the version and base.** The base is the tip of
   `release/X.Y` for the latest line SECURITY.md supports. The version is
   the line's next patch (`X.Y.Z+1`), or, while that line is in
   candidates, its next candidate (`X.Y.Zrc<N+1>`), carrying this fix
   and nothing picked from `main`.

   **If a release is already in progress on the same line, the
   security fix goes first. The in-progress release pauses, rebases onto
   the security patch, and restarts its gate** (owner ruling on #867,
   2026-09-30). In progress means any stage before its tag is pushed,
   from picks being prepared locally to a release commit merged and not
   yet tagged. A release whose tag is already pushed does not pause and
   is not rebased (the last case below). Which number the security
   release takes depends on how far the other release got:
   - **No release commit merged yet** (picks being prepared, or its pick
     or release-commit PR open): the security release takes the next
     number, and the paused release's number moves up by one. When it
     rebases, its `isocenter/_version.py`, `CITATION.cff` and
     `CHANGELOG.md` heading conflict, and move to that number.
   - **A release commit merged on `release/X.Y`, and no tag pushed:**
     the pending version is unreleased, and the security fix folds into
     it (owner ruling on #867, 2026-09-30). The version is that pending
     one (1.0.2, say), and it ships as the security release. The branch
     leaves `isocenter/_version.py` alone, puts its entry in the
     existing `[X.Y.Z]` section, as a fix after the release-commit PR
     does (step 3 of "Cutting a release"), and moves the section's and
     `CITATION.cff`'s dates when the upload's UTC date differs. Step 5's
     runs below are that release's integration run, made again at this
     branch's head. Any of its PRs still open (a rehearsal fix, say)
     pause, and rebase afterwards.
   - **A tag already pushed:** a pushed tag is never moved (owner
     ruling on #867, 2026-09-30). That release does not pause. It is
     published first, as it is, and the security release follows it,
     taking the next number, on top of that tag.

   **Nothing public says why a release paused:** no comment, label,
   title or description on its PRs names a security release. The reason
   is recorded on the advisory.
2. **Develop** on a work branch off that tip, tests first, as "Changes
   land on `main`" says. The branch holds four things:
   - the fix and its tests;
   - a `CHANGELOG.md` section `## [X.Y.Z+1] - YYYY-MM-DD` (or the
     pending release's section) holding a `### Security` entry, and any
     `**Output:**` line (below);
   - `isocenter/_version.py` and `CITATION.cff` at the new version
     (already there, dates aside, when the fix folds into a pending
     release);
   - `fingerprint/output.json` and the `CONFIG_VERSION` bump, when the
     fix needs them (below).

   **This is the release commit too,** unlike step 3 of "Cutting a
   release", which contains only the version files: it lands as one
   squash commit, so that the window holds one public PR, not two
   (owner ruling on #867, 2026-09-30). The
   dates are the UTC date the upload will happen on ("Both dates are
   UTC"). Choose a disclosure time and write its date; if the time
   moves past midnight UTC, correct both dates on the branch before
   disclosure.
3. **The `### Security` entry** is written to the depth of 0.9.7's
   GHSA-phg9-vcvc-j4r7 entry: its bold lead names what an export carried
   or let someone recover, ending with the GHSA ID (and the CVE ID, if one
   is assigned) in the parenthesis where an issue number goes; which
   releases did it, measured on the latest; who reported it or how it was
   found; **Now**; what it does not cover; what a previously-working call
   now does differently; and what to do about files earlier releases
   exported. The advisory's description says the same, and its affected
   range and patched version match the entry. Commit messages end with
   the GHSA ID where an issue number goes.
4. **If the fix changes exported output**, retake the fingerprint on this
   branch as "Changes land on `main`", step 3, says (check on 3.12, take,
   check on 3.14t), and name every group in an `**Output:**` line in the
   new section. **If it changes what a configuration file does to the
   same input** (findings, values a rule writes, tags a rule reaches,
   pixel zones or date jitter), bump `CONFIG_VERSION`'s minor as the
   comment above it in `isocenter/config_manager.py` says, with the
   literals and the fingerprint that bump moves.
5. **Rebase on the tip of `release/X.Y` and run, at the branch head,
   with the SHA in each log:**
   - `pytest -v --changed --changed-base=release/X.Y` on 3.12 and 3.14t;
   - **the full suite on 3.12 and 3.14t**, the patch release's
     integration test (step 3 of "Cutting a release"), with its rerun
     rule;
   - **the full suite on 3.13 and 3.14**, which replaces the rehearsal
     (below). A red here is answered as step 4 of "Cutting a release"
     answers a red `test-supported` job: fix it, or delete that version's
     classifier from `setup.py` on this branch;
   - `python -m build` in a scratch worktree at the head, with every
     `isocenter/resources/*.json` in the wheel (`unzip -l`);
   - `python -m scripts.output_fingerprint check` on 3.12 and 3.14t, and
     `compare --base` the tag that `previous-tag --line X.Y` prints, as
     step 3 of "Cutting a release" requires of a patch.

   The runs happen before anything is public, so a failure costs only
   time.
6. **Adversarial review**, locally, of the branch as rebased. The reviewer
   approves one SHA and checks:
   - the new tests fail at the base tip and pass at the head;
   - the fix, as for any change;
   - `git diff --stat <base> <head>` touches only the fix, its tests,
     `CHANGELOG.md`, `isocenter/_version.py`, `CITATION.cff`, the
     fingerprint and `CONFIG_VERSION` changes if any, and step 5's
     classifier deletion from `setup.py`, or its fix for 3.13 or 3.14, if
     any, named by its own `CHANGELOG.md` line: nothing else rides
     along;
   - the entry is to the depth above, and agrees with the advisory
     draft's text, affected range, patched version and credit;
   - the version files agree and the dates are the planned UTC date;
   - every `**Output:**` group, and the `CONFIG_VERSION` bump when the fix
     needs one;
   - the logs of step 5, all at the SHA under review.

   A change after approval is a re-review of the delta and step 5 again.
   Record the approval, with its SHA, as a comment on the advisory: the
   fork and anything in it are deleted when the advisory is published,
   while the advisory's conversation stays with its collaborators. Push
   the approved branch to the fork.

**No rehearsal** (owner ruling on #867, 2026-09-30). Step 4 of "Cutting
a release" runs `publish.yml` from `release/X.Y`, which would need the
fix there, public, for a second full run before the tag. The local full
suite on 3.13 and 3.14 replaces it, beside step 5's runs on 3.12 and
3.14t and the local build. `publish.yml` then runs the same gates from
the tag before it uploads. A failure there is not free, as it is under
"Cutting a release": the fix is public by then, with nothing to install.
Disclosure step 5 says what happens instead. If `test-supported` goes red
in that run after step 5's local runs were green, the upload has already
happened and the release ships (owner ruling on #867, 2026-09-30). The
next release on that line deletes that version's classifier from
`setup.py`, or fixes the break, and its `CHANGELOG.md` entry says which,
and why.

### Disclosure

An admin does these steps in one sitting, without pausing between them.

1. **Check the date.** The upload comes about 25 minutes after step 2's
   push: a few minutes to land and tag, then about 20 after step 5's
   dispatch. If it would not fall on the written UTC date, stop here,
   while nothing is public: wait, or correct the dates on the branch
   (a date-only delta, re-approved by the reviewer).
2. **Land it:** push the approved branch to `origin`, open its PR into
   `release/X.Y` with every log and the approved SHA in the body (the PR
   is the public record that outlives the fork), and merge it at once
   with `gh pr merge N --squash --admin --match-head-commit <sha>`
   (owner ruling on #867, 2026-09-30). Not the advisory's **Merge pull
   request(s)**, which 0.9.7 used: it pins no SHA, and GitHub documents
   what it does to `main` only, not to a release branch.
3. **Check the tree:** `git fetch origin`, then `git diff --stat <sha>
   origin/release/X.Y` prints nothing. If it prints anything, the branch
   moved under the merge: stop, and do not tag.
4. **Tag and push** the merge commit, as step 5 of "Cutting a release"
   says. This deploys the documentation, as it always does.
5. **Publish**: `gh workflow run publish.yml --ref vX.Y.Z -f
   target=pypi`, and watch it to the upload. **A red `test-floor` job is
   rerun once** (`gh run rerun <run id> --failed`; owner ruling on #867,
   2026-09-30). If it is still red, the fix goes forward in the same
   sitting. It takes "The private fix" steps 2 to 6 (the tests, the runs
   and the review), though nothing about it is private any more, then
   these steps again from step 1, so that the UTC date check runs
   again. The pushed tag is never moved (see "The
   private fix", step 1), so the forward fix takes the next number: its
   branch moves the version files and the `CHANGELOG.md` heading to it,
   and the advisory's patched version moves with them. The version that
   never uploaded is skipped. The advisory waits for an upload.
6. **Publish the advisory** once the upload has succeeded, and not
   before: an advisory without an installable fixed version alerts users
   with nothing to update to. This deletes the fork.
7. **Create the GitHub Release**, as step 7 of "Cutting a release" says.

### After disclosure

The problem is public now, and the rest is the ordinary procedure.

1. **Forward-port to `main`** as "Patch releases", step 3, says, by
   `git cherry-pick -x` of the merge commit, the same day. The commit
   also carries the version files, so take all three back:
   `git checkout HEAD~ -- CHANGELOG.md isocenter/_version.py CITATION.cff`
   before the amend, or `--ours` on each in a conflict. A fingerprint or
   `CONFIG_VERSION` change is carried as "Patch releases", step 3, says.
   On a line in candidates, the next release's pick range leaves it out as
   a forward-port, by its `-x` line.
2. **Bring the release record back** as step 8 of "Cutting a release"
   says.

## Documentation site

The site is versioned with [mike](https://github.com/jimporter/mike) (#866).
Everything lives on the `gh-pages` branch, which GitHub Pages serves under
the `github-pages` environment (its deployment-branch policy admits only
`gh-pages`):

| Folder | Holds | Written by |
|---|---|---|
| `X.Y/` (`1.0/`, `1.1/`, …) | The newest release of line X.Y: its highest final `vX.Y.Z`, or its highest pre-release while it has no final. Titled with that version | A tag push, or a dispatch from `release/X.Y` |
| `latest/` | A copy of the folder of the highest final release (a copy, not a symlink or redirect, so its URLs stay put at the next minor) | Moved only by the tag of the highest final release |
| `dev/` | `main`, hidden from the version selector | A dispatch from `main` |
| root `index.html` | A redirect to `latest/` | By hand, once (the cutover below) |
| root page stubs and `404.html` | Redirects from the pre-versioning URLs to the same page under `latest/` | By hand, once; frozen, and never touched by mike |

Lines below 1.0 are not published. The root redirects to `latest/`, so the
default site is always the latest release, never unreleased `main`; every
folder but `latest/` carries a banner linking to it.

`docs.yml` runs on a pushed `v*` tag and on manual dispatch. Its `guard`
job decides which folder the ref may write, and the `deploy` job runs only
after it passes:

| Ref | Writes | Moves `latest` |
|---|---|---|
| tag `vX.Y.Z` (final) that is X.Y's highest final | `X.Y/`, titled `X.Y.Z` | only if it is the highest final release of all |
| tag `vX.Y.Z{a,b,rc}N` that is X.Y's highest pre-release, while X.Y has no final | `X.Y/` | no |
| branch `release/X.Y`, by dispatch, containing X.Y's newest tag, with nothing under `isocenter/` changed since it | `X.Y/`, titled with that tag's version | no (a folder that already holds `latest` refreshes its copy) |
| branch `main`, by dispatch | `dev/`, hidden | no |
| anything else | nothing: the run fails, naming why | |

So an older tag on a line (`v1.0.0` after `v1.0.1`) deploys nothing, a patch
to an older line (`v1.0.4` after `v1.1.0`) rewrites `1.0/` and leaves
`latest` on 1.1, and a patch candidate never reaches a folder a final
holds. Version order is git's `version:refname`, so `v1.0.10` is above
`v1.0.9`.

- **A docs-only change on a released line** (the MIDI-B results page, say)
  goes live without a tag: merge it to `release/X.Y`, then run
  `gh workflow run docs.yml --ref release/X.Y`. If anything under
  `isocenter/` changed since the line's newest tag, the run refuses and names
  the files, because the API reference is rendered from those docstrings and
  would describe code no release installs. Ship the page with the next
  patch.
- **`dev`:** `gh workflow run docs.yml --ref main`. Nothing deploys `main`
  on push.
- **To redeploy line X.Y** (after a failed or cancelled run), dispatch from
  `release/X.Y` if it is docs-only since its newest tag, or from that tag if
  the tag was cut after #866.
- **Never "Re-run" a failed docs run; dispatch it again from its ref.** A
  re-run reuses the old run's guard decision, which a newer tag may have
  overtaken (a `latest` that belongs to a newer line, a patch that is no
  longer its line's newest). The deploy job decides again from the tags just
  before `mike deploy` and fails on any difference, so a re-run of a stale
  run fails rather than deploying, but only a fresh dispatch deploys the
  right thing. The same check stops a tag's deploy that queued behind a
  newer tag's; dispatch that one again too, if its folder still needs it.
- **Never dispatch `docs.yml` from a tag cut before #866** (`v1.0.0rc6` and
  every earlier tag). A dispatch runs the workflow file *at that ref*,
  which is the old `mkdocs gh-deploy`. While such a tag is the highest `v*`
  tag, its old guard admits it, and gh-deploy commits a tree holding only its
  root site, deleting every version folder, `versions.json` and the stubs.
  Once a later tag exists every old tag fails its own old guard, but do not
  rely on that. If it happens, `git revert` that commit on `gh-pages` and
  push; neither tool force-pushes, so the history is intact.
- **Concurrency.** Deploys queue in one group and never cancel one in
  progress: each writes its own folder, and a cancelled `1.0/` deploy would
  lose 1.0's update silently. GitHub keeps one run pending per group, so a
  third deploy in flight cancels the pending one; that shows as a cancelled
  run, and is re-dispatched from its ref (a deploy is idempotent per
  folder). A refused run is outside the group and cancels nothing.
- **Previews.** `mkdocs serve` previews one tree (it needs the `docs`
  extra). `mike serve` shows the version selector against your local
  `gh-pages` branch. Both write into the checkout's root (`site/`, and
  mike's temporary `mike-mkdocs*.yml`), so never run them inside a test or
  beside a concurrent pytest run.

### Rollback

Everything is ordinary history on `gh-pages`, and neither mike nor the
workflow rewrites it. mike pushes without force and refuses a diverged
local `gh-pages`; never pass `--ignore-remote-status` or `--force`. Run the
hand commands in a clean checkout of the line's branch with the `docs`
extra and `origin/gh-pages` fetched, so that `mkdocs.yml`'s
`alias_type: copy` applies (a tree without it makes a symlink alias).

- **Bad content in a folder:** re-dispatch that folder's ref, after
  reverting the bad change on `release/X.Y` if that is where it came from.
  By hand,
  `mike deploy --push --title X.Y.Z X.Y` works only from a checkout that
  carries #866's `mkdocs.yml`: a checkout of `release/X.Y` at the good
  commit, or a tag cut after #866. From an older tree, such as
  `v1.0.0rc6`, the build has no version selector and mike falls back to
  its default alias type, so `latest` becomes a git symlink (ruled out,
  Q7). **Until a tag cut after #866 exists, the rollback for `1.0/` is a
  re-dispatch of `release/1.0`**; the tag-based form is valid for 1.0 from
  the first tag cut after #866 (the next 1.0 candidate or `v1.0.0`) on.
- **Wrong `latest`:** `mike alias --push --update-aliases X.Y latest`.
- **Unwanted `dev`:** `mike delete --push dev`.
- **Abandon versioning** (before or after 1.0.0): revert the #866 change on
  `main` and on `release/1.0`; on `gh-pages`, restore the tree of
  `dd5c7409` (the last root site) as a new commit (`git checkout dd5c7409
  -- .`, `git rm` what that tree lacks, commit, push without force); the
  next tag then deploys a root site through the old workflow. Only the
  `latest/` links published since the cutover break.

### The cutover to the versioned site (one time, #866)

None of the tags up to `v1.0.0rc6` carries the versioned `docs.yml`, so the
first versioned build comes from `release/1.0` (owner ruling Q1: now, by
hand, not with a tag). Steps 3 to 6 are an admin's; steps 4 to 6 edit
`gh-pages` by hand, and each is pushed without force. Until step 4, the
old root site keeps serving and nothing is broken; the README's `latest/`
links are dead from the merge of #866 until step 4, so keep that short.

1. Merge the #866 change to `main` under the usual gate.
2. Pick it onto `release/1.0` ("Later releases on an existing line", step 2).
3. `gh workflow run docs.yml --ref release/1.0`. The guard takes
   `v1.0.0rc6` as 1.0's head; the branch must have no `isocenter/` change
   since it, or the run refuses and the cutover waits for the next tag. The
   run deploys `1.0/` titled `1.0.0rc6`. Check `/Isocenter/1.0/`.
4. In a clean checkout of `release/1.0` after the pick, with the `docs`
   extra installed and `git fetch origin gh-pages` done:
   `mike alias --push 1.0 latest`.
5. `mike set-default --push latest`: the root `index.html` becomes a
   redirect to `latest/`.
6. One hand-made commit on `gh-pages`, from a checkout of it: each of the 29
   page paths of `dd5c7409` becomes a redirect stub to the same path under
   `latest/` (depth-adjusted); the root `404.html` is replaced by a static
   page that sends any path whose first segment is not in `versions.json`
   to `latest/`; and the old root `assets/`, `search/`, `stylesheets/`,
   `images/`, `sitemap.xml`, `sitemap.xml.gz` and `objects.inv` are deleted
   (ruling Q5: a stale inventory is worse than none). The shell loop and
   the 404 page are in the pull request that closed #866, verbatim. The
   commit message names #866 and `dd5c7409`.
7. Optionally, `gh workflow run docs.yml --ref main` for `dev/`.
8. Check, and record in #866: `curl -sI` a few old URLs; in a browser,
   `/Isocenter/configuration/#privacy-profile`, `/Isocenter/`,
   `/Isocenter/latest/`, `/Isocenter/1.0/` and a bogus path; the selector
   shows `1.0.0rc6 latest` and not `dev`; no banner on `latest/` or `1.0/`.

After the cutover, the next candidate and then `v1.0.0` deploy into `1.0/`
from their tags, and `latest/` follows (mike refreshes every alias of the
folder it deploys).

## What cannot be undone

- A version uploaded to PyPI or TestPyPI can never be uploaded again, even
  after deleting it.
- A DOI Zenodo mints for a GitHub Release is permanent. The record's
  metadata can be corrected later; the DOI cannot be reissued.

Trusted Publishing matches on this repository, the workflow **filename**
(`publish.yml`) and the environment names (`pypi`, `testpypi`). Renaming
any of them breaks publishing until the publisher configuration on PyPI is
updated to match.
