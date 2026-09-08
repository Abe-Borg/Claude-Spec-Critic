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

1. **Bump the version in both places** (`tests/test_release_metadata.py` keeps
   them in lockstep, so a mismatch fails CI):
   - `pyproject.toml` → `project.version`
   - `src/__init__.py` → `__version__`

   Versions are `MAJOR.MINOR.PATCH` with an optional `rcN` suffix (e.g. `3.1.0`
   or `3.1.0rc2`). A final release always supersedes its own release
   candidates.

2. **Commit** the bump on `master` (via a normal PR).

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

5. **(Recommended) Edit the release notes** on GitHub to describe what changed.
   The `notes` string in `latest.json` is what shows in the app's update dialog;
   by default it points users to the release page.

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
`pyproject.toml`). On a PR the read-only `build` job builds and self-checks the
app and compiles the installer **without publishing** (only the tag-gated
`publish` job can write to the repo), and it uploads the installer as a
downloadable artifact. So you can:

- confirm the Windows build still works before merging, and
- download and hand-test the actual installer from the PR's workflow run.

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

pyinstaller packaging/windows/spec-critic.spec --noconfirm --clean
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
| `packaging/windows/spec-critic.spec` | PyInstaller recipe. Bundles customtkinter/tkinterdnd2 assets, tiktoken's `tiktoken_ext` **and its pre-warmed `cl100k_base` rank file**, keyring's Windows backend, and the HTML trace viewer (the imports/data PyInstaller can't discover on its own). Embeds `spec-critic.manifest`; uses `spec-critic.ico` when that file exists. |
| `packaging/windows/bundle_assets.py` | Build-time helper the spec calls: turns the warmed tiktoken cache directory (`SPEC_CRITIC_TIKTOKEN_CACHE_SRC`) into `datas` entries under `tiktoken_cache/`, and **fails the build** when the directory is missing, empty, or its rank file does not hash-verify. Run it directly to validate a local cache. Unit-tested in `tests/test_packaging_entry.py`. |
| `packaging/windows/spec-critic.manifest` | The application manifest embedded in `SpecCritic.exe`: PyInstaller's default entries plus an explicit `longPathAware=true`. |
| `packaging/windows/spec-critic.ico` | **Not yet supplied.** The spec picks it up automatically once it exists; until then PyInstaller's stock windowed icon is used. |
| `packaging/windows/installer.iss` | Inno Setup script → `SpecCriticSetup.exe`. Per-user install (no admin), Start-menu shortcut, clean uninstaller, closes a running instance on update. Carries Spec Critic's own AppId GUID. |
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
