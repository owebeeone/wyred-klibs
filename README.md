# wyred-klibs — pinned KiCad library forks + on-demand materialize

Pinned, verified mirrors of the three official KiCad libraries — symbols,
footprints, and 3D packages — with **materialize-on-demand** in the wyred-t9y
style: `update = none` submodules, a commit-SHA pin recorded in
`manifest.json`, a gitignored `view/`, and a pure-stdlib `materialize.py`.
Nothing multi-GB is ever transferred. The libraries are **tooling input** that
re-pins on KiCad upgrades (a different invariant class from t9y's frozen
corpus), consumed by wyred-kicad (rung 1) and, later, wyred-3d (rung 2).

Pinned at the **10.0.4** release train (matching local `kicad-cli 10.0.4`).

## Usage

```sh
git clone https://github.com/owebeeone/wyred-klibs.git   # do NOT --recurse-submodules
cd wyred-klibs
python3 materialize.py                    # symbols + footprints wholesale,
                                          #   + packages3D partial clone (tree only)
python3 materialize.py --3d-from-board \
    ../wyred-t9y/view/watchy/Watchy.kicad_pcb   # materialize a board's 3D models
python3 materialize.py --check            # verify view/ against the pins
```

`view/` (gitignored, rebuildable):

| view path | source | how |
|---|---|---|
| `view/symbols/` | kicad-symbols @ 10.0.4 | wholesale `git archive` of the pinned tree |
| `view/footprints/` | kicad-footprints @ 10.0.4 | wholesale `git archive` of the pinned tree |
| `view/3dmodels/` | kicad-packages3D @ 10.0.4 | **blob-filtered** partial clone; only requested model blobs fetched |

Each view root carries the upstream `LICENSE.md`. Integrity rides on git
content addressing — no per-file sha256 lists — and `--check` verifies each
repo is at the pinned tree and compares view files to the pinned git blob ids.

### The resolver — `klibs.resolver`

```sh
python3 -m klibs.resolver --board ../wyred-t9y/view/watchy/Watchy.kicad_pcb --json
python3 -m klibs.resolver --footprint Resistor_SMD:R_0402_1005Metric
python3 -m klibs.resolver --board BOARD.kicad_pcb --fetch-list   # feeds materialize
```

Two modes over one targeted s-expr scan (no full parser): **footprint-id**
(`Lib:Name` → its `.kicad_mod` → its `(model ...)` entries) and **board**
(a `.kicad_pcb` → every embedded `(model ...)` ref). Each ref is classified
`resolved` / `fetchable` / `fetchable_twin` / `confessed_missing` against the
pinned packages3D tree and the view. `materialize.py --3d-from-board` feeds the
resolver's fetch targets straight into the sparse materializer.

## Fork provenance and the stale-GitHub-mirror discrepancy

The Rung-1 plan assumed `github.com/KiCad/{kicad-symbols,kicad-footprints,
kicad-packages3D}` are current GitLab mirrors and forked them to
`owebeeone/klibs-*` as availability pins. **They are not:** those GitHub
mirrors are frozen at **KiCad 5.1.7** (the project moved to GitLab ~2020). The
10.0.x train lives only on `gitlab.com/kicad/libraries/*`.

Consequences, all handled honestly rather than papered over:

1. **Fetches target GitLab** (`upstream` in `manifest.json`); the
   `owebeeone/klibs-*` forks are recorded per-library as `fork_url` with a
   `fork_status` flag. **Action to make the forks real 10.0.x availability
   pins: re-sync each fork from its GitLab upstream** (a GitHub account action,
   out of scope here). Until then `.gitmodules` points at GitLab so a fresh
   clone works.
2. **No `.wrl` twins upstream at 10.0.x.** packages3D 10.0.4 ships 7241 `.step`
   files and **zero** `.wrl`. The materializer fetches the `.step` twin for
   every referenced model and *confesses* the missing `.wrl` (not a fetch
   error). `kicad-cli --subst-models` does **not** substitute a `.step` twin for
   a `.wrl` board ref unless the `.wrl` file physically exists, so `.wrl` refs
   stay unresolved by kicad-cli against a git-materialized view — expected and
   confessed.
3. **Symbols use the split `*.kicad_symdir/` layout** at 10.0.x (per-symbol
   files, e.g. `Device.kicad_symdir/R.kicad_sym`), not a monolithic
   `Device.kicad_sym`.

## License

kicad-symbols / kicad-footprints / kicad-packages3D are **CC-BY-SA-4.0 with the
KiCad libraries exception**: designs, STEP models, and renders produced *using*
the libraries carry no copyleft obligation; *redistributing the libraries*
requires attribution + share-alike + preserved license text. wyred-klibs
redistributes nothing — `view/` is gitignored and materialized on demand from
the upstream repos, and the materializer copies the upstream `LICENSE.md` into
every view root so any downstream copy of a view tree carries its terms.
