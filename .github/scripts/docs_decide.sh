#!/usr/bin/env bash
# Which folder of the versioned documentation site may this ref write
# (#866)? Run by docs.yml twice: by the `guard` job, whose outputs the
# deploy job reads, and again by the deploy job just before `mike deploy`,
# which fails the run if the answer has changed since (a "Re-run failed
# jobs" of an old run, or two tags whose deploys queued out of order). One
# file, so the two cannot disagree about the rule.
#
# Reads GITHUB_REF, GITHUB_SHA and the repository's tags; appends
# `folder=`, `title=`, `alias=` and `hidden=` to $GITHUB_OUTPUT, and
# writes nothing there until the decision is whole, so a refused run
# leaves no half-written outputs.
#
# "Line head" of X.Y is its highest *final* `vX.Y.Z`, or, while the line
# has no final, its highest pre-release. Order is git's `version:refname`
# (`v1.0.10` > `v1.0.9`), which creation order and text order are not. A
# pre-release is never ranked against a final -- finals are taken first --
# so `versionsort.suffix` would decide nothing here and is deliberately
# not set.
#
# The release-branch rule compares against $GITHUB_SHA, the tip that was
# dispatched, never HEAD. `isocenter/` is the discriminator because
# mkdocstrings renders the API reference from it: a branch whose
# `isocenter/` equals its line head's describes exactly what `pip install
# isocenter==<title>` installs (#866, Q6).
#
# tests/test_packaging_contract.py runs this script against real tags.
set -e

refuse() {
  echo "::error::$1"
  exit 1
}
all_tags=$(git tag --list 'v*' --sort=-version:refname)
first_matching() {
  printf '%s\n' "$all_tags" | awk -v re="$1" '$0 ~ re { print; exit }'
}
line_head() {
  local head
  head=$(first_matching "^v$1[.]$2[.][0-9]+\$")
  if [ -z "$head" ]; then
    head=$(first_matching "^v$1[.]$2[.][0-9]+(a|b|rc)[0-9]+\$")
  fi
  printf '%s' "$head"
}
alias=""
hidden=false
case "$GITHUB_REF" in
  refs/tags/v*)
    tag=${GITHUB_REF#refs/tags/}
    if [[ ! "$tag" =~ ^v([0-9]+)\.([0-9]+)\.[0-9]+((a|b|rc)[0-9]+)?$ ]]; then
      refuse "${tag} is not a release tag (vX.Y.Z, or vX.Y.Z with an a, b or rc suffix)"
    fi
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    if [ "$major" -lt 1 ]; then
      refuse "${tag}: lines below 1.0 are not published"
    fi
    head=$(line_head "$major" "$minor")
    if [ "$tag" != "$head" ]; then
      refuse "${tag} is not the head of ${major}.${minor} (${head}); deploying it would replace that line's documentation with an older or pre-release build"
    fi
    folder="${major}.${minor}"
    title=${tag#v}
    top_final=$(first_matching '^v[0-9]+[.][0-9]+[.][0-9]+$')
    if [ "$tag" = "$top_final" ]; then
      alias=latest
    fi
    ;;
  refs/heads/release/*)
    line=${GITHUB_REF#refs/heads/release/}
    if [[ ! "$line" =~ ^([0-9]+)\.([0-9]+)$ ]]; then
      refuse "release/${line} is not a release line (release/X.Y)"
    fi
    major=${BASH_REMATCH[1]}
    minor=${BASH_REMATCH[2]}
    if [ "$major" -lt 1 ]; then
      refuse "release/${line}: lines below 1.0 are not published"
    fi
    head=$(line_head "$major" "$minor")
    if [ -z "$head" ]; then
      refuse "release/${line} has no v${line}.* tag, so there is no release for its documentation to describe"
    fi
    if ! git merge-base --is-ancestor "refs/tags/$head" "$GITHUB_SHA"; then
      refuse "release/${line} (${GITHUB_SHA}) does not contain ${head}, the line's newest tag"
    fi
    changed=$(git diff --name-only "refs/tags/$head" "$GITHUB_SHA" -- isocenter/)
    if [ -n "$changed" ]; then
      echo "changed under isocenter/ since ${head}:"
      printf '%s\n' "$changed" | sed 's/^/  /'
      refuse "release/${line} changed isocenter/ since ${head} ($(printf '%s' "$changed" | tr '\n' ' ')); the API reference would describe code no release installs. Ship the page with the next patch."
    fi
    folder="${major}.${minor}"
    title=${head#v}
    ;;
  refs/heads/main)
    folder=dev
    title=dev
    hidden=true
    ;;
  *)
    refuse "${GITHUB_REF} may not deploy the documentation: only a release tag, release/X.Y or main may"
    ;;
esac
{
  echo "folder=${folder}"
  echo "title=${title}"
  echo "alias=${alias}"
  echo "hidden=${hidden}"
} >> "$GITHUB_OUTPUT"
echo "deploying ${GITHUB_REF} into ${folder}/ titled ${title}${alias:+, moving ${alias}}"
