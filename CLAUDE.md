# wyred-klibs — boundary rules

- **Data-mirror utility, not a wyred workspace member.** Like wyred-t9y, this
  repo is a sibling of wyred-wz, not inside it. It imports nothing from the
  wyred workspace; consumers (wyred-kicad now, wyred-3d later) import
  `klibs.resolver` as a plain external library and reach kicad-cli by
  subprocess.
- **No vendoring of KiCad libraries.** `view/` and the `upstream/klibs-*`
  submodule clones are gitignored / materialized on demand. Never commit
  library bytes. The pin is the commit SHA in `manifest.json` (tags are
  mutable); `.gitmodules` carries `update = none`.
- **Fetches go to GitLab canonical (`upstream`), not the GitHub forks.** The
  `owebeeone/klibs-*` forks are frozen at KiCad 5.1.7 (their `github.com/KiCad/*`
  parents are stale mirrors) and cannot serve 10.0.x. See README "Fork
  provenance". Do not repoint `.gitmodules` at the forks until they are
  re-synced from GitLab.
- **Confess, never silently fix.** `.wrl` twins are absent upstream at 10.0.x
  (packages3D ships `.step` only); unresolvable model refs (`${KIPRJMOD}`,
  non-official libs) are reported in the confessed-missing list, not fabricated.
- Pure Python 3.10 stdlib + git. No new dependencies. Cross-repo composition is
  subprocess-only.
- House git rules: never commit/push/tag without Gianni's explicit instruction;
  no AI co-author trailer.
