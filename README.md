# rive-mirror

Keep local copies of the Rive files you can open, organized like your account:

```
<mirror>/<workspace>-<id>/<project>/<folders...>/<file>-<fileId>/
```

It is a small wrapper around the [Rive CLI](https://rive.app/docs/cli/overview) (`rive create --from-remote-file`, `rive pull`). The Editor stays the source of truth; the mirror is a working copy of it. Unofficial: not made by or affiliated with Rive.

A mirror works like a git repository. Run `init` in a folder to make it one; every command then finds it by looking upward from where you are, and acts on the folder you are in and everything under it.

## Requirements

- macOS (written and tested there), with bash, Python 3 and [`jq`](https://jqlang.github.io/jq/)
- The Rive CLI (tested with 1.8.0), signed in with `rive login`

## Install

```sh
git clone <this repo> && cd rive-mirror
ln -s "$PWD/rive-mirror" ~/.local/bin/rive-mirror    # any folder on your PATH
```

The script finds `rive-mirror-select.py` and `rive-mirror-engine.py` next to the real file, so keep the three together.

## Quick start

```sh
mkdir ~/RiveMirror && cd ~/RiveMirror
rive-mirror init          # makes this folder a mirror, tied to the account you are signed in as
rive-mirror select        # tick the workspaces, projects and folders you want
rive-mirror               # download what is missing
```

Later:

```sh
rive-mirror status        # where things stand, instantly, without asking Rive
rive-mirror refresh       # update folders from the Editor, only those you have not edited
rive-mirror why heart     # is "heart" mirrored? which rule says so?
rive-mirror -n            # see what would be downloaded, change nothing
```

## Commands

| Command | What it does |
| --- | --- |
| `init [PATH]` | Make a folder a mirror. Refuses your home folder, folders inside another mirror, and downloaded file folders. |
| `select` | Terminal browser: tick things to keep. Ticking a parent includes its children; untick some of them to exclude. `--list` shows the rules, `--clear` forgets them. |
| `sync` _(the default)_ | Download files you don't have yet, for your rules, inside the current folder. Flags new workspaces, projects and folders your rules don't cover (`--no-check` skips this). |
| `refresh` (or `--refresh`) | Pull the Editor version into mirrored folders that have **no local changes**. Folders you edited are listed and left alone. |
| `refresh --baseline` | One-off for folders downloaded before checksums existed: record them as unchanged as of now. |
| `refresh --baseline=changed` | After you pushed edited folders, record them as current again (use with `--only`). |
| `status` | An overview from the saved listing and the disk: mirrored, unchanged, locally changed, not downloaded, ignored, outside the selection. Never asks Rive. |
| `why NAME` | For workspaces, projects and files matching NAME: whether they are mirrored, and which selection rule or ignore word decides. Never asks Rive. |
| `fetch` | List your account and save the answer (`.rive-mirror/index.json`). Downloads nothing. |
| `ignore [add W.. \| rm W \| edit]` | Skip names containing a word. Local to this mirror by default; `--global` applies to every mirror. |
| `root` | Print the mirror you are in. |
| `move DEST` | Move this mirror, like `mv`: into `DEST` if it is an existing folder, otherwise renamed to `DEST`. Nothing is downloaded again and edits stay recorded as edits. Refuses to move into another mirror or a downloaded file folder, or while a run is going. `-n` shows where it would go. |
| `-C PATH` | Run as if started in `PATH`. |

Options: `--all` (ignore the saved rules for this run), `--only=WORD|PATH`, `--workspace=ID`, `--dry-run` / `-n`, `--offline` (with `--dry-run`: use the saved listing), `--yes` / `-y`, `--limit=N`, `--jobs=N`, `--ignore=WORD`, `--no-ignore`.

Inside a workspace, project, folder or a single file's folder, a run only touches that part, still following your saved rules.

## Where things are stored

- Global, in `~/.config/rive-mirror/` (or `$XDG_CONFIG_HOME`): the global ignore list and the list of known mirrors.
- Local, in `<mirror>/.rive-mirror/`: the selection, what you have already seen, folder checksums, the saved listing of your account (`index.json`), the Rive account the mirror belongs to, and the local ignore list. The folder contains a `.gitignore` so it is not committed by accident.

A mirror remembers the account it was created with and refuses to run when you are signed in as another one. Only one run per mirror at a time.

## Why it is quick

- The account is listed several requests at a time (`--jobs`, default 4). Requests that will probably still be needed start from the saved listing while the workspace and project lists refresh.
- Downloads and refreshes also run several at a time. The CLI's `create` and `pull` work on any file whatever its workspace, so nothing switches your CLI's current workspace.
- The listing is saved, so `status`, `why` and `--dry-run --offline` answer from disk.
- A folder's checksum comes with a cheap signature of its files' names, sizes and times, checked first, as git's index does. If it matches, nothing is read. This trusts a file whose size and modification time are unchanged.
- Selection, ignore words, `--only` and the current-folder scope are applied in one pass instead of once per file.

What cannot be quick: the CLI doesn't say when a file last changed in the Editor, so `refresh` must pull every unedited folder to find out whether anything changed.

## Things to know

- `rive-mirror` never pushes and never deletes. A plain run only creates folders that don't exist yet; it never changes one you already have.
- Some Editor features can't be written as markup yet, so `rive create` fails for those files. `rive-mirror` removes the partial folder, reports the failure and carries on.
- New-item detection covers workspaces, projects and folders, not individual files.

## Questions

**I edited a file locally and haven't pushed. Will `refresh` overwrite my edits?**
No. `refresh` only pulls into folders whose files still match their checksum from the last download or refresh. Folders with local changes are skipped and listed under "Skipped, local changes". Before pulling anything it asks `Pull N folders? [y/N]` (unless you pass `--yes`), and `-n` shows the list without pulling.

**I pushed my edits. Why does `refresh` still skip that folder?**
Its files still differ from the checksum recorded at download, so it still counts as changed. Record it as current with `rive-mirror refresh --baseline=changed --only=<name>`.

**A file was renamed or moved in the Editor. What happens to my copy?**
Folders are matched to Editor files by the `fileId` in their `rive.yaml`, not by name. Your folder keeps its old name, isn't downloaded again, and `refresh` still updates it. For the new name, rename the folder yourself (see the next question).

**Can I rename or move folders inside a mirror?**
Yes. A folder is still recognized after a rename or move inside the mirror, for the same reason. To move the whole mirror, use `rive-mirror move`.

**A file was deleted in the Editor, or I unticked it or added an ignore word. Is my copy deleted?**
No, nothing is ever deleted. `status` counts such folders as "outside these rules", and no command touches them. Delete them yourself if you no longer want them.

**Why don't `status` and `why` show what I just changed in the Editor?**
They read the saved listing and never ask Rive. Run `rive-mirror fetch` (or any plain run) to update it.

**It says the mirror belongs to another account.**
A mirror is tied to the Rive account it was created with. Run `rive login` with that account. The message also says which file to delete if you do want to rebind the mirror.

**It says another `rive-mirror` is running.**
Only one run per mirror at a time. Wait for the other one. If a run crashed, its leftover lock is cleared automatically the next time.

## Pitfalls

- **`rive push` overwrites the Editor file.** Every mirrored folder is linked to its Editor file, so `rive push` inside one replaces the Editor version, including anything changed in the Editor since you downloaded it. Check the Editor before pushing.
- **A copied folder still points at the original.** A copy of a mirrored folder keeps its `rive.yaml`, so `rive push` from the copy overwrites the original's Editor file. The mirror also tracks only one of the two. Keep copies outside the mirror, or remove the `push:` section from the copy's `rive.yaml`.
- **`--baseline=changed` before pushing loses your edits.** It records edited folders as unchanged, so the next `refresh` overwrites them. Run it only after pushing, and use `--only`, or every changed folder in scope is recorded.
- **Ignore words match parts of names, ignoring case.** `old` also skips "Bold" and "Golden". Use a wildcard such as `old*` or `*-old` to match a whole name, and check with `rive-mirror why NAME` or `-n` before a run.
- **Restoring old files can hide an edit.** To be quick, a file with the same size and modification time is trusted as unchanged. Tools that keep timestamps when copying (`cp -p`, `rsync -a`, unzipping a backup) can put back different contents that look unchanged, and `refresh` would then overwrite them. Use `--refresh -n` to see what would be pulled.
- **`--all` ignores your selection.** It downloads everything the account can open. Try it with `-n` first.

## Tests

```sh
tests/run.sh
```

Runs against a fake `rive` in `tests/fake-rive/`: no network, no account. It covers init and its refusals, scope, downloads and their failures, refresh, ignore lists, the selection, the saved listing and offline use, `status`, `why`, the account check and the lock.

## License

MIT, see [LICENSE](LICENSE).
