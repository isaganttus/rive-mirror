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
"""
import hashlib
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor


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


# ---- command line ---------------------------------------------------------

def cmd_state(stamps):
    rows = [l.rstrip("\n").split("\t") for l in sys.stdin if l.strip()]
    rows = [r for r in rows if len(r) >= 3]
    with ThreadPoolExecutor(max_workers=8) as ex:
        states = list(ex.map(lambda r: folder_state(r[1], os.path.join(stamps, r[0])), rows))
    for st, r in zip(states, rows):
        print("%s\t%s\t%s\t%s" % (st, r[0], r[1], r[2]))


def cli(argv):
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
