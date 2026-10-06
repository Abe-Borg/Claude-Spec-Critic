# Releasing the Windows desktop app

This is the runbook for packaging Spec Critic as a downloadable Windows app
and shipping updates to installed users. It is written to be followed
step-by-step; you don't need to remember any of it between releases.

## The big picture (no server required)

Spec Critic is a **desktop app**, like VS Code. It runs entirely on the
user's PC and talks only to the Anthropic API with the user's own key. There is
**no backend to host** — no droplet, no always-on service, nothing to pay for
month to month.

Two free pieces of GitHub infrastructure do all the work:

- **GitHub Actions** builds the Windows installer (you don't need a Windows
  machine).
- **GitHub Releases** hosts the installer file *and* a tiny `latest.json`
  manifest that installed apps read to discover updates.

```
  You: git tag v3.1.0 && git push origin v3.1.0
        │
        ▼
  GitHub Actions (windows runner)
    • warm tiktoken→ build/tiktoken_cache/  (the cl100k_base rank file)
    • PyInstaller  → dist/SpecCritic/  (the frozen app, rank file bundled)
    • self-check   → prove the exe runs AND counts tokens from the bundle
    • Inno Setup   → SpecCriticSetup.exe
    • make_manifest→ latest.json  (version + download URL + sha256)
        │
        ▼
  GitHub Release  v3.1.0
    • SpecCriticSetup.exe   ← users download this
    • latest.json           ← installed apps poll this
        │
        ▼
  Installed apps  →  fetch releases/latest/download/latest.json  →
                     "v3.1.0 is available"  →  download + verify sha256  →  run installer
```

## Cutting a release

1. **Bump the version in all five places** (`tests/test_release_metadata.py`
   keeps them in lockstep, so a mismatch fails CI, and
   `packaging/windows/check_release_version.py` fails the release itself):
   - `pyproject.toml` → `project.version`
   - `src/__init__.py` → `__version__`
   - `README.md` → the `**vX.Y.Z**` headline
   - `CLAUDE.md` → the `# CLAUDE.md — Spec Critic vX.Y.Z` title
   - `CLAUDE.md` → the `# Package version (X.Y.Z)` source-layout note

   The first two decide behaviour (the shipped app reports `__version__`, which
   the updater compares against the manifest); the three documentation literals
   used to drift unguarded and are now checked read-only by the same guard. A
   missing literal fails rather than passing silently.

   Versions are `MAJOR.MINOR.PATCH` with an optional `rcN` suffix (e.g. `3.1.0`
   or `3.1.0rc2`). A final release always supersedes its own release
   candidates.

2. **Commit** the bump on `master` (via a normal PR).

   Add a nonempty `### vX.Y.Z` section to the README changelog (use the exact
   version, including `rcN` for a release candidate). The build extracts that
   section and installation instructions into a separate `release-notes`
   artifact; the publish job publishes the notes with the installer.
   Missing, empty, or duplicate version sections fail the build.

3. **Tag and push:**
   ```bash
   git tag v3.1.0
   git push origin v3.1.0
   ```
   The tag must be `v` + the exact version (`v3.1.0` for version `3.1.0`). The
   workflow refuses to publish if the tag and the version literals disagree
   (`packaging/windows/check_release_version.py`).

4. **Wait for the `Release (Windows installer)` workflow** to finish. It creates
   the GitHub Release with `SpecCriticSetup.exe` and `latest.json` attached.
   That's it — installed apps will offer the update within a day, or
   immediately when a user clicks **Check for Updates**.

5. **Check the release notes** on GitHub against the README changelog.
   The `notes` string in `latest.json` is what shows in the app's update dialog;
   it points users to the release page, which now includes that version's changes.

> **Release candidates are handled for you.** A tag with an `rcN` suffix
> (`v3.1.0rc1`) is published as a GitHub **pre-release** automatically, so GitHub
> never marks it "latest". The updater reads
> `releases/latest/download/latest.json`, which always resolves to the newest
> **full** release — so stable installs are never auto-offered a release
> candidate. Testers can still install an RC by downloading it from its release
> page directly.

## What CI validates on every PR (before you ever tag)

The same workflow runs on pull requests that touch packaging files
(`packaging/windows/**`, `release.yml`, `src/core/updates.py`,
`src/core/tokenizer.py`, `pyproject.toml`, `requirements.txt`, and `LICENSE`,
which the installer displays and installs). On a PR the read-only `build` job builds and self-checks the
app and compiles the installer **without publishing** (only the tag-gated
`publish` job can write to the repo), and it uploads the installer as a
downloadable artifact. So you can:

- confirm the Windows build still works before merging,
- download and hand-test the actual installer from the PR's workflow run, and
- read the `third-party-notices` artifact: the `THIRD-PARTY-NOTICES.txt` that
  installer installs (see "Third-party notices" below).

The self-check step runs the frozen `SpecCritic.exe --selfcheck`, which imports
the pipeline, the GUI toolkit, and the updater inside the frozen app, then
counts one short string with the app's tokenizer. This catches the #1
PyInstaller failure mode — a dependency that imports fine from source but was
never bundled into the exe — and the tokenizer's own first-use trap (see
"Bundled tokenizer data" below). The self-check output carries a line like

```
tokenizer: cl100k_base tokens=11 rank_file_present=True cache_dir=D:\a\...\_internal\tiktoken_cache
```

and the workflow fails unless the count is positive, `rank_file_present` is
`True` (sampled *before* the load — the runner has network access, so a
successful count alone could have been a download), and `cache_dir` is the
bundled folder. (The exe is windowed, so `sys.stdout` may be `None`; the
result is also written to the file named by `SPEC_CRITIC_SELFCHECK_OUT` so CI
can read it either way.)

## Building locally (optional)

You need a Windows machine with Python 3.11+.

```powershell
pip install -r requirements.txt
pip install -e . --no-deps
pip install pyinstaller

# Warm the tokenizer's rank file into the directory the spec bundles (see
# "Bundled tokenizer data" below). Mandatory: the build fails without it.
$env:TIKTOKEN_CACHE_DIR = "$PWD\build\tiktoken_cache"
python -c "import tiktoken; tiktoken.get_encoding('cl100k_base')"
Remove-Item Env:TIKTOKEN_CACHE_DIR
python packaging/windows/bundle_assets.py      # validates + lists what will ship
python packaging/windows/third_party_notices.py  # interpreter + Tcl/Tk license texts found?

pyinstaller packaging/windows/spec-critic.spec --noconfirm --clean
# → dist\SpecCritic\ and dist\THIRD-PARTY-NOTICES.txt (the build fails if a
#   bundled component's license text is missing; see "Third-party notices")
$env:SPEC_CRITIC_SELFCHECK_OUT = "$PWD\selfcheck.txt"
dist\SpecCritic\SpecCritic.exe --selfcheck   # sanity check; then read selfcheck.txt

# Then build the installer (install Inno Setup 6 first: https://jrsoftware.org/):
& "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" /DMyAppVersion=3.1.0 packaging\windows\installer.iss
# → dist\installer\SpecCriticSetup.exe
```

## The pieces (what each file does)

| File | Role |
|---|---|
| `packaging/windows/app_entry.py` | The frozen app's entry point; points `TIKTOKEN_CACHE_DIR` at the bundled rank file before any `src` import when frozen; adds `--version` / `--selfcheck` (imports + tokenizer probe) flags for CI. |
| `packaging/windows/spec-critic.spec` | PyInstaller recipe. Bundles customtkinter/tkinterdnd2 assets, tiktoken's `tiktoken_ext` **and its pre-warmed `cl100k_base` rank file**, keyring's Windows backend, and the HTML trace viewer (the imports/data PyInstaller can't discover on its own). Writes `dist/THIRD-PARTY-NOTICES.txt` after the analysis (see `third_party_notices.py`). Embeds `spec-critic.manifest`; uses `spec-critic.ico` when that file exists. |
| `packaging/windows/bundle_assets.py` | Build-time helper the spec calls: turns the warmed tiktoken cache directory (`SPEC_CRITIC_TIKTOKEN_CACHE_SRC`) into `datas` entries under `tiktoken_cache/`, and **fails the build** when the directory is missing, empty, or its rank file does not hash-verify. Run it directly to validate a local cache. Unit-tested in `tests/test_packaging_entry.py`. |
| `packaging/windows/third_party_notices.py` | Build-time helper the spec calls after the analysis: writes `dist/THIRD-PARTY-NOTICES.txt` with the license text of every bundled distribution, the interpreter, and Tcl/Tk, and **fails the build** when any of them cannot be found. Run it directly to check the interpreter and Tcl/Tk texts before a build. Unit-tested in `tests/test_packaging_entry.py`. |
| `packaging/windows/spec-critic.manifest` | The application manifest embedded in `SpecCritic.exe`: PyInstaller's default entries plus an explicit `longPathAware=true`. |
| `packaging/windows/spec-critic.ico` | **Not yet supplied.** The spec picks it up automatically once it exists; until then PyInstaller's stock windowed icon is used. |
| `packaging/windows/installer.iss` | Inno Setup script → `SpecCriticSetup.exe`. License Agreement page the user must accept (see "The license page" below), per-user install (no admin), Start-menu shortcut, clean uninstaller, closes a running instance on update. Installs `LICENSE` beside the app as `LICENSE.txt`, and `dist\THIRD-PARTY-NOTICES.txt` as `THIRD-PARTY-NOTICES.txt`. Carries Spec Critic's own AppId GUID. |
| `packaging/windows/make_manifest.py` | Writes `latest.json` (version, download URL, sha256). Round-tripped against the app's parser in `tests/test_updates.py`. |
| `packaging/windows/check_release_version.py` | The tag-time guard: tag must equal BOTH version literals. |
| `src/core/updates.py` | The in-app updater: fetch manifest → compare → download → verify sha256 → launch installer. Fully unit-tested, no network in tests. |
| `src/gui/update_controller.py` | The GUI side: footer (version + Check for Updates), daily auto-check, update dialog with progress bar, download lifecycle guards. |
| `.github/workflows/release.yml` | Ties it together: a read-only `build` job (every relevant PR + tag) and a write-scoped, tag-only `publish` job. RC tags publish as pre-releases. |

## Bundled tokenizer data (no first-use download)

The app estimates token counts locally with `tiktoken`'s `cl100k_base`
encoding. The tiktoken wheel does **not** contain that encoding's BPE rank
file: on first use tiktoken looks for a cached copy in the directory named by
`TIKTOKEN_CACHE_DIR` and, when it is absent, downloads it from a public Azure
blob host. Corporate workstations routinely allow `api.anthropic.com` and block
that host — on such a machine token analysis raised, the gauge stayed blank,
and the Run button never enabled, and nothing in CI could catch it because the
runner has network access.

The build therefore ships the file:

1. **Warm** — the workflow's "Warm the tiktoken cache" step loads
   `cl100k_base` once with `TIKTOKEN_CACHE_DIR` pointed at
   `build\tiktoken_cache` (the directory named by
   `SPEC_CRITIC_TIKTOKEN_CACHE_SRC`; the same path is the helper's default when
   the variable is unset). tiktoken names the cached file `sha1(<download
   URL>)`, so it is a bare 40-hex-character file, not `cl100k_base.tiktoken`.
2. **Bundle** — `spec-critic.spec` calls
   `bundle_assets.tiktoken_cache_datas()`, which adds every file in that
   directory to `datas` under the bundle folder `tiktoken_cache/`
   (`dist\SpecCritic\_internal\tiktoken_cache\` after the build). The helper
   **refuses to build** — a loud `BundleAssetError` naming
   `SPEC_CRITIC_TIKTOKEN_CACHE_SRC` — when the directory is missing or empty,
   lacks the rank file, or the rank file's SHA-256 does not match the digest
   tiktoken verifies at runtime. An installer that would download at first use
   can never ship silently.
3. **Point** — when running frozen, `app_entry.configure_tiktoken_cache()`
   sets `TIKTOKEN_CACHE_DIR` to that bundled folder *before any `src` import*
   (`setdefault`, so an operator-set value still wins). Source runs are
   untouched.
4. **Prove** — `--selfcheck` counts one string and reports
   `rank_file_present` (sampled before the load) and the cache directory in
   effect; the smoke step asserts a positive count, `rank_file_present=True`,
   and the bundled `cache_dir`.

If the rank file ever fails to load anyway, the app raises
`src.core.tokenizer.EncoderLoadError` with a message that names
`TIKTOKEN_CACHE_DIR`, the directory in effect, whether the file was present,
and the download tiktoken attempted — and logs the same text — instead of a
bare connection error.

## Application manifest and icon

`spec-critic.spec` embeds `packaging/windows/spec-critic.manifest` into
`SpecCritic.exe`. PyInstaller 6.11.1 treats a supplied manifest as a
**replacement** for its built-in template, not an overlay — it re-injects only
the `requestedExecutionLevel` and the Common-Controls v6 dependency — so the
file mirrors PyInstaller's default entry for entry (the `supportedOS` list,
`asInvoker`, Common-Controls) and pins `longPathAware=true` explicitly rather
than relying on a PyInstaller default that a future pin might change.
`longPathAware` lets the process open paths longer than 260 characters (deep
OneDrive / SharePoint sync folders). Note that Windows honors it **only when
the machine's `LongPathsEnabled` policy is also on**; the manifest is necessary
but not sufficient — on a machine without that policy only the extended-length
(`\\?\`) path form reaches such files, which is a code-side concern outside
the manifest. No DPI-awareness entry is declared: customtkinter
sets process DPI awareness at runtime and a manifest entry would override it.

**Icon: still to be supplied.** The spec uses `packaging/windows/spec-critic.ico`
when that file exists and otherwise passes `icon=None`, which gives PyInstaller's
stock windowed icon. Drop a real `.ico` (multi-size, 16–256 px) at that path
and the next build picks it up — nothing else has to change. Do not commit a
placeholder; a fabricated icon is worse than the stock one.

## The license page

`installer.iss` sets `LicenseFile=..\..\LICENSE`, so the installer shows the
repository's `LICENSE` (the PolyForm Noncommercial License 1.0.0, starting with
its `Required Notice:` line) on Inno Setup's **License Agreement** page, before
the user chooses where to install. "I do not accept the agreement" is selected
by default and **Next** stays disabled until the user selects **"I accept the
agreement"**; the only other way off the page is **Cancel**, which installs
nothing. The file is shown verbatim, including its Markdown headings, because
it is the text of record — a reformatted copy could drift from it.

- **Every interactive run shows the page, updates included.** The in-app
  updater launches the installer with no arguments, so a user accepts the terms
  again on each update, as they stand in that release.
- **Silent installs skip it.** `/SILENT` and `/VERYSILENT` skip every wizard
  page, this one too. Whoever deploys the app silently (an IT department, say)
  is responsible for the users accepting the terms.
- **The terms are installed too.** `[Files]` installs `LICENSE` beside the app
  as `LICENSE.txt` (by default `%LOCALAPPDATA%\Programs\Spec Critic\LICENSE.txt`),
  because the license's Notices clause requires anyone who receives the
  software to receive the terms and the Required Notice line with it. The
  uninstaller removes it with the rest of the app.
- **Encoding.** Inno Setup reads a Unicode `.txt` license only as UTF-8 or
  UTF-16LE. `LICENSE` is ASCII today; `tests/test_packaging_entry.py`
  (`TestInstallerLicense`) fails if it stops being valid UTF-8, if the page or
  the installed copy is removed, if a `[Code]` section skips the page or checks
  the accept button for the user, or if `LICENSE` is dropped from the paths
  that rebuild the installer on a pull request.

Changing the license text needs no packaging change: the next build picks it up.

## Third-party notices

`SpecCritic.exe` bundles the Python interpreter, Tcl/Tk, and every runtime
package with its own dependencies, and a binary that bundles them must carry
their license texts. The build writes them all into one file,
`dist\THIRD-PARTY-NOTICES.txt`, and `installer.iss` installs it beside the app
(by default `%LOCALAPPDATA%\Programs\Spec Critic\THIRD-PARTY-NOTICES.txt`),
next to `LICENSE.txt`.

**What it lists.** `spec-critic.spec` calls
`third_party_notices.write_third_party_notices()` right after the analysis,
before the archive, exe, and folder are built:

- **Every bundled distribution.** A package counts as bundled when the analysis
  collected a file its `RECORD` lists (a module, runtime hook, binary, or data
  file), so the list is what ships, not everything installed on the build
  machine: PyInstaller's own build-time helpers (altgraph, pefile, pip, ...) are
  left out unless a file of theirs is collected. PyInstaller is always listed,
  because its bootloader is `SpecCritic.exe`. Spec Critic's own distribution is
  not; its license is `LICENSE.txt`. For each: name, version, the license it
  declares (`License-Expression`, else a short `License` field, else its
  license classifiers), and the full text of every license file it ships —
  the files its metadata declares (`License-File`), any other LICENSE / LICENCE
  / COPYING / NOTICE / COPYRIGHT file in its `.dist-info`, and license-named
  files elsewhere in its `RECORD` (code vendored inside the package). A package
  that ships no file but puts the whole text in its `License` field is covered
  by that field.
- **The interpreter.** `LICENSE.txt` from the Python install the build runs on
  (`sys.base_prefix`), reproduced whole. python.org's Windows build writes that
  file by appending the terms of the native libraries it ships (the MSVC
  runtime notice, bzip2, libffi, OpenSSL, Tcl, Tk, Tix) to the PSF license, so
  those travel with it.
- **Tcl and Tk.** `license.terms` from the library directories PyInstaller's
  tkinter hook bundles (`tcltk_info.tcl_data_dir` / `tk_data_dir`). The
  python.org install has that file for Tk only (`tcl\tk8.6\license.terms`),
  so the Tcl terms are taken from the interpreter's `LICENSE.txt`, which holds
  them verbatim.

The file is UTF-8 with a byte-order mark and Windows line endings, so every
Notepad version shows license texts' non-ASCII characters correctly. Its order
is deterministic: the interpreter, Tcl, Tk, then packages by name.

**It fails the build rather than leave a license out.** If a bundled package
ships no license text, if the interpreter's `LICENSE.txt` is missing or is not
the Python license, or if the Tcl or Tk terms cannot be found, the spec raises
`NoticesError` listing every gap and PyInstaller stops. A notices file from an
earlier build is deleted first, so a failed build never leaves an old one for
the installer to pick up, and if the file is absent `ISCC` stops with "Source
file does not exist". Two earlier checks catch the likely failures sooner:

- The workflow's **Check interpreter and Tcl/Tk license texts** step runs
  `python packaging/windows/third_party_notices.py` before PyInstaller. Those
  texts depend on the runner's Python install, so a change there fails in
  seconds, with the files it looked for named.
- `tests/test_packaging_entry.py` (`TestRuntimeLockShipsLicenseTexts`) checks,
  in the ordinary test run, that every package pinned in `requirements.txt`
  and installed there ships a license text, so a pin bump that drops one fails
  on its pull request. (The Windows-only pins are not installed on the Linux
  test runner; the build checks them.)

**When a package ships no license text,** first look for a release that does.
Otherwise take the license text from that project's source at exactly the
pinned version and save it as
`packaging/windows/third_party_licenses/<name>-<version>.txt` (the name
lowercased with runs of `-`, `_`, `.` as one `-`, e.g.
`jaraco-classes-3.4.0.txt`). The notices then reproduce it, marked as
supplied with Spec Critic. The version in the file name is deliberate: the next
upgrade fails again until someone checks the new release's text. No package
needs this today.

**Why not `pip-licenses`.** It would be a new build-only dependency, and it
reports on what is installed: on the build machine that includes PyInstaller's
build-time helpers, and it cannot tell what the analysis actually collected.
It does not cover the interpreter or Tcl/Tk either. The standard library's
`importlib.metadata` reads each distribution's `RECORD` and license files,
which covers all of it with nothing extra to install.

## The code-signing situation (why users see a SmartScreen warning)

The app is shipped **unsigned** — there is no paid OS code-signing certificate.
The consequence, and *only* the consequence, is cosmetic: Windows SmartScreen
shows a **"Windows protected your PC / unrecognized app"** notice on the first
install and on each update. Users click **More info → Run anyway**. It does not
block anything.

This is a deliberate, documented trade-off, not a security hole. Note the two
*different* things both called "signing":

- **OS code-signing (skipped, the paid one)** — a certificate that makes the
  SmartScreen notice go away. ~$200–400/yr for Windows. Not used.
- **Update-integrity signing (used, free)** — the SHA-256 in `latest.json`,
  which the app verifies before running any downloaded installer. This is what
  actually protects users from a tampered download, and it costs nothing.
  `latest.json` and the installer URL are both required to be `https` — the
  manifest is the root of trust for that hash, so it must never arrive over
  plaintext.

### If you later want to remove the SmartScreen warning

Buy an OV/EV code-signing certificate, then add a signing step to
`release.yml` after the Inno Setup compile (sign both `SpecCritic.exe`
inside the bundle and the final `SpecCriticSetup.exe` with `signtool`).
Nothing else about this pipeline has to change.

## Operator env vars

| Variable | Effect |
|---|---|
| `SPEC_CRITIC_UPDATE_URL` | Override the manifest URL (testing / a fork's releases). Must be https. |
| `SPEC_CRITIC_DISABLE_UPDATE_CHECK` | Set truthy to turn update checks off entirely (locked-down deployments). |
| `SPEC_CRITIC_UPDATE_STATE_PATH` | Override the throttle-state file (`~/.spec_critic/update_check.json`). |
| `SPEC_CRITIC_SELFCHECK_OUT` | CI-only: where `--selfcheck` writes its result (the windowed exe may have no stdout). |
| `SPEC_CRITIC_TIKTOKEN_CACHE_SRC` | Build-only: the pre-warmed tiktoken cache directory `spec-critic.spec` bundles (default `<repo>/build/tiktoken_cache`). The build fails if it is missing/empty. |
| `TIKTOKEN_CACHE_DIR` | tiktoken's own cache-directory variable. The frozen app sets it to the bundled `tiktoken_cache` folder at startup unless it is already set; source runs leave it to tiktoken's default (`<tempdir>/data-gym-cache`). |

## Moving the repository

If the repo ever moves, update the owner/name in **one** place —
`GITHUB_OWNER` / `GITHUB_REPO` in `src/core/updates.py` — and the manifest and
releases-page URLs follow. (The workflow derives URLs from
`${{ github.repository }}` automatically.)
