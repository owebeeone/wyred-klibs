#!/usr/bin/env python3
"""Tests for klibs.resolver.

Pure-logic unit tests (extraction, path expansion, twin, resolved/confessed
classification) run with no network and no kicad-cli. Integration tests are
gated on prerequisites and skip -- loudly -- when absent:
  * classification of fetchable / fetchable_twin / confessed_missing needs the
    packages3D blob-filtered partial clone (materialize.py ensures it);
  * the board-mode differential and footprint-id mode need the wyred-t9y
    watchy board and (differential) kicad-cli.

The differential test is the house-style "two independent engines, one
disagreement code": the resolver's board-mode missing set (refs whose exact
expanded path is absent from a controlled model dir) must EQUAL the set of
paths kicad-cli reports as `File not found` when its *_3DMODEL_DIR vars point
at that same dir. Run against an empty model dir so the comparison is
deterministic and network-free: both engines then flag the board's complete
model-ref set.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)

from klibs import resolver as R  # noqa: E402

WATCHY = os.environ.get("WATCHY_PCB") or os.path.join(
    REPO, "..", "wyred-t9y", "view", "watchy", "Watchy.kicad_pcb")
WATCHY = os.path.abspath(WATCHY)


def find_kicad_cli():
    for cand in [os.environ.get("KICAD_CLI"), "/opt/homebrew/bin/kicad-cli",
                 shutil.which("kicad-cli")]:
        if cand and os.path.exists(cand):
            return cand
    return None


def p3d_clone_ready() -> bool:
    m = R.load_manifest()
    sub = os.path.join(REPO, m["libraries"]["packages3d"]["submodule"])
    return os.path.isdir(os.path.join(sub, ".git")) and subprocess.run(
        ["git", "-C", sub, "cat-file", "-e",
         m["libraries"]["packages3d"]["pinned_sha"] + "^{commit}"],
        capture_output=True).returncode == 0


# ---------------------------------------------------------------------------
# pure-logic unit tests
# ---------------------------------------------------------------------------

class TestExtraction(unittest.TestCase):
    def test_quoted_kicad6(self):
        text = '  (model "${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.wrl"\n    (offset (xyz 0 0 0))\n  )\n'
        self.assertEqual(R.extract_model_refs(text),
                         ["${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.wrl"])

    def test_unquoted_kicad5(self):
        text = "    (model ${KISYS3DMOD}/Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step\n"
        self.assertEqual(R.extract_model_refs(text),
                         ["${KISYS3DMOD}/Package_SO.3dshapes/SOIC-8_3.9x4.9mm_P1.27mm.step"])

    def test_quoted_with_spaces_and_parens(self):
        text = '    (model "${KIPRJMOD}/3D/JST - SMD (R) - 2Pin - 1.25mm.step"\n'
        self.assertEqual(R.extract_model_refs(text),
                         ["${KIPRJMOD}/3D/JST - SMD (R) - 2Pin - 1.25mm.step"])

    def test_order_and_duplicates_preserved(self):
        text = ('(model "a.wrl")\n(model "b.step")\n(model "a.wrl")\n')
        self.assertEqual(R.extract_model_refs(text), ["a.wrl", "b.step", "a.wrl"])


class TestExpansion(unittest.TestCase):
    def setUp(self):
        m = R.load_manifest()
        self.vars = set(m["resolver"]["model_dir_vars"])
        self.prj = m["resolver"]["project_var"]

    def test_model_dir_var(self):
        k, v, rel = R.expand_ref("${KICAD6_3DMODEL_DIR}/Lib.3dshapes/M.step", self.vars, self.prj)
        self.assertEqual((k, v, rel), ("model_dir", "KICAD6_3DMODEL_DIR", "Lib.3dshapes/M.step"))

    def test_kisys3dmod(self):
        k, v, rel = R.expand_ref("${KISYS3DMOD}/Lib.3dshapes/M.wrl", self.vars, self.prj)
        self.assertEqual((k, v), ("model_dir", "KISYS3DMOD"))

    def test_kiprjmod(self):
        k, v, rel = R.expand_ref("${KIPRJMOD}/3D/M.step", self.vars, self.prj)
        self.assertEqual((k, v, rel), ("project", "KIPRJMOD", "3D/M.step"))

    def test_absolute(self):
        k, v, rel = R.expand_ref("/opt/models/M.step", self.vars, self.prj)
        self.assertEqual(k, "other")

    def test_bare_relative_is_model_dir(self):
        # KiCad-5 boards may write a relative model path with no ${var} prefix;
        # KiCad resolves it against the 3D-model search path.
        k, v, rel = R.expand_ref("Capacitor_SMD.3dshapes/C_0603_1608Metric.step",
                                 self.vars, self.prj)
        self.assertEqual((k, v, rel),
                         ("model_dir", None, "Capacitor_SMD.3dshapes/C_0603_1608Metric.step"))

    def test_step_twin(self):
        self.assertEqual(R.step_twin("Lib.3dshapes/M.wrl"), "Lib.3dshapes/M.step")
        self.assertEqual(R.step_twin("Lib.3dshapes/M.step"), "Lib.3dshapes/M.step")
        self.assertEqual(R.step_twin("Lib.3dshapes/M.STP"), "Lib.3dshapes/M.step")


def _resolver_with_view(tmp, files):
    """Build a throwaway Resolver rooted at tmp with a manifest + view files."""
    m = R.load_manifest()
    # point packages3d submodule at a non-existent dir so at_sha lookups are None
    m["libraries"]["packages3d"]["submodule"] = "no_such_repo"
    with open(os.path.join(tmp, "manifest.json"), "w") as fh:
        json.dump(m, fh)
    for rel in files:
        p = os.path.join(tmp, "view", "3dmodels", rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").close()
    return R.Resolver(manifest=m, repo=tmp)


class TestClassifyNoNetwork(unittest.TestCase):
    def test_resolved_when_in_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = _resolver_with_view(tmp, ["Resistor_SMD.3dshapes/R_0402_1005Metric.step"])
            rec = res.classify_ref("${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.step")
            self.assertEqual(rec["klass"], "resolved")
            self.assertTrue(rec["exists"])

    def test_kiprjmod_is_confessed_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = _resolver_with_view(tmp, [])
            rec = res.classify_ref("${KIPRJMOD}/3D/PagerMotor.STEP", pcb_dir="/no/such/dir")
            self.assertEqual(rec["klass"], "confessed_missing")
            self.assertFalse(rec["exists"])

    def test_unknown_when_no_p3d_and_not_in_view(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = _resolver_with_view(tmp, [])
            rec = res.classify_ref("${KICAD6_3DMODEL_DIR}/Foo.3dshapes/Bar.step")
            # no partial clone -> at_sha None -> confessed_missing (cannot prove fetchable)
            self.assertEqual(rec["klass"], "confessed_missing")


# ---------------------------------------------------------------------------
# integration: classification against the pinned packages3D tree
# ---------------------------------------------------------------------------

@unittest.skipUnless(p3d_clone_ready(),
                     "packages3D partial clone not present -- run materialize.py first")
class TestClassifyAgainstPin(unittest.TestCase):
    def setUp(self):
        self.res = R.Resolver()

    def test_step_ref_is_fetchable(self):
        rec = self.res.classify_ref("${KISYS3DMOD}/Resistor_SMD.3dshapes/R_0603_1608Metric.step")
        self.assertIn(rec["klass"], ("fetchable", "resolved"))
        self.assertTrue(rec["at_sha"])

    def test_wrl_ref_is_fetchable_twin(self):
        rec = self.res.classify_ref("${KICAD6_3DMODEL_DIR}/Resistor_SMD.3dshapes/R_0402_1005Metric.wrl")
        # packages3D 10.0.x ships .step only: the .wrl is absent, its twin present
        self.assertIn(rec["klass"], ("fetchable_twin", "resolved"))
        self.assertFalse(rec["at_sha"])
        self.assertTrue(rec["twin_at_sha"])

    def test_libresolar_is_confessed_missing(self):
        rec = self.res.classify_ref("${KISYS3DMOD}/LibreSolar.3dshapes/DTMSS-27-H.STEP")
        self.assertEqual(rec["klass"], "confessed_missing")
        self.assertFalse(rec["at_sha"])
        self.assertFalse(rec["twin_at_sha"])


# ---------------------------------------------------------------------------
# integration: board-mode differential vs kicad-cli
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.path.isfile(WATCHY), "watchy board not materialized (wyred-t9y)")
class TestBoardMode(unittest.TestCase):
    def test_json_roundtrips(self):
        out = subprocess.run(
            [sys.executable, "-m", "klibs.resolver", "--board", WATCHY, "--json"],
            cwd=REPO, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        doc = json.loads(out.stdout)
        self.assertEqual(doc["mode"], "board")
        self.assertTrue(doc["records"])

    def test_seven_kiprjmod_confessed_missing(self):
        res = R.Resolver()
        out = res.board(WATCHY)
        prj = [r for r in out["records"] if r["kind"] == "project"]
        self.assertEqual(len(prj), 7, "watchy has 7 distinct ${KIPRJMOD} models")
        self.assertTrue(all(r["klass"] == "confessed_missing" for r in prj))

    @unittest.skipUnless(find_kicad_cli(), "kicad-cli not found")
    def test_differential_against_kicad_cli(self):
        kcli = find_kicad_cli()
        res = R.Resolver()
        with tempfile.TemporaryDirectory() as empty, \
                tempfile.TemporaryDirectory() as outdir:
            # corpus-safe: kicad-cli migrates/creates project sidecars in the
            # board's directory, so run it on a COPY, never the read-only
            # wyred-t9y corpus. Both engines then read the same copied board
            # (identical bytes -> identical model refs; ${KIPRJMOD} expands to
            # the copy's dir either way).
            board = os.path.join(outdir, os.path.basename(WATCHY))
            shutil.copyfile(WATCHY, board)
            # resolver: refs whose exact path is absent from the empty model dir
            out = res.board(board, model_dir=empty)
            resolver_missing = {r["ref"] for r in out["records"] if not r["exists"]}
            # kicad-cli: File-not-found with every *_3DMODEL_DIR var = empty dir
            env = dict(os.environ)
            for var in ("KISYS3DMOD", "KICAD5_3DMODEL_DIR", "KICAD6_3DMODEL_DIR",
                        "KICAD7_3DMODEL_DIR", "KICAD8_3DMODEL_DIR",
                        "KICAD9_3DMODEL_DIR", "KICAD10_3DMODEL_DIR"):
                env[var] = empty
            step = os.path.join(outdir, "watchy.step")
            proc = subprocess.run(
                [kcli, "pcb", "export", "step", "--subst-models", "--force",
                 "-o", step, board], env=env, capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            kcli_missing = set()
            for line in (proc.stdout + "\n" + proc.stderr).splitlines():
                if line.startswith("File not found: "):
                    kcli_missing.add(line[len("File not found: "):].strip())
            self.assertEqual(
                resolver_missing, kcli_missing,
                "\nresolver-only: %s\nkicad-cli-only: %s"
                % (sorted(resolver_missing - kcli_missing),
                   sorted(kcli_missing - resolver_missing)))
            self.assertTrue(kcli_missing, "expected a non-empty missing set")


# ---------------------------------------------------------------------------
# integration: footprint-id mode
# ---------------------------------------------------------------------------

class TestFootprintMode(unittest.TestCase):
    @unittest.skipUnless(
        os.path.isfile(os.path.join(REPO, "view", "footprints",
                                    "Resistor_SMD.pretty", "R_0402_1005Metric.kicad_mod")),
        "footprints view not materialized -- run materialize.py first")
    def test_resistor_footprint_yields_model_refs(self):
        res = R.Resolver()
        out = res.footprint_id("Resistor_SMD:R_0402_1005Metric")
        self.assertEqual(out["mode"], "footprint")
        self.assertTrue(out["records"], "footprint should carry >=1 (model ...) ref")
        self.assertTrue(all(r["kind"] == "model_dir" for r in out["records"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
