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

- **`rive push` inside a mirrored folder overwrites its Editor file.** `rive-mirror` itself never pushes.
- A plain run only creates folders that don't exist yet. It never changes one you already have.
- `--refresh` replaces a folder's contents with the Editor version, so it only does that for folders whose files still match what was downloaded. Unedited folders are always re-pulled, because the CLI doesn't report when a file last changed.
- Some Editor features can't be written as markup yet, so `rive create` fails for those files. `rive-mirror` removes the partial folder, reports the failure and carries on.
- New-item detection covers workspaces, projects and folders, not individual files.

## Tests

```sh
tests/run.sh
```

Runs against a fake `rive` in `tests/fake-rive/`: no network, no account. It covers init and its refusals, scope, downloads and their failures, refresh, ignore lists, the selection, the saved listing and offline use, `status`, `why`, the account check and the lock.

## License

MIT, see [LICENSE](LICENSE).
