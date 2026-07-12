#!/usr/bin/env python3
"""klibs.resolver -- footprint id / board -> 3D model path resolver.

Lives beside the pinned KiCad libraries it indexes. Consumed by wyred-kicad
(rung 1) and, later, wyred-3d (rung 2) as a plain importable library plus a
small CLI -- no wyred-workspace imports, no adapter-to-adapter coupling.

Two modes share one targeted s-expr scan (no full parser; pcb_extract spirit):

  1. footprint-id mode:  "Resistor_SMD:R_0402_1005Metric"
       -> view/footprints/Resistor_SMD.pretty/R_0402_1005Metric.kicad_mod
       -> that footprint's (model ...) entries, each classified.
  2. board mode:  a .kicad_pcb  ->  every embedded (model ...) ref, classified
       (boards carry footprint instances inline; no library needed).

Path normalization expands ${KISYS3DMOD} / ${KICAD5..10_3DMODEL_DIR} to the
packages3D view root and ${KIPRJMOD} to the board's own directory. Each ref is
classified against the pinned packages3D tree + the materialized view:

  resolved        exact model file present in the view
  fetchable       exact file exists at the pinned packages3D SHA (feeds
                  materialize.py)
  fetchable_twin  exact file absent at the SHA but its .step twin exists
                  (e.g. a .wrl board ref -- packages3D 10.0.x ships .step only)
  confessed_missing  ${KIPRJMOD}-relative, or an official-var ref whose file
                  AND .step twin are both absent at the SHA (non-official libs
                  such as LibreSolar.3dshapes) -- never silently dropped.

`exists` on each record is the physical presence of the exact expanded path in
the model dir under test (the view, or a caller-supplied dir). It is the field
the board-mode differential test compares against kicad-cli's File-not-found
set: two independent engines must agree on the same set.

Pure Python 3 stdlib + git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
MANIFEST_PATH = os.path.join(REPO, "manifest.json")

_MODEL_LINE = re.compile(r"\(\s*model\b")
_VAR = re.compile(r"^\$\{([^}]+)\}[/\\]?(.*)$", re.DOTALL)


def load_manifest(path: str = MANIFEST_PATH) -> dict:
    with open(path) as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# targeted s-expr scan
# ---------------------------------------------------------------------------

def extract_model_refs(text: str) -> list[str]:
    """Every (model ...) path in file order (with duplicates preserved).

    Handles both KiCad quoting styles:
      (model "${KICAD6_3DMODEL_DIR}/Foo.3dshapes/Bar.wrl" ...)   quoted (KiCad 6+)
      (model ${KISYS3DMOD}/Foo.3dshapes/Bar.step               unquoted (KiCad 5)
    Unquoted paths never contain whitespace (KiCad requires quoting otherwise),
    so the token ends at the first whitespace; quoted paths keep spaces/parens.
    """
    refs: list[str] = []
    for line in text.splitlines():
        m = _MODEL_LINE.search(line)
        if not m:
            continue
        rest = line[m.end():].lstrip()
        if not rest:
            continue
        if rest[0] == '"':
            end = rest.find('"', 1)
            if end == -1:
                continue
            refs.append(rest[1:end])
        else:
            refs.append(rest.split()[0].rstrip(")"))
    return refs


def expand_ref(ref: str, model_dir_vars, project_var: str):
    """Return (kind, var, rel) for a raw model ref.

    kind in {"model_dir", "project", "other"}; rel is forward-slashed and
    relative to the resolved root (or the raw path for "other").

    A bare *relative* path (no ${var}, not absolute) is a model_dir ref:
    KiCad resolves relative model paths against the 3D-model search path
    (the *_3DMODEL_DIR / KISYS3DMOD dirs), e.g. KiCad-5 boards that write
    `(model Capacitor_SMD.3dshapes/C_0603_1608Metric.step` with no prefix.
    """
    m = _VAR.match(ref)
    if m:
        var, rel = m.group(1), m.group(2).replace("\\", "/")
        if var in model_dir_vars:
            return "model_dir", var, rel
        if var == project_var:
            return "project", var, rel
        return "other", var, rel
    norm = ref.replace("\\", "/")
    if not os.path.isabs(norm) and not norm.startswith("$"):
        return "model_dir", None, norm
    return "other", None, norm


def step_twin(rel: str) -> str:
    """The canonical packages3D geometry path for a model ref: <base>.step.

    packages3D 10.0.x stores geometry as lowercase `.step` only, so any ref
    extension (.wrl/.stp/.STP/.STEP/.step) maps to `<base>.step`; a ref that is
    already `<base>.step` maps to itself.
    """
    return os.path.splitext(rel)[0] + ".step"


# ---------------------------------------------------------------------------
# pinned-tree lookups (offline once the blob:none partial clone exists)
# ---------------------------------------------------------------------------

def _git_blob_exists(repo: str, sha: str, relpath: str) -> bool:
    """True iff relpath exists in the tree at sha. Uses rev-parse (reads the
    tree only) rather than `cat-file -e`, which would fault-in the blob from
    the promisor remote and defeat the blob:none partial clone."""
    if not repo or not os.path.isdir(os.path.join(repo, ".git")):
        return False
    res = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", "--quiet",
         "%s:%s" % (sha, relpath)],
        capture_output=True, text=True)
    return res.returncode == 0


class Resolver:
    def __init__(self, manifest: dict | None = None, repo: str = REPO):
        self.repo = repo
        self.manifest = manifest or load_manifest(os.path.join(repo, "manifest.json"))
        rcfg = self.manifest["resolver"]
        self.model_dir_vars = set(rcfg["model_dir_vars"])
        self.project_var = rcfg["project_var"]
        self.view3d = os.path.join(repo, "view", rcfg["packages3d_view_root"])
        self.footprints_view = os.path.join(repo, "view", rcfg["footprints_view_root"])
        p3d = self.manifest["libraries"]["packages3d"]
        self.p3d_repo = os.path.join(repo, p3d["submodule"])
        self.p3d_sha = p3d["pinned_sha"]

    # -- classification -----------------------------------------------------

    def classify_ref(self, ref: str, *, model_dir: str | None = None,
                     pcb_dir: str | None = None) -> dict:
        """Classify one raw model ref.

        model_dir: the directory the *_3DMODEL_DIR vars expand to for the
        `exists` check (defaults to the packages3D view). Pass an alternate
        dir (e.g. an empty scratch dir) to mirror a specific kicad-cli env.
        """
        m3d = self.view3d if model_dir is None else model_dir
        kind, var, rel = expand_ref(ref, self.model_dir_vars, self.project_var)
        rec = {"ref": ref, "kind": kind, "var": var, "rel": rel,
               "exists": False, "at_sha": None, "twin_at_sha": None,
               "klass": "confessed_missing"}
        if kind == "model_dir":
            exact = os.path.join(m3d, rel)
            rec["exists"] = os.path.isfile(exact)
            at_sha = _git_blob_exists(self.p3d_repo, self.p3d_sha, rel)
            twin = step_twin(rel)
            twin_at_sha = at_sha if twin == rel else _git_blob_exists(
                self.p3d_repo, self.p3d_sha, twin)
            rec["at_sha"], rec["twin_at_sha"] = at_sha, twin_at_sha
            in_view = os.path.isfile(os.path.join(self.view3d, rel))
            if in_view:
                rec["klass"] = "resolved"
            elif at_sha:
                rec["klass"] = "fetchable"
            elif twin_at_sha:
                rec["klass"] = "fetchable_twin"
            else:
                rec["klass"] = "confessed_missing"
        elif kind == "project":
            if pcb_dir is not None:
                rec["exists"] = os.path.isfile(os.path.join(pcb_dir, rel))
            rec["klass"] = "confessed_missing"
        else:
            # unknown var or absolute path: klibs cannot provide it
            if os.path.isabs(ref):
                rec["exists"] = os.path.isfile(ref)
            rec["klass"] = "confessed_missing"
        return rec

    # -- modes --------------------------------------------------------------

    def board(self, pcb_path: str, *, model_dir: str | None = None) -> dict:
        with open(pcb_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        pcb_dir = os.path.dirname(os.path.abspath(pcb_path))
        seen: dict[str, dict] = {}
        for ref in extract_model_refs(text):
            if ref not in seen:
                seen[ref] = self.classify_ref(
                    ref, model_dir=model_dir, pcb_dir=pcb_dir)
        records = list(seen.values())
        return {"mode": "board", "board": os.path.abspath(pcb_path),
                "records": records, "summary": _summarize(records)}

    def footprint_id(self, fpid: str, *, model_dir: str | None = None) -> dict:
        if ":" not in fpid:
            raise ValueError("footprint id must be 'Library:Footprint', got %r" % fpid)
        lib, name = fpid.split(":", 1)
        fp_path = os.path.join(self.footprints_view, lib + ".pretty", name + ".kicad_mod")
        if not os.path.isfile(fp_path):
            raise FileNotFoundError(
                "footprint not found in view: %s (materialize footprints first?)" % fp_path)
        with open(fp_path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
        seen: dict[str, dict] = {}
        for ref in extract_model_refs(text):
            if ref not in seen:
                seen[ref] = self.classify_ref(ref, model_dir=model_dir, pcb_dir=None)
        records = list(seen.values())
        return {"mode": "footprint", "footprint": fpid, "footprint_file": fp_path,
                "records": records, "summary": _summarize(records)}

    # -- materialize feed ---------------------------------------------------

    def fetch_targets(self, records) -> list[str]:
        """packages3D-relative paths to materialize for a set of records:
        the exact file when fetchable, else the .step twin when fetchable_twin.
        Deduplicated, sorted. confessed_missing refs contribute nothing."""
        out: set[str] = set()
        for r in records:
            if r["klass"] == "fetchable":
                out.add(r["rel"])
            elif r["klass"] == "fetchable_twin":
                out.add(step_twin(r["rel"]))
        return sorted(out)


def _summarize(records) -> dict:
    s = {"total": len(records), "resolved": 0, "fetchable": 0,
         "fetchable_twin": 0, "confessed_missing": 0, "missing_exact": 0}
    for r in records:
        s[r["klass"]] += 1
        if not r["exists"]:
            s["missing_exact"] += 1
    return s


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="klibs.resolver",
        description="Resolve footprint/board 3D model refs against pinned KiCad libs.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--board", metavar="PCB", help="a .kicad_pcb file (board mode)")
    g.add_argument("--footprint", metavar="LIB:NAME", help="footprint id (footprint mode)")
    ap.add_argument("--model-dir", metavar="DIR",
                    help="override the dir *_3DMODEL_DIR vars expand to for the "
                         "`exists` check (default: the packages3D view)")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--fetch-list", action="store_true",
                    help="print only the packages3D-relative paths to materialize")
    args = ap.parse_args(argv)

    r = Resolver()
    if args.board:
        out = r.board(args.board, model_dir=args.model_dir)
    else:
        out = r.footprint_id(args.footprint, model_dir=args.model_dir)

    if args.fetch_list:
        for p in r.fetch_targets(out["records"]):
            print(p)
        return 0
    if args.json:
        json.dump(out, sys.stdout, indent=2, sort_keys=True)
        print()
        return 0
    # human summary
    s = out["summary"]
    print("mode=%s  refs=%d  resolved=%d fetchable=%d fetchable_twin=%d "
          "confessed_missing=%d  (missing_exact=%d)"
          % (out["mode"], s["total"], s["resolved"], s["fetchable"],
             s["fetchable_twin"], s["confessed_missing"], s["missing_exact"]))
    for rec in out["records"]:
        print("  [%-15s] %s" % (rec["klass"], rec["ref"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
