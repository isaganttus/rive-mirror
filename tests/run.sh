#!/usr/bin/env bash
# Runs rive-mirror against a fake `rive`: no network, no account.   Usage: tests/run.sh
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
M="$HERE/../rive-mirror"
T=$(cd "$(mktemp -d)" && pwd -P); trap 'rm -rf "$T"' EXIT
export PATH="$HERE/fake-rive:$PATH" XDG_CONFIG_HOME="$T/config" FAKE_DATA="$T/data" HOME="$T/home"
mkdir -p "$T/home" "$T/data" "$T/mirror" "$T/elsewhere"

pass=0; fail=0
has() {   # has NAME WANTED GOT: GOT contains WANTED
  case "$3" in *"$2"*) pass=$((pass+1)); echo "ok   $1" ;;
    *) fail=$((fail+1)); echo "FAIL $1"; echo "       wanted: $2"; echo "       got:    $3" ;; esac
}
eq() { if [ "$2" = "$3" ]; then pass=$((pass+1)); echo "ok   $1"; else fail=$((fail+1)); echo "FAIL $1 (wanted '$2', got '$3')"; fi; }
run() { "$M" "$@" </dev/null 2>&1; }

# One workspace, two projects: Brand has 4 files at the top, Icon Pack has one in a folder and one at the top.
D="$T/data"
echo '{"kind":"workspace","workspaceId":5,"name":"Studio","personal":true,"writable":true,"current":true}' > "$D/workspaces.jsonl"
printf '%s\n' '{"kind":"project","projectId":50,"name":"Brand"}' '{"kind":"project","projectId":51,"name":"Icon Pack"}' > "$D/projects_5.jsonl"
i=0; for n in Logo Intro Loader Menu; do i=$((i+1)); echo "{\"kind\":\"file\",\"fileId\":$i,\"path\":\"\",\"name\":\"$n\"}"; done > "$D/files_50.jsonl"
printf '%s\n' '{"kind":"file","fileId":5,"path":"social","name":"Heart"}' '{"kind":"file","fileId":6,"path":"","name":"Star"}' > "$D/files_51.jsonl"
for f in 1 2 3 4 5 6; do echo "v1-$f" > "$D/remote_$f"; done

# --- finding the mirror, init ---
out=$(cd "$T/elsewhere" && run); rc=$?
has "outside a mirror: says so" "Not inside a rive-mirror folder" "$out"; eq "outside a mirror: exit 2" 2 "$rc"
out=$(cd "$T/mirror" && run init); has "init creates a mirror" "created a mirror" "$out"
eq "init binds the signed-in account" alice "$(cat "$T/mirror/.rive-mirror/account")"
mkdir -p "$T/mirror/inner"; out=$(cd "$T/mirror/inner" && run init); has "init refuses nesting" "can't be nested" "$out"
out=$(cd "$HOME" && run init); has "init refuses the home folder" "Not creating a mirror" "$out"
mkdir -p "$T/filefolder"; : > "$T/filefolder/rive.yaml"; out=$(cd "$T/filefolder" && run init); has "init refuses a file folder" "downloaded file folder" "$out"

# --- sync ---
cd "$T/mirror"
out=$(run --dry-run); has "dry run counts" "6 files visible, 0 already mirrored, 6 to download" "$out"
out=$(run --yes); has "sync downloads everything" "done: 6 downloaded, 0 failed" "$out"
[ -f studio-5/icon-pack/social/heart-5/rive.yaml ] && has "folders are nested like the Editor" ok ok || has "folders are nested like the Editor" ok missing
out=$(run --dry-run); has "second run has nothing to do" "6 already mirrored, 0 to download" "$out"

# --- scope: commands act on the folder you are in ---
rm -rf studio-5/icon-pack/star-6
out=$(cd studio-5/brand && run --dry-run); has "scope: a sibling project is left alone" "0 to download" "$out"
out=$(cd studio-5/icon-pack && run --dry-run); has "scope: its own project is checked" "1 to download" "$out"
run --yes >/dev/null

# --- refresh ---
echo local >> studio-5/brand/intro-2/scene.rml; echo v2-3 > "$D/remote_3"; echo v2-4 > "$D/remote_4"; touch "$D/pullfail_4"
out=$(run --refresh --yes)
has "refresh skips edited folders" "1 with local changes" "$out"
has "refresh pulls what changed" "loader-3 (updated)" "$out"
has "refresh reports what did not change" "logo-1 (same)" "$out"
has "refresh reports failures" "menu-4 FAILED" "$out"
has "an edited folder keeps its edit" local "$(cat studio-5/brand/intro-2/scene.rml)"
rm .rive-mirror/stamps/1
out=$(run --refresh --dry-run); has "no checksum yet: skipped" "1 with no baseline yet" "$out"
out=$(run --refresh --baseline --yes); has "baseline records it" "recorded 1 folders" "$out"

# --- ignore lists: local and global ---
run ignore add icon >/dev/null
out=$(run --refresh --dry-run); has "local ignore hides a project" "4 mirrored folders in scope" "$out"
run ignore add --global brand >/dev/null
out=$(run --refresh --dry-run); has "global ignore applies too" "0 mirrored folders in scope" "$out"
run ignore rm icon >/dev/null; run ignore rm brand --global >/dev/null

# --- saved selection ---
printf '5\tStudio\tBrand\t+\n' > .rive-mirror/selection
out=$(cd studio-5 && run --dry-run --no-check)
has "the selection applies inside a subfolder" "Using your saved selection (1 rules)" "$out"
has "only the ticked project is listed" "4 files visible" "$out"
rm .rive-mirror/selection

# --- new things the rules don't cover are flagged, from the saved listing ---
printf '5\tStudio\tBrand\t+\n' > .rive-mirror/selection
out=$(run --yes); has "first run records a starting point" "Recorded what exists now" "$out"
printf '%s\n' '{"kind":"project","projectId":54,"name":"Newcomer"}' >> "$D/projects_5.jsonl"
echo '{"kind":"file","fileId":10,"path":"","name":"N"}' > "$D/files_54.jsonl"
: > "$D/calls.log"; out=$(run --dry-run)
has "a new project outside the rules is flagged" "project   Studio / Newcomer" "$out"
eq "the check does not list the account again" 1 "$(grep -c '^ls --workspace=5' "$D/calls.log")"
rm .rive-mirror/selection .rive-mirror/known "$D/files_54.jsonl"; sed -i.bak '/"Newcomer"/d' "$D/projects_5.jsonl"; run --dry-run --no-check >/dev/null

# --- account guard and lock ---
cp .rive-mirror/index.json "$T/index.before"
out=$(FAKE_USER=bob run --dry-run); rc=$?
has "wrong account is refused" "belongs to Rive account 'alice'" "$out"; eq "wrong account: exit 3" 3 "$rc"
cmp -s .rive-mirror/index.json "$T/index.before" && has "a refused run leaves the index alone" ok ok || has "a refused run leaves the index alone" ok changed
mkdir .rive-mirror/lock; sleep 30 & held=$!; echo "$held" > .rive-mirror/lock/pid
out=$(run --yes); rc=$?; has "a running copy blocks a second" "is running in this mirror" "$out"; eq "lock: exit 4" 4 "$rc"
kill "$held" 2>/dev/null; wait "$held" 2>/dev/null; echo 999999 > .rive-mirror/lock/pid
out=$(run --yes); rc=$?; eq "a stale lock is taken over" 0 "$rc"
[ ! -d .rive-mirror/lock ] && has "the lock is released afterwards" ok ok || has "the lock is released afterwards" ok left-behind

# --- --only, --workspace, --ignore for one run ---
out=$(run --dry-run --only=heart); has "--only with a word" "1 match" "$out"
out=$(run --dry-run --only='studio/icon pack'); has "--only with a path" "2 match" "$out"
out=$(run --dry-run --workspace=99); has "--workspace picks a workspace" "0 files visible" "$out"
out=$(run --dry-run --ignore=heart); has "--ignore for one run" "0 workspaces, 0 projects, 1 files" "$out"

# --- the saved index: the account is listed once, then it can be read from disk ---
[ -f .rive-mirror/index.json ] && has "a sync saves the index" ok ok || has "a sync saves the index" ok missing
has "workspaces are never switched" "0" "$(grep -c '^workspace [0-9]' "$D/calls.log")"
mkdir "$T/empty"
out=$(FAKE_DATA="$T/empty" run --offline --dry-run); has "offline answers from the index" "6 files visible, 6 already mirrored" "$out"
out=$(run --offline); has "offline refuses to download" "--offline only works with --dry-run" "$out"
: > "$D/calls.log"; out=$(run fetch); has "fetch lists the account" "listed 6 files" "$out"
has "fetch asks for workspaces, projects and files" "5" "$(wc -l < "$D/calls.log" | tr -d ' ')"

# a project that appears after the last listing is picked up; a failed listing falls back to the saved one
printf '%s\n' '{"kind":"project","projectId":52,"name":"Extra"}' >> "$D/projects_5.jsonl"
echo '{"kind":"file","fileId":7,"path":"","name":"Fresh"}' > "$D/files_52.jsonl"; echo "v1-7" > "$D/remote_7"
out=$(run --dry-run --no-check); has "a new project is found" "7 files visible, 6 already mirrored, 1 to download" "$out"
touch "$D/lsfail_51"; out=$(run --dry-run --no-check)
has "a failed listing warns" "warning: could not list project 51" "$out"
has "and falls back to the saved listing" "7 files visible" "$out"
rm "$D/lsfail_51" "$D/files_52.jsonl"; sed -i.bak '/"Extra"/d' "$D/projects_5.jsonl"; run --dry-run --no-check >/dev/null

# downloads run several at a time; a file that can't be converted is reported and leaves nothing behind
printf '%s\n' '{"kind":"project","projectId":53,"name":"Odd"}' >> "$D/projects_5.jsonl"
printf '%s\n' '{"kind":"file","fileId":8,"path":"","name":"Broken"}' '{"kind":"file","fileId":9,"path":"","name":"Fine"}' > "$D/files_53.jsonl"
echo v1-8 > "$D/remote_8"; echo v1-9 > "$D/remote_9"; touch "$D/createfail_8"
out=$(run --yes --jobs=3 --no-check)
has "a failed download is reported" "FAILED: cannot be written as markup" "$out"
has "and counted, while the others finish" "done: 1 downloaded, 1 failed" "$out"
[ ! -e studio-5/odd/broken-8 ] && has "a failed download leaves no partial folder" ok ok || has "a failed download leaves no partial folder" ok left-behind
[ -f studio-5/odd/fine-9/rive.yaml ] && has "the next file still downloads" ok ok || has "the next file still downloads" ok missing
rm "$D/createfail_8" "$D/files_53.jsonl"; sed -i.bak '/"Odd"/d' "$D/projects_5.jsonl"; rm -rf studio-5/odd; run --dry-run --no-check >/dev/null

# --- change detection (the engine) ---
E="$HERE/../rive-mirror-engine.py"
mkdir -p "$T/sum/sub dir" "$T/sum/.git"; echo a > "$T/sum/one.txt"; echo b > "$T/sum/sub dir/two txt"
echo junk > "$T/sum/.DS_Store"; echo x > "$T/sum/.git/config"
ref=$(cd "$T/sum" && LC_ALL=C find . -type f -not -path './.git/*' -not -name .DS_Store -print0 | LC_ALL=C sort -z | xargs -0 shasum -a 256 | shasum -a 256 | cut -d' ' -f1)
eq "checksum matches the original shell algorithm" "$ref" "$(python3 -I "$E" sum "$T/sum")"
python3 -I "$E" stamp "$T/st" 9 "$T/sum"
state() { printf '%s\t%s\t5\n' "$1" "$2" | python3 -I "$E" state "$T/st" | cut -f1; }
eq "an untouched folder is clean" clean "$(state 9 "$T/sum")"
touch "$T/sum/one.txt"; eq "new timestamp, same contents: still clean" clean "$(state 9 "$T/sum")"
echo changed > "$T/sum/one.txt"; eq "an edited file makes the folder changed" changed "$(state 9 "$T/sum")"
echo new > "$T/sum/extra.txt"; python3 -I "$E" stamp "$T/st" 9 "$T/sum"; rm "$T/sum/extra.txt"
eq "a removed file makes it changed" changed "$(state 9 "$T/sum")"
eq "no stamp yet" nobase "$(state 10 "$T/sum")"
eq "folder is gone" missing "$(state 9 "$T/nowhere")"
python3 -I "$E" stamp "$T/st" 9 "$T/sum"; touch -r "$T/sum/one.txt" "$T/ref.time"; printf 'zzzzzzz\n' > "$T/sum/one.txt"; touch -r "$T/ref.time" "$T/sum/one.txt"
eq "same size and time is trusted, like git's index" clean "$(state 9 "$T/sum")"

echo; echo "$pass passed, $fail failed"; [ "$fail" = 0 ]
