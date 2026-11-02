#!/usr/bin/env python3
"""rive-mirror-engine: the parts of rive-mirror that have to be fast.

Run by the rive-mirror script; not meant to be called by hand.

Change tracking
  A folder's checksum is saved in <mirror>/.rive-mirror/stamps/<fileId> after it is
  downloaded or refreshed. Line 1 is a hash of the folder's file contents. Line 2 is a
  cheap signature of every file's name, size and modification time. Like git's index,
  the signature is checked first: if nothing's size or time changed, the contents are
  assumed unchanged and nothing is read. Only when it differs are the files hashed.

  sum DIR                      print the content hash of a folder
  stamp STAMPS FID DIR         record the folder's current state
  state STAMPS < rows          rows are "fid TAB dir TAB wid"; prints
                               "state TAB fid TAB dir TAB wid" with state one of
                               clean | changed | nobase | missing

Listing your account
  The account is listed in parallel and the answers are kept in
  <mirror>/.rive-mirror/index.json, so commands that only look can answer from disk.

  run create|pull --root R --stamps S --result FILE < rows
                               run `rive create` / `rive pull` for each "fid TAB dir TAB wid"
                               row, several at a time, saving each folder's state afterwards

  why [rules] --name TEXT      explain, from the saved listing, whether workspaces, projects
                               and files matching TEXT are mirrored, and which rule decides

  fetch   [rules] --out DIR    list what is needed (or just read the saved index with
                               --offline) and write DIR/plan.tsv, one row per file
  classify [rules] --out DIR   apply selection, ignore words, --only and scope to plan.tsv;
                               write todo.tsv, ref.tsv, skipped.tsv and summary.env
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import stat
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed


# ---- change tracking -----------------------------------------------------

def scan(folder):
    """Regular files under folder as [(relative path as bytes, size, mtime_ns, absolute path)],
    sorted by path. Skips a top-level .git folder and any .DS_Store."""
    out = []
    for base, dirs, names in os.walk(folder):
        if base == folder and ".git" in dirs:
            dirs.remove(".git")
        for n in names:
            if n == ".DS_Store":
                continue
            p = os.path.join(base, n)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            out.append((os.fsencode(os.path.relpath(p, folder)), st.st_size, st.st_mtime_ns, p))
    out.sort(key=lambda e: e[0])
    return out


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def content_sum(files):
    """Same value the shell version produced: sha256 of the `shasum -a 256 ./path` lines."""
    h = hashlib.sha256()
    for rel, _size, _mtime, path in files:
        h.update(file_hash(path).encode() + b"  ./" + rel + b"\n")
    return h.hexdigest()


def signature(files):
    h = hashlib.sha256()
    for rel, size, mtime, _path in files:
        h.update(rel + b"\0" + str(size).encode() + b"\0" + str(mtime).encode() + b"\n")
    return h.hexdigest()


def read_stamp(path):
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().split("\n")
    except OSError:
        return None
    return (lines[0], lines[1] if len(lines) > 1 and lines[1] else None)


def write_stamp(path, total, sig):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("%s\n%s\n" % (total, sig))
    os.replace(tmp, path)


def folder_state(folder, stamp_path):
    if not os.path.isdir(folder):
        return "missing"
    stamp = read_stamp(stamp_path)
    if stamp is None or not stamp[0]:
        return "nobase"
    files = scan(folder)
    sig = signature(files)
    if stamp[1] == sig:
        return "clean"
    if content_sum(files) == stamp[0]:
        write_stamp(stamp_path, stamp[0], sig)   # same contents, new times: remember them
        return "clean"
    return "changed"


def record(stamp_path, folder):
    files = scan(folder)
    write_stamp(stamp_path, content_sum(files), signature(files))


# ---- names -----------------------------------------------------------------

def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def clean(s):
    return str(s).replace("\t", " ").replace("\n", " ")


# ---- rules: selection, ignore words, --only, scope ------------------------

def read_lines(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()
    except OSError:
        return []


def pattern_lines(path):
    out = []
    for line in read_lines(path):
        line = line.split("#", 1)[0].strip().lower()
        if line:
            out.append(line)
    return out


class Rules:
    """Everything that decides whether a workspace, project, folder or file is wanted."""

    def __init__(self, a):
        self.root = a.root
        self.local = a.local
        self.stamps = os.path.join(a.local, "stamps")
        self.use_sel = a.use_selection
        self.sel = []                      # (wid, lower path, "+"/"-")
        self.sel_label = {}                # (wid, lower path) -> "+ Workspace / path", for explanations
        if self.use_sel:
            for line in read_lines(os.path.join(a.local, "selection")):
                c = line.split("\t")
                if len(c) >= 3 and c[0].strip():
                    sign = "-" if len(c) > 3 and c[3] == "-" else "+"
                    self.sel.append((c[0], c[2].lower(), sign))
                    self.sel_label[(c[0], c[2].lower())] = "%s %s / %s" % (sign, c[1] or c[0], c[2] or "(whole workspace)")
        self.patterns = []                 # (pattern, where it came from)
        if not a.no_ignore:
            for p in pattern_lines(os.path.join(a.config, "ignore")):
                self.patterns.append((p, "global ignore list"))
            for p in pattern_lines(os.path.join(a.local, "ignore")):
                self.patterns.append((p, "this mirror's ignore list"))
            for p in (w.strip().lower() for w in a.ignore):
                if p:
                    self.patterns.append((p, "--ignore"))
        self.only = [p.strip().lower() for p in a.only if p.strip()]
        self.only_ws = set(a.workspace)
        self.scope_abs = a.scope_abs.lower()
        self.scope_ws = a.scope_ws
        self.scope_proj = a.scope_proj.lower()

    # -- ignore words
    def ignored(self, name):
        """(pattern, source) for the first word or wildcard matching this single name."""
        s = name.lower()
        for p, src in self.patterns:
            if "/" in p:
                continue
            if any(c in p for c in "*?["):
                if fnmatch.fnmatchcase(s, p):
                    return p, src
            elif p in s:
                return p, src
        return None

    def ignored_path(self, path):
        s = path.lower()
        for p, src in self.patterns:
            if "/" in p and any(fnmatch.fnmatchcase(s, g) for g in (p, p + "/*", "*/" + p, "*/" + p + "/*")):
                return p, src
        return None

    # -- --only
    def only_match(self, path):
        if not self.only:
            return True
        s = path.lower()
        segs = s.split("/")
        for p in self.only:
            if "/" in p:
                if fnmatch.fnmatchcase(s, p) or fnmatch.fnmatchcase(s, p + "/*"):
                    return True
            elif any(c in p for c in "*?["):
                if any(fnmatch.fnmatchcase(g, p) for g in segs):
                    return True
            elif p in s:
                return True
        return False

    def only_possible(self, *names):
        """Could anything below these workspace/project names still match --only?"""
        if not self.only:
            return True
        for p in self.only:
            if "/" not in p:
                return True
            ps = p.split("/")
            if all(fnmatch.fnmatchcase(k.lower(), ps[i]) for i, k in enumerate(names) if i < len(ps)):
                return True
        return False

    # -- selection: the most specific rule wins
    def sel_match(self, wid, path):
        """(included, winning rule path or None)."""
        s = path.lower()
        best, sign, rule = -1, "-", None
        for w, p, sg in self.sel:
            if w != str(wid):
                continue
            if p == "" or s == p or s.startswith(p + "/"):
                if len(p) > best:
                    best, sign, rule = len(p), sg, p
        return (sign == "+" and best >= 0), rule

    def sel_ws_possible(self, wid):
        return any(w == str(wid) and sg == "+" for w, _p, sg in self.sel)

    def sel_proj_possible(self, wid, pname):
        if self.sel_match(wid, pname)[0]:
            return True
        s = pname.lower() + "/"
        return any(w == str(wid) and sg == "+" and p.startswith(s) for w, p, sg in self.sel)

    # -- scope
    def in_scope(self, d):
        if not self.scope_abs:
            return True
        d = d.lower()
        return d == self.scope_abs or d.startswith(self.scope_abs + "/")

    def target_dir(self, wid, wname, pname, fpath, fname, fid):
        d = os.path.join(self.root, "%s-%s" % (slug(wname), wid), slug(pname))
        for seg in [x for x in fpath.split("/") if x]:
            d = os.path.join(d, slug(seg))
        return os.path.join(d, "%s-%s" % (slug(fname), fid))


def wanted_projects(rules, index):
    """The projects whose files this run needs, in the order the account listed them, plus how
    many workspaces and projects the ignore list skipped."""
    wanted, skip_ws, skip_proj = [], 0, 0
    for wid, wname in index.d["workspaces"]:
        if rules.only_ws and str(wid) not in rules.only_ws:
            continue
        if rules.scope_ws and str(wid) != rules.scope_ws:
            continue
        if rules.ignored(wname):
            skip_ws += 1
            continue
        if not rules.only_possible(wname):
            continue
        if rules.use_sel and not rules.sel_ws_possible(wid):
            continue
        for pid, pname in index.d["projects"].get(str(wid), {}).get("items", []):
            if rules.scope_proj and slug(pname) != rules.scope_proj:
                continue
            if rules.ignored(pname):
                skip_proj += 1
                continue
            if not rules.only_possible(wname, pname):
                continue
            if rules.use_sel and not rules.sel_proj_possible(wid, pname):
                continue
            wanted.append((wid, wname, pid, pname))
    return wanted, skip_ws, skip_proj


# ---- the index: what the account looked like the last time we asked ---------

class Index:
    def __init__(self, local):
        self.path = os.path.join(local, "index.json")
        self.d = {"version": 1, "fetched": 0, "workspaces": [], "projects": {}, "files": {}}
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
            if d.get("version") == 1:
                self.d = d
        except (OSError, ValueError):
            pass

    def save(self):
        keep = {str(pid) for v in self.d["projects"].values() for pid, _n in v["items"]}
        self.d["files"] = {k: v for k, v in self.d["files"].items() if k in keep}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.d, f)
        os.replace(tmp, self.path)

    def oldest(self):
        times = [v["fetched"] for v in self.d["files"].values()]
        return min(times) if times else 0


def rive_json(*args):
    r = subprocess.run(["rive", *args, "--json"], capture_output=True, text=True,
                       env=dict(os.environ, RIVE_NO_TUI="1"))
    if r.returncode != 0:
        lines = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(lines[-1] if lines else "rive exited with %d" % r.returncode)
    return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]


def whoami():
    """The signed-in Rive username, or None when not signed in."""
    try:
        r = subprocess.run(["rive", "whoami"], capture_output=True, text=True,
                           env=dict(os.environ, RIVE_NO_TUI="1"))
    except OSError:
        return None
    if r.returncode != 0:
        return None
    first = (r.stdout.splitlines() or [""])[0].split()
    return first[0] if first else ""


def account_problem(local, me):
    """None if the signed-in account may use this mirror, else the message to show. A mirror with
    no account yet is bound to the signed-in one."""
    if me is None:
        return "not signed in: run 'rive login'"
    path = os.path.join(local, "account")
    want = (read_lines(path) or [""])[0]
    if want:
        if me and want != me:
            return ("This mirror belongs to Rive account '%s' but you are signed in as '%s'.\n"
                    "  Sign in as '%s' (rive logout; rive login), or run rive-mirror in the mirror for '%s'.\n"
                    "  (To rebind this mirror to '%s', delete %s.)" % (want, me, want, me, me, path))
    elif me:
        with open(path, "w", encoding="utf-8") as f:
            f.write(me + "\n")
        print("(this mirror is now bound to Rive account '%s')" % me, file=sys.stderr)
    return None


def fetch(rules, index, jobs, max_age, check_local=None):
    """Ask the account for what this run needs: workspaces, then projects, then files.

    The saved index says what the last answer was, so requests that will probably still be
    needed start right away, alongside the ones that refresh the workspace and project lists;
    only what turns out to be new is asked for afterwards. Anything fetched less than max_age
    seconds ago is reused (0 = always ask). Returns warnings for requests that failed."""
    warnings = []
    fresh = lambda ts: max_age > 0 and time.time() - ts < max_age
    ws_ok = lambda wid: ((not rules.only_ws or str(wid) in rules.only_ws)
                         and (not rules.scope_ws or str(wid) == rules.scope_ws))
    projects_fresh = lambda wid: fresh(index.d["projects"].get(str(wid), {}).get("fetched", 0))
    files_fresh = lambda pid: fresh(index.d["files"].get(str(pid), {}).get("fetched", 0))

    def list_projects(wid):
        rows = rive_json("ls", "--workspace=%s" % wid)
        return [[r["projectId"], r["name"]] for r in rows if r.get("kind") == "project"]

    def list_files(wid, pid):
        rows = rive_json("ls", str(pid), "--all", "--workspace=%s" % wid)
        return [[r["fileId"], r.get("path") or "", r["name"]] for r in rows if r.get("kind") == "file"]

    ex = ThreadPoolExecutor(max_workers=jobs + 2)
    try:
        who = ex.submit(whoami) if check_local else None
        need_ws = not (fresh(index.d["fetched"]) and index.d["workspaces"])
        f_ws = ex.submit(rive_json, "workspace") if need_ws else None
        f_proj, f_files = {}, {}
        for wid, _n in index.d["workspaces"]:
            if ws_ok(wid) and not projects_fresh(wid):
                f_proj[wid] = ex.submit(list_projects, wid)
        if index.d["workspaces"]:
            for wid, _wn, pid, _pn in wanted_projects(rules, index)[0]:
                if not files_fresh(pid):
                    f_files[pid] = (wid, ex.submit(list_files, wid, pid))

        # Who you are is checked before anything is kept.
        if who is not None:
            problem = account_problem(check_local, who.result())
            if problem:
                print(problem, file=sys.stderr)
                raise SystemExit(3)
        if f_ws is not None:
            try:
                rows = f_ws.result()
            except (RuntimeError, OSError) as e:
                raise SystemExit("could not list your workspaces: %s" % e)
            index.d["workspaces"] = [[r["workspaceId"], r["name"]] for r in rows if r.get("kind") == "workspace"]
            index.d["fetched"] = time.time()
        for wid, _n in index.d["workspaces"]:           # workspaces we had not heard of
            if ws_ok(wid) and wid not in f_proj and not projects_fresh(wid):
                f_proj[wid] = ex.submit(list_projects, wid)
        for wid, fut in f_proj.items():
            try:
                index.d["projects"][str(wid)] = {"fetched": time.time(), "items": fut.result()}
            except (RuntimeError, OSError) as e:
                warnings.append("could not list the projects of workspace %s: %s" % (wid, e))

        wanted = wanted_projects(rules, index)[0]       # now that the lists are up to date
        for wid, _wn, pid, _pn in wanted:
            if pid not in f_files and not files_fresh(pid):
                f_files[pid] = (wid, ex.submit(list_files, wid, pid))
        still = {pid for _w, _wn, pid, _pn in wanted}
        for pid, (wid, fut) in f_files.items():
            try:
                rows = fut.result()
            except (RuntimeError, OSError) as e:
                if pid in still:
                    warnings.append("could not list project %s: %s" % (pid, e))
                continue
            index.d["files"][str(pid)] = {"fetched": time.time(), "rows": rows}
    finally:
        ex.shutdown(wait=False, cancel_futures=True)
    return warnings


def plan_rows(rules, index):
    """One row per file in the projects this run needs, from the index. Also returns how many
    wanted projects have never been fetched, and the ignore-list skip counts."""
    wanted, skip_ws, skip_proj = wanted_projects(rules, index)
    rows, unfetched = [], 0
    for wid, wname, pid, pname in wanted:
        entry = index.d["files"].get(str(pid))
        if entry is None:
            unfetched += 1
            continue
        for fid, fpath, fname in entry["rows"]:
            rows.append((wid, wname, pname, fid, fpath, fname))
    return rows, unfetched, skip_ws, skip_proj


def folders_on_disk(root):
    """{fileId: folder} for every mirrored project folder (anything with a rive.yaml that has a
    push: fileId). Doesn't look inside a project folder."""
    have = {}
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".rive-mirror", ".libraries")]
        if "rive.yaml" in names:
            dirs[:] = []
            in_push = False
            try:
                with open(os.path.join(base, "rive.yaml"), encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if line.startswith("push:"):
                            in_push = True
                        elif line.strip() and line[0] not in " \t":
                            in_push = False
                        elif in_push:
                            m = re.match(r"\s*fileId:\s*(\S+)", line)
                            if m:
                                have.setdefault(m.group(1), base)
                                break
            except OSError:
                pass
    return have


def classify(rules, rows, want_refresh):
    have = folders_on_disk(rules.root)
    todo, ref, skipped = [], [], []
    count = dict(total=0, have=0, nign=0, nonly=0, nsel=0, outside=0)
    for wid, wname, pname, fid, fpath, fname in rows:
        fpath = "" if fpath in (".", None) else fpath
        fid = str(fid)
        if rules.scope_abs:
            where = have.get(fid) or rules.target_dir(wid, wname, pname, fpath, fname, fid)
            if not rules.in_scope(where):
                continue
        count["total"] += 1
        inner = (fpath + "/" if fpath else "") + fname
        if not rules.only_match("%s/%s/%s" % (wname, pname, inner)):
            count["nonly"] += 1
            continue
        if rules.use_sel and not rules.sel_match(wid, "%s/%s" % (pname, inner))[0]:
            count["nsel"] += 1
            continue
        if (rules.ignored(fname) or rules.ignored_path("%s/%s/%s" % (wname, pname, inner))
                or any(rules.ignored(seg) for seg in fpath.split("/") if seg)):
            count["nign"] += 1
            skipped.append(("file", inner))
            continue
        if fid in have:
            count["have"] += 1
            if want_refresh:
                ref.append((fid, have[fid], wid))
            continue
        todo.append((fid, rules.target_dir(wid, wname, pname, fpath, fname, fid), wid))
    # folders on disk in this scope that the rules above don't reach (excluded, or gone from the account)
    count["outside"] = sum(1 for d in have.values() if rules.in_scope(d)) - count["have"]
    return todo, ref, skipped, count


# ---- running create / pull -------------------------------------------------

def last_line(text):
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return lines[-1] if lines else "no output"


def run_one(kind, root, stamps, fid, folder, inflight):
    """One create or pull. Returns (ok, note)."""
    env = dict(os.environ, RIVE_NO_TUI="1")
    if kind == "create":
        os.makedirs(os.path.dirname(folder), exist_ok=True)
        inflight.add(folder)
        r = subprocess.run(["rive", "create", folder, "--from-remote-file=%s" % fid],
                           capture_output=True, text=True, env=env)
        inflight.discard(folder)
        if r.returncode != 0:
            shutil.rmtree(folder, ignore_errors=True)    # never leave a half-written copy
            return False, last_line(r.stdout + r.stderr)
        record(os.path.join(stamps, fid), folder)
        return True, ""
    before = (read_stamp(os.path.join(stamps, fid)) or ("", None))[0]
    r = subprocess.run(["rive", "pull", folder, "--yes", "--quiet"], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        return False, last_line(r.stdout + r.stderr)
    record(os.path.join(stamps, fid), folder)
    return True, "same" if (read_stamp(os.path.join(stamps, fid)) or ("",))[0] == before else "updated"


def cmd_run(argv):
    p = argparse.ArgumentParser(prog="rive-mirror-engine run")
    p.add_argument("kind", choices=["create", "pull"])
    p.add_argument("--root", required=True)
    p.add_argument("--stamps", required=True)
    p.add_argument("--result", required=True)
    p.add_argument("--jobs", type=int, default=int(os.environ.get("RIVE_MIRROR_JOBS") or 4))
    a = p.parse_args(argv)
    rows = [l.rstrip("\n").split("\t") for l in sys.stdin if l.strip()]
    rows = [r for r in rows if len(r) >= 3]
    n, done, failed, updated, same = len(rows), 0, 0, 0, 0
    inflight = set()
    ex = ThreadPoolExecutor(max_workers=max(1, min(a.jobs, 8)))
    try:
        futures = {ex.submit(run_one, a.kind, a.root, a.stamps, r[0], r[1], inflight): r for r in rows}
        for fut in as_completed(futures):
            fid, folder = futures[fut][0], futures[fut][1]
            rel = os.path.relpath(folder, a.root)
            done += 1
            try:
                ok, note = fut.result()
            except OSError as e:
                ok, note = False, str(e)
            if ok:
                if a.kind == "pull":
                    updated, same = updated + (note == "updated"), same + (note == "same")
                    print("[%d/%d] %s (%s)" % (done, n, rel, note), flush=True)
                else:
                    print("[%d/%d] %s" % (done, n, rel), flush=True)
            else:
                failed += 1
                if a.kind == "pull":
                    print("[%d/%d] %s FAILED: %s" % (done, n, rel, note), flush=True)
                else:
                    print("[%d/%d] %s\n   FAILED: %s" % (done, n, rel, note), flush=True)
    except KeyboardInterrupt:
        ex.shutdown(wait=False, cancel_futures=True)
        for folder in list(inflight):                    # unfinished downloads leave nothing behind
            shutil.rmtree(folder, ignore_errors=True)
        print("\ninterrupted", file=sys.stderr)
        return 130
    finally:
        ex.shutdown(wait=True)
    with open(a.result, "w") as f:
        f.write("TOTAL=%d\nFAILED=%d\nUPDATED=%d\nSAME=%d\n" % (n, failed, updated, same))
    return 0


# ---- command line ---------------------------------------------------------

def cmd_state(stamps):
    rows = [l.rstrip("\n").split("\t") for l in sys.stdin if l.strip()]
    rows = [r for r in rows if len(r) >= 3]
    with ThreadPoolExecutor(max_workers=8) as ex:
        states = list(ex.map(lambda r: folder_state(r[1], os.path.join(stamps, r[0])), rows))
    for st, r in zip(states, rows):
        print("%s\t%s\t%s\t%s" % (st, r[0], r[1], r[2]))


def add_rule_args(p):
    p.add_argument("--root", required=True)
    p.add_argument("--local", required=True)
    p.add_argument("--config", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--use-selection", action="store_true")
    p.add_argument("--no-ignore", action="store_true")
    p.add_argument("--ignore", action="append", default=[])
    p.add_argument("--only", action="append", default=[])
    p.add_argument("--workspace", action="append", default=[])
    p.add_argument("--scope-abs", default="")
    p.add_argument("--scope-ws", default="")
    p.add_argument("--scope-proj", default="")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--name", default="")
    p.add_argument("--check-account", action="store_true", help="refuse unless the signed-in account owns this mirror")
    p.add_argument("--offline", action="store_true", help="never ask the account; use the saved index")
    p.add_argument("--max-age", type=int, default=0, help="reuse listings newer than this many seconds")
    p.add_argument("--jobs", type=int, default=int(os.environ.get("RIVE_MIRROR_JOBS") or 4))


def write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write("\t".join(clean(x) for x in r) + "\n")


def explain(rules, index, have, a):
    """Yield (heading, [reasons], verdict) for the workspaces, projects and files matching a.name."""
    needle = a.name.lower()
    ws_names = dict((str(w), n) for w, n in index.d["workspaces"])

    def parent_checks(wid, wname, pname=None):
        """Reasons a workspace (and project) is skipped; empty if it is in play."""
        why = []
        if rules.only_ws and str(wid) not in rules.only_ws:
            why.append("not in the --workspace list")
        if rules.scope_ws and str(wid) != rules.scope_ws:
            why.append("outside the folder you are running in")
        hit = rules.ignored(wname)
        if hit:
            why.append("workspace name matches ignore word '%s' (%s)" % hit)
        if not rules.only_possible(wname):
            why.append("excluded by --only")
        if rules.use_sel and not rules.sel_ws_possible(wid):
            why.append("no selection rule includes anything in this workspace")
        if pname is not None and not why:
            if rules.scope_proj and slug(pname) != rules.scope_proj:
                why.append("outside the folder you are running in")
            hit = rules.ignored(pname)
            if hit:
                why.append("project name matches ignore word '%s' (%s)" % hit)
            if not rules.only_possible(wname, pname):
                why.append("excluded by --only")
            if rules.use_sel and not rules.sel_proj_possible(wid, pname):
                why.append("no selection rule includes anything in this project")
        return why

    for wid, wname in index.d["workspaces"]:
        if needle in wname.lower():
            why = parent_checks(wid, wname)
            yield ("workspace  %s" % wname, why or ["in play: its projects are checked one by one"],
                   "skipped" if why else "included")
    for wid, wname in index.d["workspaces"]:
        for pid, pname in index.d["projects"].get(str(wid), {}).get("items", []):
            if needle in pname.lower():
                why = parent_checks(wid, wname, pname)
                note = [] if why else ["in play: its files are checked one by one"]
                if not why and str(pid) not in index.d["files"]:
                    note = ["in play, but its files have not been listed yet: run 'rive-mirror fetch'"]
                yield ("project    %s / %s" % (wname, pname), why or note, "skipped" if why else "included")
    for wid, wname in index.d["workspaces"]:
        for pid, pname in index.d["projects"].get(str(wid), {}).get("items", []):
            for fid, fpath, fname in index.d["files"].get(str(pid), {}).get("rows", []):
                inner = (fpath + "/" if fpath else "") + fname
                if needle not in inner.lower():
                    continue
                label = "file       %s / %s / %s" % (wname, pname, inner)
                why = parent_checks(wid, wname, pname)
                if not why:
                    path = "%s/%s" % (pname, inner)
                    if not rules.only_match("%s/%s" % (wname, path)):
                        why.append("excluded by --only")
                    if rules.use_sel:
                        inc, rule = rules.sel_match(wid, path)
                        if not inc:
                            why.append("selection rule '%s' excludes it" % rules.sel_label[(str(wid), rule)]
                                       if rule is not None else "no selection rule covers it")
                    hit = rules.ignored(fname) or rules.ignored_path("%s/%s/%s" % (wname, pname, inner))
                    for seg in fpath.split("/"):
                        hit = hit or (rules.ignored(seg) if seg else None)
                    if hit:
                        why.append("matches ignore word '%s' (%s)" % hit)
                if why:
                    yield (label, why, "skipped")
                    continue
                reasons = []
                if rules.use_sel:
                    inc, rule = rules.sel_match(wid, "%s/%s" % (pname, inner))
                    reasons.append("covered by selection rule '%s'" % rules.sel_label[(str(wid), rule)])
                where = have.get(str(fid))
                if where:
                    state = folder_state(where, os.path.join(rules.stamps, str(fid)))
                    reasons.append("on disk: %s (%s)" % (os.path.relpath(where, rules.root), {
                        "clean": "no local changes", "changed": "has local changes",
                        "nobase": "no checksum recorded yet", "missing": "folder missing"}[state]))
                else:
                    reasons.append("not downloaded yet: the next run will download it")
                yield (label, reasons, "included")


def cmd_why(a):
    rules, index = Rules(a), Index(a.local)
    if not index.d["workspaces"]:
        print("nothing saved yet: run 'rive-mirror fetch' first")
        return
    have = folders_on_disk(rules.root)
    shown = 0
    for heading, reasons, verdict in explain(rules, index, have, a):
        if shown == 12:
            print("... more matches; use a longer name to narrow it down")
            break
        shown += 1
        print(heading)
        for r in reasons:
            print("  - " + r)
        print("  => " + ("will be mirrored" if verdict == "included" else "skipped"))
    if not shown:
        print("nothing in the saved listing matches '%s' (listing is updated by 'rive-mirror fetch')" % a.name)


def cmd_fetch(a):
    rules, index = Rules(a), Index(a.local)
    os.makedirs(a.out, exist_ok=True)
    if not a.offline:
        for w in fetch(rules, index, max(1, min(a.jobs, 8)), a.max_age, a.local if a.check_account else None):
            print("warning: " + w, file=sys.stderr)
        index.save()
    rows, unfetched, skip_ws, skip_proj = plan_rows(rules, index)
    write_rows(os.path.join(a.out, "plan.tsv"),
               [(w, wn, pn, fid, fp or ".", fn) for w, wn, pn, fid, fp, fn in rows])
    with open(os.path.join(a.out, "fetch.env"), "w") as f:
        f.write("UNFETCHED=%d\nSKIP_WS=%d\nSKIP_PROJ=%d\nINDEX_AGE=%d\n" % (
            unfetched, skip_ws, skip_proj, int(time.time() - index.oldest()) if index.d["files"] else -1))


def cmd_classify(a):
    rules = Rules(a)
    rows = []
    for line in read_lines(os.path.join(a.out, "plan.tsv")):
        c = line.split("\t")
        if len(c) >= 6:
            rows.append((c[0], c[1], c[2], c[3], c[4], c[5]))
    todo, ref, skipped, count = classify(rules, rows, a.refresh)
    write_rows(os.path.join(a.out, "todo.tsv"), todo)
    write_rows(os.path.join(a.out, "ref.tsv"), ref)
    write_rows(os.path.join(a.out, "skipped.tsv"), skipped)
    with open(os.path.join(a.out, "summary.env"), "w") as f:
        f.write("TOTAL=%d\nHAVE=%d\nNIGN=%d\nNONLY=%d\nNSEL=%d\nOUTSIDE=%d\nNTODO=%d\n" % (
            count["total"], count["have"], count["nign"], count["nonly"], count["nsel"],
            count["outside"], len(todo)))


def cli(argv):
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    if argv and argv[0] in ("fetch", "classify", "why"):
        p = argparse.ArgumentParser(prog="rive-mirror-engine " + argv[0])
        add_rule_args(p)
        a = p.parse_args(argv[1:])
        {"fetch": cmd_fetch, "classify": cmd_classify, "why": cmd_why}[argv[0]](a)
        return 0
    if argv and argv[0] == "account":
        p = argparse.ArgumentParser(prog="rive-mirror-engine account")
        p.add_argument("--local", required=True)
        problem = account_problem(p.parse_args(argv[1:]).local, whoami())
        if problem:
            print(problem, file=sys.stderr)
            return 3
        return 0
    if len(argv) >= 2 and argv[0] == "sum":
        print(content_sum(scan(argv[1])))
    elif len(argv) >= 4 and argv[0] == "stamp":
        record(os.path.join(argv[1], argv[2]), argv[3])
    elif len(argv) >= 2 and argv[0] == "state":
        cmd_state(argv[1])
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(cli(sys.argv[1:]))
