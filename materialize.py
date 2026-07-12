#!/usr/bin/env python3
"""Materialize the pinned KiCad libraries into view/ (wyred-klibs).

view/ is a gitignored, on-demand build; manifest.json is the single source of
truth for the pin (each library's commit SHA + tree SHA at the 10.0.4 train).
Nothing multi-GB is ever transferred: symbols/footprints are wholesale trees
extracted with `git archive` at the pinned SHA; packages3D is a *blob-filtered*
partial clone (--filter=blob:none) from which only the requested model blobs
are lazily fetched.

Integrity rides on git content addressing -- there are no per-file sha256
lists. --check verifies each library repo is at the pinned tree and spot-checks
(symbols/footprints) or fully checks (packages3D) view files against the git
blob ids at the pinned SHA.

Usage:
    python3 materialize.py                     # symbols + footprints wholesale;
                                               #   ensure packages3D partial clone
    python3 materialize.py --lib symbols       # one wholesale library
    python3 materialize.py --3d-from-board PCB [PCB ...]
                                               # resolve boards' model refs and
                                               #   materialize their .step twins
    python3 materialize.py --3d-refs-file FILE # one raw (model ...) ref per line
    python3 materialize.py --check             # verify view/ against the pins
    python3 materialize.py --force             # rebuild even if up to date

Pure Python 3 stdlib + git. Idempotent: an up-to-date wholesale tree or an
already-present, blob-verified model file is skipped, and a second run performs
no network access.

STALE-FORK / TWIN / SYMBOLS-LAYOUT discrepancies against the plan's pre-10.0
assumptions are recorded in manifest.json "notes" and README.md. In short:
fetches go to GitLab (`upstream`) because the owebeeone GitHub forks are frozen
at 5.1.7; packages3D 10.0.x ships .step only (no .wrl twins), so a .wrl board
ref materializes its .step twin and the .wrl is confessed absent-upstream.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time

REPO = os.path.dirname(os.path.abspath(__file__))
MANIFEST_PATH = os.path.join(REPO, "manifest.json")
VIEW = os.path.join(REPO, "view")

sys.path.insert(0, REPO)
from klibs import resolver as _resolver  # noqa: E402


class MaterializeError(Exception):
    pass


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def load_manifest() -> dict:
    with open(MANIFEST_PATH) as fh:
        return json.load(fh)


def run_git(args, cwd, check=True, capture=True):
    res = subprocess.run(["git"] + args, cwd=cwd, check=False,
                         capture_output=capture, text=True)
    if check and res.returncode != 0:
        raise MaterializeError(
            "git %s (in %s) failed with exit %d:\n%s"
            % (" ".join(args), cwd, res.returncode, (res.stderr or "").strip()))
    return res


def git_out(args, cwd) -> str:
    return run_git(args, cwd).stdout.strip()


def have_commit(repo: str, sha: str) -> bool:
    return subprocess.run(["git", "-C", repo, "cat-file", "-e", sha + "^{commit}"],
                          capture_output=True).returncode == 0


def write_bytes(dest: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)


def fetch_urls(entry: dict) -> list[str]:
    order = entry.get("fetch_order") or ["upstream"]
    urls = []
    for key in order:
        u = entry.get(key)
        if u:
            urls.append(u)
    if not urls:
        urls = [entry["upstream"]]
    return urls


# ---------------------------------------------------------------------------
# submodule repo setup + pin fetch
# ---------------------------------------------------------------------------

def ensure_repo(entry: dict, *, partial: bool) -> str:
    """Ensure the library's submodule dir is a git repo with origin set."""
    sub = os.path.join(REPO, entry["submodule"])
    if not os.path.isdir(os.path.join(sub, ".git")):
        os.makedirs(sub, exist_ok=True)
        run_git(["init", "-q"], cwd=sub)
        run_git(["remote", "add", "origin", entry["upstream"]], cwd=sub)
        if partial:
            run_git(["config", "remote.origin.promisor", "true"], cwd=sub)
            run_git(["config", "remote.origin.partialclonefilter", "blob:none"], cwd=sub)
    return sub


def ensure_pin(entry: dict, *, partial: bool) -> str:
    """Ensure the pinned commit is present locally and matches pinned_tree.
    Returns the submodule repo path. Fetches from `upstream` (GitLab) if
    absent; verifies the mutable tag deref equals the immutable pinned SHA."""
    sub = ensure_repo(entry, partial=partial)
    sha, tag = entry["pinned_sha"], entry["pinned_tag"]
    if not have_commit(sub, sha):
        tagref = "refs/tags/%s" % tag
        fetch = ["fetch", "-q", "--depth", "1"]
        if partial:
            fetch += ["--filter=blob:none"]
        last = None
        for url in fetch_urls(entry):
            r = run_git(fetch + [url, "%s:%s" % (tagref, tagref)], cwd=sub, check=False)
            if r.returncode == 0 and have_commit(sub, sha):
                last = None
                break
            last = r.stderr
        if not have_commit(sub, sha):
            raise MaterializeError(
                "could not fetch pinned commit %s (tag %s) for %s from %s:\n%s"
                % (sha[:12], tag, entry["submodule"], fetch_urls(entry), (last or "").strip()))
    got_tree = git_out(["rev-parse", sha + "^{tree}"], cwd=sub)
    if got_tree != entry["pinned_tree"]:
        raise MaterializeError(
            "%s: pinned tree mismatch (pin %s, got %s) -- the pinned SHA does "
            "not resolve to the recorded tree" % (entry["submodule"],
                                                  entry["pinned_tree"], got_tree))
    return sub


# ---------------------------------------------------------------------------
# git archive extraction
# ---------------------------------------------------------------------------

def archive_extract(sub: str, sha: str, dest_root: str, pathspecs=None):
    """Stream `git archive sha [-- pathspecs]` into dest_root. With a partial
    clone this lazily fetches exactly the blobs the pathspecs touch."""
    cmd = ["git", "-C", sub, "archive", sha]
    if pathspecs is not None:
        cmd += ["--"] + [":(literal)%s" % p for p in pathspecs]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    count = 0
    with tarfile.open(fileobj=proc.stdout, mode="r|") as tf:
        for member in tf:
            if not member.isfile():
                continue
            data = tf.extractfile(member).read()
            write_bytes(os.path.join(dest_root, member.name), data)
            count += 1
    rc = proc.wait()
    if rc != 0:
        raise MaterializeError("git archive %s failed in %s (exit %d)" % (sha[:12], sub, rc))
    return count


def blob_id(sub: str, sha: str, relpath: str) -> str | None:
    """Blob OID for a path at the pinned SHA. rev-parse reads the tree only --
    it never faults-in the blob, so classification stays offline."""
    r = run_git(["rev-parse", "--verify", "--quiet", "%s:%s" % (sha, relpath)],
                cwd=sub, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def sparse_extract(sub: str, blob_map: dict, dest_root: str) -> None:
    """Fault-in and write exactly the given {relpath: oid} blobs.

    On a blob:none partial clone `git archive` faults-in the WHOLE tree's
    blobs; `git cat-file --batch` faults-in only the requested OIDs (each via
    the promisor remote), so the transfer is exactly the wanted models. The
    OIDs are resolved offline by the caller via rev-parse.
    """
    if not blob_map:
        return
    items = list(blob_map.items())
    proc = subprocess.Popen(
        ["git", "-C", sub, "cat-file", "--batch"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        for rel, oid in items:
            proc.stdin.write((oid + "\n").encode())
            proc.stdin.flush()
            header = proc.stdout.readline().decode().strip()
            parts = header.split()
            if len(parts) != 3 or parts[1] != "blob":
                raise MaterializeError("cat-file --batch unexpected header for %s: %r"
                                       % (rel, header))
            size = int(parts[2])
            data = _read_exact(proc.stdout, size)
            proc.stdout.read(1)  # trailing newline
            write_bytes(os.path.join(dest_root, rel), data)
    finally:
        proc.stdin.close()
        proc.wait()


def _read_exact(stream, n: int) -> bytes:
    chunks = []
    while n > 0:
        b = stream.read(n)
        if not b:
            raise MaterializeError("cat-file --batch: short read (%d left)" % n)
        chunks.append(b)
        n -= len(b)
    return b"".join(chunks)


def file_blob_id(sub: str, path: str) -> str:
    return git_out(["hash-object", path], cwd=sub)


def read_marker(view_root: str) -> dict | None:
    p = os.path.join(view_root, ".klibs_pin.json")
    if os.path.isfile(p):
        try:
            with open(p) as fh:
                return json.load(fh)
        except Exception:
            return None
    return None


def write_marker(view_root: str, entry: dict, extra: dict | None = None) -> None:
    rec = {"sha": entry["pinned_sha"], "tree": entry["pinned_tree"],
           "tag": entry["pinned_tag"], "materialized_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if extra:
        rec.update(extra)
    with open(os.path.join(view_root, ".klibs_pin.json"), "w") as fh:
        json.dump(rec, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------------------
# wholesale (symbols, footprints)
# ---------------------------------------------------------------------------

def materialize_wholesale(name: str, entry: dict, force: bool) -> None:
    view_root = os.path.join(VIEW, entry["view_root"])
    marker = read_marker(view_root)
    if not force and marker and marker.get("sha") == entry["pinned_sha"] \
            and os.path.isdir(view_root):
        # offline up-to-date check
        if have_commit(os.path.join(REPO, entry["submodule"]), entry["pinned_sha"]):
            print("[%s] up to date (pin %s) -- skipped (no network)"
                  % (name, entry["pinned_sha"][:12]))
            return
    sub = ensure_pin(entry, partial=False)
    print("[%s] materializing wholesale tree %s ..." % (name, entry["pinned_sha"][:12]))
    tmp = view_root + ".materialize.tmp"
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    try:
        n = archive_extract(sub, entry["pinned_sha"], tmp)
        # ensure the upstream LICENSE.md landed at the view root
        if not os.path.isfile(os.path.join(tmp, entry.get("license_file", "LICENSE.md"))):
            raise MaterializeError("[%s] upstream %s missing from extracted tree"
                                   % (name, entry.get("license_file", "LICENSE.md")))
        if os.path.isdir(view_root):
            shutil.rmtree(view_root)
        os.replace(tmp, view_root)
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    write_marker(view_root, entry, {"files": n})
    size = _dir_size(view_root)
    print("[%s] OK -- %d files, %.1f MB, tree %s"
          % (name, n, size / 1e6, entry["pinned_tree"][:12]))


# ---------------------------------------------------------------------------
# sparse packages3D
# ---------------------------------------------------------------------------

def materialize_packages3d(entry: dict, targets: list[str], force: bool) -> dict:
    """Fetch the given packages3D-relative paths (+ LICENSE.md) into the view.
    Idempotent & verifying: an already-present file whose bytes match the
    pinned blob is skipped. Returns a report dict."""
    view_root = os.path.join(VIEW, entry["view_root"])
    sub = ensure_pin(entry, partial=True)
    sha = entry["pinned_sha"]
    license_file = entry.get("license_file", "LICENSE.md")

    wanted = list(dict.fromkeys(targets))
    fetched, skipped, absent = [], [], []
    need_archive = []

    # LICENSE.md is always part of the view root
    for rel in [license_file] + wanted:
        want_id = blob_id(sub, sha, rel)
        if want_id is None:
            if rel != license_file:
                absent.append(rel)
            else:
                raise MaterializeError("packages3D LICENSE.md absent at pin %s" % sha[:12])
            continue
        dest = os.path.join(view_root, rel)
        if not force and os.path.isfile(dest) and file_blob_id(sub, dest) == want_id:
            skipped.append(rel)
        else:
            need_archive.append(rel)

    if need_archive:
        print("[packages3d] fetching %d model file(s) (blob-filtered, per-blob) ..."
              % len(need_archive))
        blob_map = {rel: blob_id(sub, sha, rel) for rel in need_archive}
        sparse_extract(sub, blob_map, view_root)
        # verify what we just wrote against the pinned blobs
        for rel in need_archive:
            dest = os.path.join(view_root, rel)
            if not os.path.isfile(dest) or file_blob_id(sub, dest) != blob_map[rel]:
                raise MaterializeError("packages3D post-fetch verify FAILED: %s" % rel)
            fetched.append(rel)
    else:
        print("[packages3d] all requested files already present & verified -- no network")

    write_marker(view_root, entry, {"model_files": len(_list_model_files(view_root))})
    size = _dir_size(view_root)
    report = {"requested": len(wanted), "fetched": len(fetched),
              "skipped": len(skipped), "confessed_missing": sorted(absent),
              "view_bytes": size}
    print("[packages3d] OK -- fetched=%d skipped=%d confessed_missing=%d  view=%.2f MB"
          % (len(fetched), len(skipped), len(absent), size / 1e6))
    if absent:
        print("[packages3d] confessed-missing (not in official packages3D at pin):")
        for a in sorted(absent):
            print("    %s" % a)
    return report


def _list_model_files(view_root: str) -> list[str]:
    out = []
    for dp, _dn, fn in os.walk(view_root):
        for f in fn:
            if f.endswith(".step") or f.endswith(".wrl") or f.endswith(".stp"):
                out.append(os.path.join(dp, f))
    return out


def _dir_size(path: str) -> int:
    total = 0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
            except OSError:
                pass
    return total


def targets_from_boards(boards: list[str]) -> tuple[list[str], list[dict]]:
    r = _resolver.Resolver()
    all_targets: set[str] = set()
    per_board = []
    for pcb in boards:
        out = r.board(pcb)
        tgts = r.fetch_targets(out["records"])
        all_targets.update(tgts)
        per_board.append({"board": pcb, "summary": out["summary"], "targets": len(tgts)})
    return sorted(all_targets), per_board


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------

SPOT_FILES = {
    "symbols": ["LICENSE.md", "Device.kicad_symdir/R.kicad_sym"],
    "footprints": ["LICENSE.md", "Resistor_SMD.pretty/R_0402_1005Metric.kicad_mod"],
}


def check_wholesale(name: str, entry: dict) -> list[str]:
    problems = []
    view_root = os.path.join(VIEW, entry["view_root"])
    if not os.path.isdir(view_root):
        return ["%s: view not materialized" % name]
    sub = os.path.join(REPO, entry["submodule"])
    if not have_commit(sub, entry["pinned_sha"]):
        return ["%s: submodule repo missing the pinned commit (run materialize)" % name]
    got_tree = git_out(["rev-parse", entry["pinned_sha"] + "^{tree}"], cwd=sub)
    if got_tree != entry["pinned_tree"]:
        problems.append("%s: repo tree %s != pinned %s" % (name, got_tree, entry["pinned_tree"]))
    for rel in SPOT_FILES.get(name, ["LICENSE.md"]):
        p = os.path.join(view_root, rel)
        if not os.path.isfile(p):
            problems.append("%s: spot file missing: %s" % (name, rel))
            continue
        if file_blob_id(sub, p) != blob_id(sub, entry["pinned_sha"], rel):
            problems.append("%s: spot file blob mismatch: %s" % (name, rel))
    return problems


def check_packages3d(entry: dict) -> list[str]:
    problems = []
    view_root = os.path.join(VIEW, entry["view_root"])
    if not os.path.isdir(view_root):
        # packages3D is demand-driven: an absent view (no board materialized
        # yet) is a valid state, not a verification failure.
        print("[packages3d] (no models materialized yet -- nothing to verify)")
        return []
    sub = os.path.join(REPO, entry["submodule"])
    if not have_commit(sub, entry["pinned_sha"]):
        return ["packages3d: partial clone missing the pinned commit (run materialize)"]
    got_tree = git_out(["rev-parse", entry["pinned_sha"] + "^{tree}"], cwd=sub)
    if got_tree != entry["pinned_tree"]:
        problems.append("packages3d: repo tree %s != pinned %s" % (got_tree, entry["pinned_tree"]))
    sha = entry["pinned_sha"]
    for path in [os.path.join(view_root, entry.get("license_file", "LICENSE.md"))] \
            + _list_model_files(view_root):
        rel = os.path.relpath(path, view_root)
        want = blob_id(sub, sha, rel)
        if want is None:
            problems.append("packages3d: view file not at pin: %s" % rel)
        elif file_blob_id(sub, path) != want:
            problems.append("packages3d: view file blob mismatch: %s" % rel)
    return problems


def do_check(manifest: dict) -> int:
    libs = manifest["libraries"]
    problems = []
    for name in ("symbols", "footprints"):
        probs = check_wholesale(name, libs[name])
        print("[%s] %s" % (name, "OK" if not probs else "%d problem(s)" % len(probs)))
        problems += probs
    probs = check_packages3d(libs["packages3d"])
    print("[packages3d] %s" % ("OK" if not probs else "%d problem(s)" % len(probs)))
    problems += probs
    for p in problems[:40]:
        print("  " + p)
    print("CHECK: %s" % ("PASS" if not problems else "FAIL (%d problems)" % len(problems)))
    return 0 if not problems else 1


# ---------------------------------------------------------------------------
# driver
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="materialize", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lib", choices=["symbols", "footprints", "packages3d"],
                    help="materialize only this library")
    ap.add_argument("--3d-from-board", dest="boards", nargs="+", metavar="PCB",
                    help="resolve these boards' model refs and materialize their .step twins")
    ap.add_argument("--3d-refs-file", dest="refs_file", metavar="FILE",
                    help="materialize packages3D from raw (model ...) refs, one per line")
    ap.add_argument("--check", action="store_true", help="verify view/ against the pins")
    ap.add_argument("--force", action="store_true", help="rebuild even if up to date")
    args = ap.parse_args(argv)

    manifest = load_manifest()
    libs = manifest["libraries"]

    if args.check:
        return do_check(manifest)

    try:
        if args.boards or args.refs_file:
            # the resolver classifies fetchable vs confessed_missing against the
            # pinned packages3D tree, so the blob:none partial clone (trees) must
            # exist before we resolve.
            ensure_pin(libs["packages3d"], partial=True)
            if args.refs_file:
                with open(args.refs_file) as fh:
                    raw = [ln.strip() for ln in fh if ln.strip() and not ln.startswith("#")]
                r = _resolver.Resolver()
                recs = [r.classify_ref(x) for x in dict.fromkeys(raw)]
                targets = r.fetch_targets(recs)
                per_board = None
            else:
                targets, per_board = targets_from_boards(args.boards)
            if per_board:
                for pb in per_board:
                    print("[board] %s  targets=%d  %s" % (pb["board"], pb["targets"], pb["summary"]))
            materialize_packages3d(libs["packages3d"], targets, args.force)
            return 0

        if args.lib == "packages3d":
            print("packages3d needs a model set: use --3d-from-board PCB or --3d-refs-file FILE")
            return 2

        selected = [args.lib] if args.lib else ["symbols", "footprints"]
        for name in selected:
            materialize_wholesale(name, libs[name], args.force)
        if not args.lib:
            # ensure the packages3D partial clone (tree only) exists so the
            # resolver can classify fetchable vs confessed-missing offline
            ensure_pin(libs["packages3d"], partial=True)
            print("[packages3d] partial clone ready (blob-filtered; no model blobs fetched). "
                  "Use --3d-from-board to materialize models.")
    except MaterializeError as exc:
        print("MATERIALIZE: FAIL\n%s" % exc, file=sys.stderr)
        return 1
    print("MATERIALIZE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
