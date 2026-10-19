#!/usr/bin/env bash
# Put one built documentation tree onto the versioned site on `gh-pages`
# (#866 made the layout; this script replaced mike at the move to Sphinx).
#
#   docs_deploy.sh                  deploy $SITE into $FOLDER
#   docs_deploy.sh delete <folder>  remove a folder that holds no alias
#
# Deploy reads FOLDER, TITLE, ALIAS and HIDDEN (the guard's outputs, from
# docs_decide.sh) and SITE (the built `dirhtml` tree). It replaces
# `<FOLDER>/` with SITE, records FOLDER in `versions.json` under TITLE,
# moves ALIAS (only ever `latest`) onto FOLDER, replaces the folder of
# every alias FOLDER then holds with SITE too, and writes `switcher.json`,
# pydata-sphinx-theme's version list, from `versions.json`.
#
# It writes nothing else. The root `index.html`, `404.html`, `.nojekyll`
# and the pre-versioning stubs were made by hand at the cutover and are
# never touched: `git add` names only the paths above, so a stray write
# anywhere else in the tree is not committed.
#
# Things a later edit must keep, each because it was mike's behaviour and
# something relies on it:
#
# - `versions.json` stays mike 2.2.0's file, byte for byte: its key order,
#   `properties` only when set, two-space indent, a final newline, and
#   mike's order (a version not starting with a digit, i.e. `dev`, first;
#   then newest first). The root `404.html` reads it to tell a version
#   from a pre-versioning URL. tests/test_packaging_contract.py compares
#   the output with mike's own serialisation.
# - A deploy with no alias still refreshes every alias its folder already
#   holds, so a docs-only dispatch of the line `latest` copies also
#   updates `latest/` (RELEASING.md, "Documentation site").
# - An alias is a copy, never a symlink: `latest/`'s URLs must survive the
#   next minor (#866, Q7).
#
# Never `--force`, never a `+` refspec. The push is a plain push of one
# new commit on the fetched tip, so a gh-pages that moved since the fetch
# rejects it and the run fails; nothing another deploy wrote is pushed
# over. FOLDER and every name read back from `versions.json` are checked
# against the names the guard can produce before anything is removed: an
# empty or dotted name here would turn `rm -rf "$tree/$name"` into the
# deletion of the whole site, which is what `mkdocs gh-deploy` from an old
# tag does (RELEASING.md).
set -euo pipefail

SITE_URL="https://kvnlng.github.io/Isocenter/"
PYTHON=${PYTHON:-python3}

refuse() {
  echo "::error::$1" >&2
  exit 1
}

mode=deploy
if [ "$#" -gt 0 ]; then
  if [ "$1" = delete ] && [ "$#" -eq 2 ]; then
    mode=delete
    FOLDER=$2
  else
    refuse "usage: docs_deploy.sh (deploy, from FOLDER, TITLE, ALIAS, HIDDEN and SITE), or docs_deploy.sh delete <folder>"
  fi
fi

FOLDER=${FOLDER:-}
if [[ ! "$FOLDER" =~ ^([0-9]+\.[0-9]+|dev)$ ]]; then
  refuse "folder '${FOLDER}' is not X.Y or dev"
fi
if [ "$mode" = deploy ]; then
  TITLE=${TITLE:-}
  ALIAS=${ALIAS:-}
  HIDDEN=${HIDDEN:-false}
  SITE=${SITE:-}
  [ -n "$TITLE" ] || refuse "TITLE is empty"
  case "$ALIAS" in "" | latest) ;; *) refuse "alias '${ALIAS}' is not latest" ;; esac
  case "$HIDDEN" in true | false) ;; *) refuse "HIDDEN is '${HIDDEN}', not true or false" ;; esac
  [ -f "$SITE/index.html" ] || refuse "SITE ('${SITE}') holds no index.html; build the site first"
  [ ! -e "$SITE/.doctrees" ] || refuse "SITE holds .doctrees/, Sphinx's pickled build state; build with -d outside SITE"
  SITE=$(cd "$SITE" && pwd)
fi

git fetch --quiet origin gh-pages
tip=$(git rev-parse FETCH_HEAD)
if git show-ref --verify --quiet refs/heads/gh-pages; then
  if ! git merge-base --is-ancestor refs/heads/gh-pages "$tip"; then
    refuse "the local gh-pages branch holds commits origin's does not; it has diverged, and deploying would push over neither. Inspect it, then delete or reset it"
  fi
fi

tree=$(mktemp -d)
cleanup() {
  git worktree remove --force "$tree" >/dev/null 2>&1 || true
  rm -rf "$tree"
}
trap cleanup EXIT
git worktree add --quiet --detach "$tree" "$tip"

if [ ! -f "$tree/.nojekyll" ]; then
  refuse "gh-pages has no .nojekyll at its root. Without it GitHub Pages runs Jekyll, which drops every path starting with an underscore, Sphinx's _static/ among them. Restore it by hand first"
fi

# The JSON half, standard library only. It validates everything it reads
# before it writes either file, and prints the folders the deploy must
# replace (FOLDER first, then its aliases), one per line.
folders=$(MODE="$mode" FOLDER="$FOLDER" TITLE="${TITLE:-}" ALIAS="${ALIAS:-}" \
  HIDDEN="${HIDDEN:-false}" SITE_URL="$SITE_URL" TREE="$tree" "$PYTHON" - <<'PY'
import json
import os
import re
import sys

mode, folder = os.environ["MODE"], os.environ["FOLDER"]
tree, site_url = os.environ["TREE"], os.environ["SITE_URL"]
versions_path = os.path.join(tree, "versions.json")
VERSION = re.compile(r"^([0-9]+\.[0-9]+|dev)$")


def refuse(message):
    print(f"::error::{message}", file=sys.stderr)
    sys.exit(1)


try:
    with open(versions_path, encoding="utf-8") as handle:
        entries = json.load(handle)
except FileNotFoundError:
    entries = []
for entry in entries:
    if not VERSION.match(str(entry.get("version"))):
        refuse(f"versions.json names version {entry.get('version')!r}, "
               "which is not X.Y or dev; fix gh-pages by hand first")
    for alias in entry.get("aliases", []):
        if alias != "latest":
            refuse(f"versions.json gives {entry['version']} the alias "
                   f"{alias!r}; only latest is written by this site")
by_version = {entry["version"]: entry for entry in entries}

if mode == "delete":
    entry = by_version.get(folder)
    if entry is None:
        refuse(f"versions.json has no {folder}; there is nothing to delete")
    if entry.get("aliases"):
        refuse(f"{folder} holds {', '.join(entry['aliases'])}; deleting it "
               "would take that address down too. Move the alias first, "
               "by deploying the folder it belongs on")
    del by_version[folder]
    replace = []
else:
    entry = by_version.setdefault(
        folder, {"version": folder, "title": folder, "aliases": []})
    entry["title"] = os.environ["TITLE"]
    if os.environ["HIDDEN"] == "true":
        entry["properties"] = {**(entry.get("properties") or {}),
                               "hidden": True}
    alias = os.environ["ALIAS"]
    if alias:
        for other in by_version.values():
            if alias in other.get("aliases", []):
                other["aliases"].remove(alias)
        entry["aliases"].append(alias)
    replace = [folder, *entry["aliases"]]


def loose(version):
    """mike's sort key: verspec's LooseVersion, after a digit-first test."""
    parts = [int(part) if part.isdigit() else part
             for part in re.split(r"(\d+|[a-z]+|\.)", version)
             if part and part != "."]
    return (0 if re.match(r"v?\d", version) else 1, parts)


ordered = sorted(by_version.values(), key=lambda e: loose(e["version"]),
                 reverse=True)


def mike_entry(entry):
    """One entry in mike 2.2.0's `VersionInfo.to_json` key order."""
    data = {"version": entry["version"], "title": entry["title"],
            "aliases": sorted(entry.get("aliases", []))}
    if entry.get("properties"):
        data["properties"] = entry["properties"]
    return data


with open(versions_path, "w", encoding="utf-8") as handle:
    handle.write(json.dumps([mike_entry(e) for e in ordered], indent=2) + "\n")

switcher = []
for entry in ordered:
    if (entry.get("properties") or {}).get("hidden"):
        continue
    latest = "latest" in entry.get("aliases", [])
    item = {"name": entry["title"] + (" (latest)" if latest else ""),
            "version": entry["version"],
            "url": site_url + ("latest/" if latest else entry["version"] + "/")}
    if latest:
        item["preferred"] = True
    switcher.append(item)
with open(os.path.join(tree, "switcher.json"), "w", encoding="utf-8") as handle:
    handle.write(json.dumps(switcher, indent=2) + "\n")

print("\n".join(replace))
PY
)

paths=(versions.json switcher.json "$FOLDER")
rm -rf "${tree:?}/${FOLDER:?}"
while IFS= read -r name; do
  [ -n "$name" ] || continue
  rm -rf "${tree:?}/${name:?}"
  mkdir -p "$tree/$name"
  cp -R "$SITE/." "$tree/$name/"
  if [ "$name" != "$FOLDER" ]; then
    paths+=("$name")
  fi
done <<< "$folders"

source_sha=$(git rev-parse --short HEAD)
if [ "$mode" = deploy ]; then
  builder=$("$PYTHON" -c 'import sphinx; print(sphinx.__version__)' 2>/dev/null || echo '(version not found)')
  message="Deployed ${source_sha} to ${FOLDER} with Sphinx ${builder}"
else
  message="Deleted ${FOLDER} (from ${source_sha})"
fi
git -C "$tree" add -A -- "${paths[@]}"
# The runner has no identity of its own; a hand run commits as its user.
identity=()
if [ -z "$(git config user.email || true)" ]; then
  identity=(-c user.name="github-actions[bot]"
            -c user.email="41898282+github-actions[bot]@users.noreply.github.com")
fi
# (The `+` form: bash before 4.4 calls an empty array unbound under -u.)
git -C "$tree" ${identity[@]+"${identity[@]}"} commit --quiet --allow-empty -m "$message"
git -C "$tree" push --quiet origin HEAD:refs/heads/gh-pages
echo "${message}; gh-pages is now $(git -C "$tree" rev-parse --short HEAD)"
