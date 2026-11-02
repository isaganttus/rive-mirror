#!/usr/bin/env python3
"""rive-mirror-select: browse workspaces, projects and folders and tick what to keep mirrored.

Saves to <mirror>/.rive-mirror/selection (the mirror you are in),
one rule per line:  <workspaceId> TAB <workspace name> TAB <project/folder/...> TAB <+ or ->
An empty path means the whole workspace. "+" includes a node and everything inside it;
"-" excludes one. The most specific rule wins, so you can tick a workspace and untick
some of its children (or tick one back inside an unticked child). Lines with no sign are "+".
Only reads from your Rive account (rive workspace / rive ls); never writes to it.
"""
import curses
import json
import locale
import os
import subprocess
import sys

CONF_DIR = os.environ.get("RIVE_MIRROR_LOCAL")   # <mirror>/.rive-mirror, set by rive-mirror
if not CONF_DIR:
    sys.exit("run this through `rive-mirror select` from inside a mirror folder")
SEL_FILE = os.path.join(CONF_DIR, "selection")
KNOWN_FILE = os.path.join(CONF_DIR, "known")
ENV = dict(os.environ, RIVE_NO_TUI="1")

HELP = "↑↓ move   → open   ← back   space tick/untick   a all here   c clear   s save   esc cancel"
HELP_ASCII = "up/down move  right open  left back  space tick/untick  a all here  c clear  s save  esc cancel"


def rive_json(*args):
    r = subprocess.run(["rive", *args, "--json"], capture_output=True, text=True, env=ENV)
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).strip().splitlines()
        raise RuntimeError(msg[-1] if msg else "rive failed (exit %d)" % r.returncode)
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


class Node:
    def __init__(self, kind, name, parent=None, wid=None, pid=None):
        self.kind, self.name, self.parent, self.wid, self.pid = kind, name, parent, wid, pid
        self.children = None      # None until loaded
        self.expanded = False
        self.nfiles = None

    @property
    def path(self):
        parts, n = [], self
        while n is not None and n.kind != "ws":
            parts.append(n.name)
            n = n.parent
        return tuple(reversed(parts))

    @property
    def expandable(self):
        return self.kind in ("ws", "project", "folder")

    def depth(self):
        d, n = 0, self.parent
        while n is not None:
            d, n = d + 1, n.parent
        return d


def build_project_tree(project, rows):
    """Turn a flat list of file rows (with folder paths) into folder/file nodes."""
    folders = {(): project}
    project.children = []
    counts = {}

    def folder_for(segs):
        if segs in folders:
            return folders[segs]
        parent = folder_for(segs[:-1])
        node = Node("folder", segs[-1], parent, project.wid, project.pid)
        node.children = []
        parent.children.append(node)
        folders[segs] = node
        return node

    for row in rows:
        if row.get("kind") != "file":
            continue
        segs = tuple(s for s in (row.get("path") or "").split("/") if s)
        parent = folder_for(segs)
        parent.children.append(Node("file", row["name"], parent, project.wid, project.pid))
        for i in range(len(segs) + 1):
            counts[segs[:i]] = counts.get(segs[:i], 0) + 1
    for segs, node in folders.items():
        node.nfiles = counts.get(segs, 0)
        node.children.sort(key=lambda c: (c.kind == "file", c.name.casefold()))


def read_selection():
    """Return ({(wid, path): include?}, {wid: name})."""
    sel, names = {}, {}
    try:
        with open(SEL_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) < 2 or not parts[0].strip().isdigit():
                    continue
                wid = int(parts[0])
                path = tuple(s for s in (parts[2] if len(parts) > 2 else "").split("/") if s)
                sel[(wid, path)] = not (len(parts) > 3 and parts[3].strip() == "-")
                names[wid] = parts[1]
    except FileNotFoundError:
        pass
    return sel, names


def write_selection(sel, names):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = SEL_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for (wid, path), include in sorted(sel.items(), key=lambda e: (e[0][0], e[0][1])):
            f.write("%d\t%s\t%s\t%s\n" % (wid, names.get(wid, ""), "/".join(path), "+" if include else "-"))
    os.replace(tmp, SEL_FILE)


def main(stdscr, workspaces, sel, names):
    curses.curs_set(0)
    try:
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
    except curses.error:
        pass
    original = dict(sel)
    roots = [Node("ws", w["name"], None, w["workspaceId"]) for w in workspaces]
    wsname = {r.wid: r.name for r in roots}
    cur, top, msg = 0, 0, ""
    utf8 = "utf" in (locale.getpreferredencoding() or "").lower()
    help_line = HELP if utf8 else HELP_ASCII

    def visible():
        out = []

        def walk(n):
            out.append(n)
            if n.expanded and n.children:
                for c in n.children:
                    walk(c)
        for r in roots:
            walk(r)
        return out

    def rule_for(wid, path):
        """Nearest rule at or above path: the most specific one wins."""
        for i in range(len(path), -1, -1):
            v = sel.get((wid, path[:i]))
            if v is not None:
                return v
        return None

    def selected(n):
        return bool(rule_for(n.wid, n.path))

    def inside(n):
        return [(p, v) for (w, p), v in sel.items()
                if w == n.wid and len(p) > len(n.path) and p[:len(n.path)] == n.path]

    def state(n):
        if selected(n):
            return "partial" if any(not v for _, v in inside(n)) else "full"
        return "partial" if any(v for _, v in inside(n)) else "none"

    def set_selected(n, want):
        """Tick or untick n and everything under it, adding a rule only if the parent disagrees."""
        for p, _ in inside(n):
            del sel[(n.wid, p)]
        sel.pop((n.wid, n.path), None)
        inherited = bool(n.path) and bool(rule_for(n.wid, n.path[:-1]))
        if want != inherited:
            sel[(n.wid, n.path)] = want

    def draw(status=""):
        rows = visible()
        h, w = stdscr.getmaxyx()
        stdscr.erase()
        try:
            stdscr.addnstr(0, 0, "rive-mirror: choose what to keep mirrored", w - 1, curses.A_BOLD)
            stdscr.addnstr(1, 0, help_line, w - 1, curses.A_DIM)
            body = h - 4
            for i, n in enumerate(rows[top:top + body]):
                st = state(n)
                box = {"full": "[x]", "partial": "[-]", "none": "[ ]"}[st]
                mark = ("- " if n.expanded else "+ ") if n.expandable else "  "
                extra = ""
                if n.kind in ("project", "folder") and n.nfiles is not None:
                    extra = "  (%d)" % n.nfiles
                elif n.kind == "ws" and n.children is not None:
                    extra = "  (%d projects)" % len(n.children)
                line = "%s%s %s%s%s" % ("  " * n.depth(), box, mark, n.name, extra)
                attr = curses.A_REVERSE if top + i == cur else 0
                if st in ("full", "partial"):
                    attr |= curses.color_pair(1)
                    if selected(n) and (n.wid, n.path) not in sel:
                        attr |= curses.A_DIM      # ticked only because a parent is
                stdscr.addnstr(2 + i, 0, line, w - 1, attr)
            inc = sum(1 for v in sel.values() if v)
            exc = len(sel) - inc
            foot = "%d ticked, %d unticked%s" % (inc, exc, ("   " + status) if status else "")
            stdscr.addnstr(h - 1, 0, foot, w - 1, curses.color_pair(2))
        except curses.error:
            pass
        stdscr.refresh()

    def load(n):
        if n.children is not None:
            return
        draw("loading…")
        try:
            if n.kind == "ws":
                rows = rive_json("ls", "--workspace=%d" % n.wid)
                n.children = [Node("project", r["name"], n, n.wid, r["projectId"])
                              for r in rows if r.get("kind") == "project"]
                n.children.sort(key=lambda c: c.name.casefold())
            elif n.kind == "project":
                rows = rive_json("ls", str(n.pid), "--all", "--workspace=%d" % n.wid)
                build_project_tree(n, rows)
        except (RuntimeError, OSError) as e:
            n.children = None
            raise RuntimeError(str(e))

    while True:
        rows = visible()
        cur = max(0, min(cur, len(rows) - 1))
        h, _ = stdscr.getmaxyx()
        body = max(1, h - 4)
        if cur < top:
            top = cur
        if cur >= top + body:
            top = cur - body + 1
        draw(msg)
        msg = ""
        k = stdscr.getch()
        n = rows[cur] if rows else None
        if k in (curses.KEY_UP, ord("k")):
            cur -= 1
        elif k in (curses.KEY_DOWN, ord("j")):
            cur += 1
        elif k in (curses.KEY_RIGHT, ord("l"), 10, 13, curses.KEY_ENTER) and n and n.expandable:
            try:
                load(n)
            except RuntimeError as e:
                msg = "error: " + str(e)
                continue
            if n.expanded and n.children:
                cur += 1
            n.expanded = True
        elif k in (curses.KEY_LEFT, ord("h")) and n:
            if n.expanded:
                n.expanded = False
            elif n.parent is not None:
                cur = rows.index(n.parent)
        elif k == ord(" ") and n:
            set_selected(n, not selected(n))
        elif k == ord("a") and n:
            sibs = n.parent.children if n.parent else roots
            want = not all(selected(s) and state(s) == "full" for s in sibs)
            for s in sibs:
                set_selected(s, want)
        elif k == ord("c"):
            sel.clear()
        elif k in (ord("s"), ord("q")):
            return True
        elif k == 27:  # esc
            if sel == original:
                return False
            draw("discard changes? y/n")
            if stdscr.getch() in (ord("y"), ord("Y")):
                return False
        elif k == curses.KEY_RESIZE:
            pass


# ---- "what's new?" check -------------------------------------------------
# `known` remembers which workspaces (w), projects (p) and folders (f) you have already seen,
# plus a "b" marker per project meaning its folders have been recorded. Anything not in it is
# new. New things your rules already cover are just counted; new things they don't cover are
# shown so you can add them to the selection, dismiss them, or decide later.

def read_known():
    out = set()
    try:
        with open(KNOWN_FILE, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 3 and parts[1].isdigit():
                    out.add((parts[0], int(parts[1]), parts[2]))
    except FileNotFoundError:
        pass
    return out


def write_known(known):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = KNOWN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for kind, wid, path in sorted(known):
            f.write("%s\t%d\t%s\n" % (kind, wid, path))
    os.replace(tmp, KNOWN_FILE)


def snapshot_projects(workspaces):
    """Workspaces and projects that exist right now, as known-set entries."""
    out = set()
    for w in workspaces:
        out.add(("w", w["workspaceId"], ""))
        for r in rive_json("ls", "--workspace=%d" % w["workspaceId"]):
            if r.get("kind") == "project":
                out.add(("p", w["workspaceId"], r["name"]))
    return out


def included(rules, wid, segs):
    """Most specific rule wins (case-insensitive), same as the shell side."""
    cf = tuple(s.casefold() for s in segs)
    for i in range(len(cf), -1, -1):
        v = rules.get((wid, cf[:i]))
        if v is not None:
            return v
    return False


def load_patterns(path):
    pats = []
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                pats = [l.strip().casefold() for l in f if l.strip()]
        except OSError:
            pass
    return pats


def ignored(pats, segs):
    """Same rules as the shell ignore list: words are substrings, wildcards match a whole name,
    patterns with a slash match the whole path (workspace/project/folder/...)."""
    import fnmatch
    full = "/".join(s.casefold() for s in segs)
    for p in pats:
        if "/" in p:
            if fnmatch.fnmatchcase(full, p) or fnmatch.fnmatchcase(full, p + "/*") \
                    or fnmatch.fnmatchcase(full, "*/" + p) or fnmatch.fnmatchcase(full, "*/" + p + "/*"):
                return True
        elif any(c in p for c in "*?["):
            if any(fnmatch.fnmatchcase(s.casefold(), p) for s in segs):
                return True
        elif any(p in s.casefold() for s in segs):
            return True
    return False


def indexed_account(index_path):
    """Workspaces and projects from the saved listing (written by the engine), shaped like the
    answers from `rive`, so the check doesn't ask the account again."""
    with open(index_path, encoding="utf-8") as f:
        d = json.load(f)
    workspaces = [{"kind": "workspace", "workspaceId": int(w), "name": n} for w, n in d["workspaces"]]
    current = set()
    for w in workspaces:
        current.add(("w", w["workspaceId"], ""))
        for _pid, pname in d["projects"].get(str(w["workspaceId"]), {}).get("items", []):
            current.add(("p", w["workspaceId"], pname))
    return workspaces, current


def check_new(plan_file, patterns_file, report_only, index_path=None):
    """Compare the world now with `known`. Returns 10 if the selection was changed."""
    sel, names = read_selection()
    rules = {(w, tuple(s.casefold() for s in p)): v for (w, p), v in sel.items()}
    pats = load_patterns(patterns_file)
    try:
        workspaces, current = indexed_account(index_path)
    except (OSError, ValueError, KeyError, TypeError):
        workspaces = [r for r in rive_json("workspace") if r.get("kind") == "workspace"]
        current = snapshot_projects(workspaces)
    for w in workspaces:
        names[w["workspaceId"]] = w["name"]

    # current layout: workspaces + projects from the account, folders from the sync's plan
    folders_by_proj = {}
    with open(plan_file, encoding="utf-8") as f:
        for line in f:
            c = line.rstrip("\n").split("\t")
            if len(c) < 6:
                continue
            wid, pname, fpath = int(c[0]), c[2], ("" if c[4] == "." else c[4])
            fs = folders_by_proj.setdefault((wid, pname), set())
            segs = [s for s in fpath.split("/") if s]
            for i in range(1, len(segs) + 1):
                fs.add("/".join([pname] + segs[:i]))
    known = read_known()
    first_time = not known
    new_items = []   # (kind, wid, segs)
    baseline = set(current)
    for (wid, pname), fs in folders_by_proj.items():
        if ("b", wid, pname) not in known:
            baseline |= {("f", wid, x) for x in fs} | {("b", wid, pname)}
        else:
            for x in sorted(fs):
                if ("f", wid, x) not in known:
                    new_items.append(("f", wid, tuple(x.split("/"))))
    for kind, wid, path in sorted(current):
        if (kind, wid, path) in known:
            continue
        new_items.append((kind, wid, tuple(s for s in path.split("/") if s)))
    if first_time:
        new_items = []
    wname = lambda wid: names.get(wid) or str(wid)
    label = lambda it: " / ".join([wname(it[1])] + list(it[2]))
    kind_name = {"w": "workspace", "p": "project", "f": "folder"}

    ignored_items = [it for it in new_items if ignored(pats, [wname(it[1])] + list(it[2]))]
    covered = [it for it in new_items if it not in ignored_items and included(rules, it[1], it[2])]
    uncovered = [it for it in new_items if it not in ignored_items and it not in covered]

    # keep only the top-most uncovered items (a new project covers its own folders)
    def under(a, b):   # is a inside b?
        return a != b and a[1] == b[1] and a[2][:len(b[2])] == b[2] and (b[0] == "w" or a[0] != "w")
    top = [a for a in uncovered if not any(under(a, b) for b in uncovered)]
    top_set = set(top)
    uncovered_hidden = [a for a in uncovered if a not in top_set]

    if first_time:
        print("Recorded what exists now as the starting point; from the next sync on, "
              "anything new is flagged.")
    if covered:
        print("New and already covered by your selection (will be mirrored): %d" % len(covered))
    changed = False
    decided = set()
    if top:
        print("")
        print("New since your selection, and NOT covered by it (%d):" % len(top))
        for it in top[:25]:
            print("  %-9s %s" % (kind_name[it[0]], label(it)))
        if len(top) > 25:
            print("  ... and %d more" % (len(top) - 25))
        interactive = sys.stdin.isatty() and sys.stdout.isatty() and not report_only
        if not interactive:
            print("(Run rive-mirror in a terminal to add or dismiss these.)")
        else:
            print("")
            ans = input("[a] add all   [p] pick one by one   [d] dismiss all   [enter] decide later: ").strip().lower()
            if ans == "p":
                for it in top:
                    a = input("  %s %s: [y] add  [n] dismiss  [enter] later: " % (kind_name[it[0]], label(it))).strip().lower()
                    if a == "y":
                        sel[(it[1], it[2])] = True; changed = True; decided.add(it)
                    elif a == "n":
                        decided.add(it)
            elif ans == "a":
                for it in top:
                    sel[(it[1], it[2])] = True; decided.add(it)
                changed = True
            elif ans == "d":
                decided.update(top)
    if report_only:
        return 0
    # everything covered, ignored or decided now counts as seen; "later" items (and what is
    # inside them) stay new so they are flagged again next time
    later = [a for a in top if a not in decided]
    keep_new = {(k, w, "/".join(s)) for k, w, s in later}
    keep_new |= {(k, w, "/".join(s)) for k, w, s in uncovered_hidden
                 if any(under((k, w, s), b) for b in later)}
    seen_new = {(k, w, "/".join(s)) for k, w, s in new_items}
    write_known(known | ((baseline | seen_new) - keep_new))
    if changed:
        write_selection(sel, names)
        print("Selection updated.")
        return 10
    return 0


def cli():
    os.environ.setdefault("ESCDELAY", "25")   # make Esc respond quickly
    if len(sys.argv) > 1 and sys.argv[1] == "--list":
        sel, names = read_selection()
        if not sel:
            print("nothing selected yet. Choose with: rive-mirror select")
        for (wid, path), include in sorted(sel.items(), key=lambda e: (e[0][0], e[0][1])):
            print("%s %s / %s" % ("+" if include else "-", names.get(wid) or wid, "/".join(path) or "(whole workspace)"))
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        args = sys.argv[2:]
        plan = args[0]
        pats = args[args.index("--patterns") + 1] if "--patterns" in args else None
        index = args[args.index("--index") + 1] if "--index" in args else None
        try:
            return check_new(plan, pats, "--report-only" in args, index)
        except (RuntimeError, OSError) as e:
            print("could not check for new items: %s" % e, file=sys.stderr)
            return 0
    if len(sys.argv) > 1 and sys.argv[1] == "--clear":
        try:
            os.remove(SEL_FILE)
        except FileNotFoundError:
            pass
        print("selection cleared")
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("select needs a terminal", file=sys.stderr)
        return 2
    try:
        workspaces = [r for r in rive_json("workspace") if r.get("kind") == "workspace"]
    except (RuntimeError, OSError) as e:
        print("could not list workspaces: %s (are you signed in? try 'rive login')" % e, file=sys.stderr)
        return 3
    sel, names = read_selection()
    for w in workspaces:
        names[w["workspaceId"]] = w["name"]
    locale.setlocale(locale.LC_ALL, "")
    saved = curses.wrapper(main, workspaces, sel, names)
    if not saved:
        print("cancelled; selection unchanged")
        return 1
    write_selection(sel, names)
    try:
        write_known(read_known() | snapshot_projects(workspaces))   # you've now seen these
    except (RuntimeError, OSError):
        pass   # not fatal: the first sync records a starting point instead
    inc = sum(1 for v in sel.values() if v)
    print("saved %d ticked, %d unticked to %s" % (inc, len(sel) - inc, SEL_FILE))
    return 0


if __name__ == "__main__":
    sys.exit(cli())
