# Shipping It: The Windows App & Self-Updater

Every previous chapter describes a program you run from a checkout. That is how
the engineers who wrote it use it. It is not how a specification reviewer uses
it — they want an installer, an icon, and an app that keeps itself current.

This chapter is about the distribution path: a PyInstaller build wrapped in an
Inno Setup installer, published by a tag-triggered GitHub Actions workflow, and
an in-app updater that authenticates what it downloads.

It belongs in a handbook about trust because distribution is where a trustworthy
program most easily becomes an untrustworthy one. The reviewed-code guarantees of
Chapters 1–22 apply to the source. What a user actually runs is a binary that
arrived over a network. Everything below exists to keep those two things the same
thing.

## 1. The shape of the build

The app ships as an **unsigned** PyInstaller one-folder build inside a **per-user**
Inno Setup installer. `packaging/windows/` holds the pieces:

| File | Role |
|---|---|
| `app_entry.py` | Frozen entry point; supports `--version` and `--selfcheck` |
| `spec-critic.spec` | PyInstaller spec — hidden imports, bundled assets |
| `installer.iss` | Inno Setup script, versioned via `/DMyAppVersion=` |
| `make_manifest.py` | Emits `latest.json` |
| `check_release_version.py` | The tag-vs-literals guard |

`.github/workflows/release.yml` builds and publishes on a `v*` tag; the full
runbook is `docs/RELEASE_WINDOWS.md`.

### Unsigned by choice

Code signing costs money annually and buys a SmartScreen reputation that accrues
slowly. The project's position is explicit: the app is unsigned, SmartScreen's
"More info → Run anyway" notice is the documented cost, and **the free SHA-256
integrity gate is what actually authenticates downloads** (§3).

That is a defensible trade so long as it is stated rather than hidden, which is
why the release body itself tells the user the warning is expected. A user who is
told in advance that they will see a scary dialog is in a better position than
one who is surprised by it — and a project that pretends the dialog does not
exist teaches its users to click through warnings.

### `--selfcheck`

The frozen exe supports a self-check whose result is written to the path in
`SPEC_CRITIC_SELFCHECK_OUT`, because a windowed PyInstaller build may have
`sys.stdout = None` — printing would go nowhere.

CI smoke-runs it on every packaging change. The failure it catches is the classic
PyInstaller one: a module imported dynamically that static analysis cannot see, so
the app builds cleanly and dies on launch. `tiktoken_ext`,
`keyring.backends.Windows`, and the customtkinter / tkinterdnd2 asset trees are
all enumerated in `spec-critic.spec` for exactly this reason.

It also catches a second, quieter failure since v3.4.x: a build that ships without
tiktoken's `cl100k_base` rank file. The tiktoken wheel does not include it —
tiktoken downloads it from a public blob host at first use, which corporate
networks routinely block even when `api.anthropic.com` is allowed, so the token
gauge never enabled the Run button on such a workstation. The release workflow
warms the file into `build/tiktoken_cache`, `bundle_assets.py` bundles it (and
fails the build when it is missing, empty, or fails tiktoken's own hash check),
`app_entry.py` points `TIKTOKEN_CACHE_DIR` at the bundled copy before any `src`
import, and `--selfcheck` counts one string and reports
`tokenizer: cl100k_base tokens=<n> rank_file_present=<bool> cache_dir=<dir>`;
the smoke step asserts a positive count and `rank_file_present=True` (sampled
before the load, because the CI runner *has* network and a count alone could be
a download).

## 2. The version guard

`check_release_version.py` requires the tag to equal **both** `pyproject.toml`'s
`project.version` and `src/__init__.py`'s `__version__`.

The failure it prevents is specific and nasty. Suppose the tag is `v3.4.0` but
`__version__` was left at `3.3.0`. The workflow builds an installer, and
`make_manifest.py` writes a `latest.json` advertising version `3.4.0`. The user
installs it. On next launch the app reports its own version as `3.3.0`, compares
that against the manifest's `3.4.0`, and offers an update — which installs the
same build, which still reports `3.3.0`. **A perpetual update loop**, shipped, on
every install.

`tests/test_release_metadata.py` keeps the pair in lockstep on every PR, so the
drift is caught long before a tag exists. It also asserts both literals parse
under the updater's own grammar — a version the updater cannot parse could never
be compared to a manifest at all.

RC tags (`vX.Y.ZrcN`) publish as GitHub **pre-releases**, so
`releases/latest/download/latest.json` never auto-offers them to stable installs.
`parse_version` orders `rcN` strictly below its final, so the ordering matches the
publishing behavior rather than merely coexisting with it.

## 3. The updater's chain of trust

`src/core/updates.py` is stdlib-only with injectable fetcher, opener, and clock
seams — which is what makes it testable without a network — and `check_for_update`
**never raises**. An updater that can crash the app it updates is a liability.

The chain has exactly one root: **the manifest is the root of trust, because it
carries the installer's authenticating SHA-256.** Everything else follows.

### https, enforced three times and after redirects

`fetch_manifest`, `parse_manifest`, and `download_installer` each check
independently that their URL is https. The redundancy is deliberate: the
`SPEC_CRITIC_UPDATE_URL` override exists for testing and forks, and no override
may be able to downgrade the chain to plaintext.

Subtler, and the detail most likely to be lost in a future refactor: because
`urlopen` **follows redirects**, the *post-redirect final URL* is re-checked
(`_require_https_final_url`). An https URL that 30x-es to http is refused. Without
that check, the https enforcement would be theater — an attacker able to control
the redirect target could serve the manifest, and therefore the expected hash,
over plaintext.

`parse_manifest` additionally enforces that `version` matches the pinned grammar
and that `sha256` is 64 hex characters (`_SHA256_RE`), so a malformed manifest
fails at parse rather than at some later comparison.

### Download atomicity

```
stream → foo.exe.part   (SHA-256 computed during the stream)
         ↓ only if the hash matches
       os.replace → foo.exe
```

The download streams to a `.part` temp file, the hash is computed **during** the
stream rather than by re-reading, and `os.replace` promotes to the final `.exe`
name **only after the hash matches**. Any failure — including a checksum mismatch
— deletes the temp.

The invariant: **nothing runnable-but-unverified ever exists at the final path.**
A partially-downloaded or tampered installer never occupies the name the app
would later launch.

I have verified this chain end to end against a live release: the published
`latest.json` for v3.4.0 carries the same SHA-256 that GitHub records for the
uploaded `SpecCriticSetup.exe`, and the real manifest passes `parse_manifest`
unmodified.

### The platform gate

The updater UI is gated on `installer_platform_supported()`. The release asset is
a Windows `.exe`, so a macOS or Linux source run is never auto-nagged and never
offered the download dialog. A manual check there reports the version and points
at the releases page.

## 4. GUI lifecycle guards

The footer carries the version and a "Check for Updates" button, plus a
once-a-day silent launch check and an update dialog offering Download & Install /
Skip this Version / Later with a progress bar. Around that sit several guards,
each of which encodes a bug someone thought about:

- **One download at a time.**
- **Closing the dialog mid-download cancels its completion handling**, so it
  cannot later pop a stray install prompt for a dialog the user dismissed.
- **Download start *and* the post-download install prompt are both refused while a
  review run or drawing digest is active** (`is_processing` /
  `_drawing_digest_running`), and the check is **re-run when the download
  completes** — because a run may have started during the download.
- **A silent check whose result arrives mid-run** sets the footer status without
  popping a dialog.

### Serializing the two startup prompts

`main()` chains the silent update check **strictly after** the batch-resume prompt
returns.

The naive fix — stagger the two with different `after` timers — does not work, and
the reason is a genuine Tk subtlety worth recording: **tk after-timers keep firing
inside a modal messagebox loop.** A resume prompt sitting open does not pause the
scheduler, so a staggered update dialog stacks on top of it. Only sequencing the
second on the first's return actually serializes them.

## 5. Workflow security posture

The workflow splits privileges along the line of who executes repository code:

| Job | Permissions | Runs repo code? |
|---|---|---|
| `build` | `contents: read`, `persist-credentials: false` | **Yes** — PyInstaller imports the package and runs the spec |
| `publish` | `contents: write`, tag-gated | **No** — downloads the artifact and calls `gh` |

The `build` job executes repository-controlled code, so it holds no write token
and does not persist a git credential. A pull-request build therefore never has a
write-capable token, which matters because PR builds run code from the PR. Only
the tag-gated `publish` job — which runs no repository code — can write.

Third-party actions are pinned to immutable commit SHAs, with the human-readable
version in a trailing comment.

The PR trigger includes `pyproject.toml`, so a version bump validates the whole
Windows packaging pipeline **before** any tag exists. That is how a release
candidate proves the installer compiles and self-checks without publishing
anything.

## 6. State and configuration

| Path / variable | Purpose |
|---|---|
| `~/.spec_critic/update_check.json` | Throttle state: last-check timestamp, "skip this version" marker |
| `SPEC_CRITIC_UPDATE_STATE_PATH` | Override for the above |
| `~/.spec_critic/updates/` | Download directory |
| `SPEC_CRITIC_UPDATE_URL` | Manifest URL override — **must be https** |
| `SPEC_CRITIC_DISABLE_UPDATE_CHECK` | Truthy disables the launch check **and** the manual button, for locked-down deployments |

## 7. Cutting a release

1. Bump the version in `pyproject.toml` **and** `src/__init__.py`.
2. Update the changelog.
3. Commit, open a PR — the packaging pipeline validates on the `pyproject.toml`
   path trigger.
4. Merge, then `git tag vX.Y.Z && git push origin vX.Y.Z`.
5. The workflow builds and publishes the installer plus `latest.json`.

Step 4 is the release. Everything before it is reversible; the tag push is what
installed clients start seeing.

## 8. Pins

`tests/test_updates.py` covers the updater contract, download atomicity, the https
gates including the post-redirect case, the throttle, the manifest maker's
round-trip, the version guard, and — AST-checked — the GUI wiring.

AST-checking the GUI wiring is unusual and worth explaining: the guards in §4 are
*absences* as much as presences (no download start while processing, no dialog on
a silent check), and an absence is hard to assert by calling a function. Checking
the syntax tree asserts the guard is written where it must be, which is the
property that actually degrades when someone refactors the controller.
