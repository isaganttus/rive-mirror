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

# --- account guard and lock ---
out=$(FAKE_USER=bob run --dry-run); rc=$?
has "wrong account is refused" "belongs to Rive account 'alice'" "$out"; eq "wrong account: exit 3" 3 "$rc"
mkdir .rive-mirror/lock; sleep 30 & held=$!; echo "$held" > .rive-mirror/lock/pid
out=$(run --yes); rc=$?; has "a running copy blocks a second" "is running in this mirror" "$out"; eq "lock: exit 4" 4 "$rc"
kill "$held" 2>/dev/null; wait "$held" 2>/dev/null; echo 999999 > .rive-mirror/lock/pid
out=$(run --yes); rc=$?; eq "a stale lock is taken over" 0 "$rc"
[ ! -d .rive-mirror/lock ] && has "the lock is released afterwards" ok ok || has "the lock is released afterwards" ok left-behind

echo; echo "$pass passed, $fail failed"; [ "$fail" = 0 ]
