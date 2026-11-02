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

The script finds `rive-mirror-select.py` next to the real file, so keep the two together.

## Quick start

```sh
mkdir ~/RiveMirror && cd ~/RiveMirror
rive-mirror init          # makes this folder a mirror, tied to the account you are signed in as
rive-mirror select        # tick the workspaces, projects and folders you want
rive-mirror               # download what is missing
```

Later:

```sh
rive-mirror --refresh     # update folders from the Editor, only those you have not edited
rive-mirror --dry-run     # see what would be downloaded, change nothing
```

## Commands

| Command | What it does |
| --- | --- |
| `init [PATH]` | Make a folder a mirror. Refuses your home folder, folders inside another mirror, and downloaded file folders. |
| `select` | Terminal browser: tick things to keep. Ticking a parent includes its children; untick some of them to exclude. `--list` shows the rules, `--clear` forgets them. |
| _(no command)_ | Download files you don't have yet, for your rules, inside the current folder. Flags new workspaces, projects and folders your rules don't cover (`--no-check` skips this). |
| `--refresh` | Pull the Editor version into mirrored folders that have **no local changes**. Folders you edited are listed and left alone. |
| `--refresh --baseline` | One-off for folders downloaded before checksums existed: record them as unchanged as of now. |
| `--refresh --baseline=changed` | After you pushed edited folders, record them as current again (use with `--only`). |
| `ignore [add W.. \| rm W \| edit]` | Skip names containing a word. Local to this mirror by default; `--global` applies to every mirror. |
| `root` | Print the mirror you are in. |
| `-C PATH` | Run as if started in `PATH`. |

Options: `--all` (ignore the saved rules for this run), `--only=WORD|PATH`, `--workspace=ID`, `--dry-run`, `--yes`, `--limit=N`, `--ignore=WORD`, `--no-ignore`.

Inside a workspace, project, folder or a single file's folder, a run only touches that part, still following your saved rules.

## Where things are stored

- Global, in `~/.config/rive-mirror/` (or `$XDG_CONFIG_HOME`): the global ignore list and the list of known mirrors.
- Local, in `<mirror>/.rive-mirror/`: the selection, what you have already seen, folder checksums, the Rive account the mirror belongs to, and the local ignore list. The folder contains a `.gitignore` so it is not committed by accident.

A mirror remembers the account it was created with and refuses to run when you are signed in as another one. Only one run per mirror at a time.

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

Runs against a fake `rive` in `tests/fake-rive/`: no network, no account.

## License

MIT, see [LICENSE](LICENSE).
