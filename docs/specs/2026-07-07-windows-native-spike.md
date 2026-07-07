# T-4-10 Stage 1 — Windows Native Port: Decision Spike

**Date:** 2026-07-07  
**Branch:** `improve/wave-4-launch`  
**Author:** Claude (spike) — Owner decision required  
**Status:** DECIDED — **NO-GO** (owner, 2026-07-07). Stage 2 (the native port) is DEFERRED; Windows users continue via WSL / `pip install jarn`. This spec + the §6 Stage-2 checklist remain ready to execute if/when Windows-native is revisited. The spike recommendation was GO-with-conditions (Stage-2 = L); the owner opted to defer given the L effort + permanent Windows-specific maintenance vs. current WSL/pip coverage.

---

## Executive Summary

jarn can reach Windows users today via WSL or `pip install jarn`. A native
`npm i -g jarn-cli` experience on Windows requires a PyInstaller
`windows-x64` binary, an npm `jarn-cli-win32-x64` platform package, and
resolution of six POSIX call-site clusters. The CI already runs
`windows-latest` (added before this spike), which is a positive signal: the
test suite is substantially Windows-compatible today with targeted skips.

**Recommendation: GO-with-conditions** — the port is tractable but not
trivial. Total Stage-2 effort is **L** (large), primary risk is the process-
group kill path and PyInstaller bootstrap time, and the maintenance cost
(permanent Windows CI path + platform divergence) is real. See §5 for the
full reasoning.

---

## 1 · POSIX Call-Site Inventory

### 1.1 `fcntl` — checkpoint file lock

| Location | Line | What it does |
|---|---|---|
| `src/jarn/agent/checkpoint.py` | 46–48 | `try: import fcntl / except ImportError: fcntl = None` |
| `src/jarn/agent/checkpoint.py` | 252–253 | `if fcntl is not None: fcntl.flock(fd, fcntl.LOCK_EX)` |
| `src/jarn/agent/checkpoint.py` | 256–257 | `if fcntl is not None: fcntl.flock(fd, fcntl.LOCK_UN)` |

**Current Windows behaviour:** `fcntl` imports with `ImportError` → the
guard `fcntl = None` silently makes locking a no-op. The checkpoint lock
(`_checkpoint_lock`) still opens the file with `os.open(..., 0o600)` and
yields — but without any exclusive lock. Concurrent undo/redo calls from
separate processes are not serialised on Windows.

**Windows equivalent:** `msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` or, more
portably, the [`filelock`](https://py-filelock.readthedocs.io/) library
(already used by many tools in the Python ecosystem; adds ~20 kB to the
wheel). A simple `if sys.platform == "win32": msvcrt.locking(...)  else:
fcntl.flock(...)` branch would work without adding a dependency.

**Effort:** S — one helper function, two call sites.  
**Risk:** Low — the no-op degradation is the current Windows behaviour; the
race is unlikely in practice but correctible with minimal code.

---

### 1.2 `os.killpg` / `os.getpgid` — process-group termination

| Location | Line | What it does |
|---|---|---|
| `src/jarn/agent/process_util.py` | 12 | `os.killpg(pgid, 0)` — liveness probe for a process group |
| `src/jarn/agent/process_util.py` | 44 | `os.getpgid(pid)` — get process-group id of the spawned shell |
| `src/jarn/agent/process_util.py` | 46 | `os.killpg(pgid, signal.SIGTERM)` — graceful group termination |
| `src/jarn/agent/process_util.py` | 49 | `os.killpg(pgid, signal.SIGKILL)` — force group kill after grace |
| `src/jarn/agent/process_util.py` | 51 | `os.killpg(pgid, signal.SIGKILL)` — immediate group kill |

**Current Windows behaviour:** `process_util.terminate_process_group` already
has an `os.name == "posix"` branch (line 43). The `else` branch falls back to
`os.kill(pid, signal.SIGTERM)` (lines 54–59) which on CPython/Windows calls
`TerminateProcess()` on the _top-level_ PID only — child processes spawned by
that shell are **not** killed. The `# pragma: no cover - non-posix fallback`
annotation confirms this path is untested.

Callers that pass `start_new_session=True` (`local_backend.py:195,207`,
`background.py:159`, `docker_backend.py:431`) create a new console group on
Windows. However, killing that group requires either `taskkill /T /F /PID
<pid>` (available on all Windows versions) or
`subprocess.Popen.send_signal(signal.CTRL_BREAK_EVENT)` (only for processes
started with `CREATE_NEW_PROCESS_GROUP`). The `send_signal(CTRL_BREAK_EVENT)`
approach is the cleanest Windows-native option.

**Windows equivalent:**
```python
# In process_util.py, inside the else branch:
import subprocess
subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)], capture_output=True)
```
Or, preferred: use `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` when
spawning (replaces `start_new_session=True` on Windows) and then
`proc.send_signal(signal.CTRL_BREAK_EVENT)`.

**Effort:** M — three call sites in `process_util.py`, three spawn sites to
add `creationflags` conditionally, plus tests.  
**Risk:** Medium — incomplete process-group kill leaves zombie child processes
on Windows. This is the highest-risk functional gap.

---

### 1.3 `termios` / `tty` — terminal background detection

| Location | Line | What it does |
|---|---|---|
| `src/jarn/tui/termbg.py` | 133–134 | `import termios; import tty` (inside `_probe()`) |
| `src/jarn/tui/termbg.py` | 144–145 | `termios.tcgetattr(fd)` — save terminal state |
| `src/jarn/tui/termbg.py` | 150 | `tty.setraw(fd)` — switch to raw mode |
| `src/jarn/tui/termbg.py` | 182 | `termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)` — restore state |

**Current Windows behaviour:** `_probe()` is only reached when both stdin and
stdout are TTYs (line 118). The import of `termios`/`tty` inside `_probe()`
will raise `ImportError` on Windows; `detect()` wraps `_probe()` in
`try/except Exception: return None` (line 125). Consequently, `termbg.detect()`
returns `None` on Windows — the caller defaults to the "dark" theme. This is a
safe, graceful no-op.

**Windows equivalent:** No-op / return None. prompt_toolkit's own Windows
console backend handles terminal attributes independently.

**Effort:** None — already works.  
**Risk:** None — graceful degradation to "dark" theme is acceptable.

---

### 1.4 `textual.drivers.linux_driver` — keyfix (Kitty protocol)

| Location | Line | What it does |
|---|---|---|
| `src/jarn/tui/keyfix.py` | 45–46 | `import textual.drivers.linux_driver as linux_driver` (inside `apply_kitty_keyfix`) |
| `src/jarn/cli.py` | 266–268 | `from jarn.tui.keyfix import apply_kitty_keyfix; apply_kitty_keyfix()` |

**Current Windows behaviour:** `apply_kitty_keyfix()` wraps the import in
`try/except Exception: return False` (line 46). On Windows the module does not
exist → `ImportError` is caught → returns `False`. This is a safe, graceful
no-op.

The test suite handles this explicitly: `tests/test_keyfix.py:10–11` skips the
entire module on win32.

**Windows equivalent:** No-op. The Kitty keyboard protocol / Caps Lock bug is
macOS/Linux-only; Windows console does not use it.

**Effort:** None — already works.  
**Risk:** None.

---

### 1.5 `resource` module — file-descriptor count

| Location | Line | What it does |
|---|---|---|
| `src/jarn/agent/background.py` | 70–82 | `/proc/self/fd` on POSIX, then `resource.RLIMIT_NOFILE` as fallback |

**Current Windows behaviour:** The outer `if os.name == "posix"` (line 70)
skips the `/proc` path. The `import resource` on line 76 is inside
`try/except Exception: return None` — on Windows, `resource` is not available,
so the function returns `None`. `_open_fd_count()` is a diagnostic helper;
returning `None` simply suppresses the FD-count log entry. This is a safe
no-op.

**Windows equivalent:** No-op / return None. Windows equivalent would be
`ctypes` calls into `ntdll`; not worth it for a diagnostic counter.

**Effort:** None — already works.  
**Risk:** None.

---

### 1.6 `chmod(0o600)` / `chmod(0o700)` — secrets tree permissions

| Location | Line | What it does |
|---|---|---|
| `src/jarn/config/secrets.py` | 273 | `current.chmod(0o700)` — restrict `~/.jarn/secrets/` directories |
| `src/jarn/config/secrets.py` | 285 | `path.chmod(0o600)` — restrict secret files to owner-read-only |
| `src/jarn/agent/checkpoint.py` | 250 | `os.open(str(lock_path), os.O_RDWR \| os.O_CREAT, 0o600)` — create lock file with restricted perms |

**Current Windows behaviour:** `Path.chmod()` on Windows maps only the
"readonly" bit. `0o600` sets the file as writeable; `0o700` sets the
directory as accessible. Group/other restriction bits are silently ignored.
No `OSError` is raised — the code runs without crashing, but secrets files in
`~/.jarn/secrets/` are readable by other local users (or any process running as
the same user). This is a security degradation, not a crash.

**Windows equivalent:** Windows ACL enforcement via `icacls` or
`win32security`. Full parity with POSIX permissions requires the
`pywin32` package or shelling out to `icacls`. A pragmatic minimum is to
document the degraded security on Windows and emit a warning in `jarn doctor`.

**Effort:** S (for a warning/doc) to M (for full ACL enforcement).  
**Risk:** Medium (security) — acceptable as a documented limitation in a v1
Windows port, with a follow-up hardening issue.

---

### 1.7 `os_sandbox.py` — macOS/Linux OS sandbox

| Location | Line | What it does |
|---|---|---|
| `src/jarn/agent/os_sandbox.py` | 75–83 | `backend_name()` returns `None` for any non-darwin, non-linux platform |
| `src/jarn/agent/os_sandbox.py` | 271–274 | `wrap()` raises `RuntimeError` when no backend is available |

**Current Windows behaviour:** `backend_name()` already returns `None` on
win32 (line 83 falls through `if sys.platform == "darwin"` and
`if sys.platform.startswith("linux")`). The sandbox_backend (`sandbox_backend.py`)
is a remote LangSmith sandbox — platform-independent. The local Docker backend
(`docker_backend.py`) works on Windows-with-Docker-Desktop.

**Windows sandbox story:** No kernel-enforced OS sandbox on Windows (no
`sandbox-exec`, no `bwrap`). Options:
1. **No sandbox** — `os_sandbox.available()` returns False; the local backend
   runs commands unsandboxed. This is the current behaviour when neither tool
   is installed on Linux.
2. **Docker backend** — works on Windows if Docker Desktop is installed.
3. **Windows Sandbox / WSL2** — possible future work, out of scope for Stage 2.

The `test_os_sandbox.py` suite already handles this: `_POSIX_SANDBOX_POLICY`
mark (line 31–33) skips policy-string tests on win32; `test_returns_none_on_windows`
(line 62–64) explicitly verifies win32 behaviour.

**Effort:** None for Stage 2 — document "no OS sandbox on Windows; use Docker".  
**Risk:** Low — documented limitation, consistent with Linux without bwrap.

---

### 1.8 npm launcher `win32` hard-refusal

| Location | Line | What it does |
|---|---|---|
| `npm/jarn-cli/bin/jarn.js` | 18–21 | `PLATFORM_PACKAGES` dict — no `win32-x64` key |
| `npm/jarn-cli/bin/jarn.js` | 43–49 | `unsupportedMessage(key)` — "run J.A.R.N. under WSL" |

**Current behaviour:** When npm runs `jarn` on a `win32-x64` host,
`platformPackage("win32", "x64")` returns `null` (line 26), `!pkg` is true
(line 70), and `unsupportedMessage` is printed. The message (line 47) currently
says: _"On native Windows, run J.A.R.N. under WSL."_

**What must change:**
1. Add `'win32-x64': 'jarn-cli-win32-x64'` to `PLATFORM_PACKAGES` (line 18).
2. Add a `win32-x64` entry to `TARGETS` in `npm/build-packages.mjs` (line 43)
   with `{ os: 'win32', cpu: 'x64' }`.
3. Add `"jarn-cli-win32-x64": "0.0.0"` to `optionalDependencies` in
   `npm/jarn-cli/package.json` (currently linux + darwin only).
4. Update `unsupportedMessage` to no longer mention WSL as the only option.

**Effort:** S — pure config/data changes in three files.  
**Risk:** Low.

---

## 2 · Windows-Equivalent Map

| POSIX call | File:line | Windows equivalent | Runtime guard needed | Effort | Risk |
|---|---|---|---|---|---|
| `fcntl.flock` | `checkpoint.py:252–257` | `msvcrt.locking` or `filelock` lib | `if sys.platform == "win32"` | S | Low |
| `os.killpg` / `os.getpgid` | `process_util.py:12,44,46,49,51` | `taskkill /T /F /PID` or `send_signal(CTRL_BREAK_EVENT)` | Already `os.name == "posix"` guard — fix else-branch | M | Medium |
| `start_new_session=True` → group kill | `local_backend.py:195,207`; `background.py:159`; `docker_backend.py:431` | `creationflags=CREATE_NEW_PROCESS_GROUP` on win32 | `if sys.platform == "win32"` at spawn site | M | Medium |
| `termios`/`tty` (termbg) | `termbg.py:133–182` | No-op (returns None) | Already Exception-guarded — no change | None | None |
| `linux_driver` (keyfix) | `keyfix.py:45–46` | No-op (returns False) | Already Exception-guarded — no change | None | None |
| `resource.RLIMIT_NOFILE` | `background.py:76–78` | No-op (returns None) | Already Exception-guarded — no change | None | None |
| `chmod(0o600/0o700)` | `secrets.py:273,285` | `icacls` / document limitation | `if sys.platform == "win32"` + warning | S (warning) | Medium (security) |
| `sandbox-exec`/`bwrap` | `os_sandbox.py:75–83` | No OS sandbox; Docker works | Already returns None on win32 | None | None (documented) |
| npm `PLATFORM_PACKAGES` | `jarn.js:18–21` | Add `win32-x64` entry | N/A (JS config) | S | Low |
| PyInstaller build | `packaging/jarn.spec` | Add `windows-x64` runner in release.yml | N/A (CI config) | S–M | Medium (build bootstrap) |

---

## 3 · Packaging Additions Sizing

### 3.1 PyInstaller `windows-x64` build

**Current setup:** `packaging/jarn.spec` is a one-file PyInstaller spec that
calls `collect_all()` for 14 packages and produces `dist/jarn` on Linux/macOS.
`scripts/build-binary.sh` drives the build. The spec is OS-agnostic (no
platform conditionals).

**What needs to change:**
- Add a `windows-latest` runner to the `binaries` matrix in
  `.github/workflows/release.yml` (currently: `ubuntu-latest`,
  `ubuntu-24.04-arm`, `macos-latest`). New entry:
  ```yaml
  - os: windows-latest
    target: win32-x64
    gh_asset: jarn-windows-x64
  ```
- The build script `scripts/build-binary.sh` uses POSIX `set -euo pipefail`
  and is not callable from PowerShell. A thin `scripts/build-binary.ps1` (or
  a cross-platform Python equivalent) is needed, or the release workflow can
  inline the two commands directly.
- The binary output on Windows is `dist/jarn.exe`. The upload step and
  smoke test must reference `dist/jarn.exe`.
- `packaging/jarn.spec` sets `console=True` and `onefile=True` — both are
  correct for Windows TUI apps.
- `keyring` (in `_PACKAGES`) picks up the Windows Credential Manager backend
  automatically on Windows — no spec change required.
- UPX compression (`upx=True` in the spec) requires UPX to be available on the
  runner. GitHub's `windows-latest` runner does not have UPX pre-installed; it
  must be installed in the build step (e.g. `choco install upx`) or the spec
  must conditionally disable UPX on Windows.

**Binary size estimate:** The existing Linux binary is roughly comparable.
Windows binaries are typically 10–20% larger due to runtime DLL inclusion.

### 3.2 npm `jarn-cli-win32-x64` platform package

**Current pattern** (from `npm/build-packages.mjs:43-47`): each platform
package gets a `package.json` with `os` and `cpu` arrays generated by
`platformPackageJson(target, version)`, plus a `bin/jarn` binary copied in.

**Changes needed:**
1. `npm/build-packages.mjs:43` — add `'win32-x64': { os: 'win32', cpu: 'x64' }` to `TARGETS`.
2. `npm/jarn-cli/bin/jarn.js:18` — add `'win32-x64': 'jarn-cli-win32-x64'` to `PLATFORM_PACKAGES`.
3. `npm/jarn-cli/package.json:optionalDependencies` — add `"jarn-cli-win32-x64": "0.0.0"` (stamped to actual version at release time by `mainPackageJson()`).
4. The binary for the win32 package is `jarn.exe`. The `build-packages.mjs`
   script copies the binary to `bin/jarn`; on Windows it must copy `bin/jarn.exe`.
   The launcher resolves `require.resolve("jarn-cli-win32-x64/bin/jarn")` — on
   Windows npm will not find `bin/jarn` (no extension). Two options:
   a. Ship the exe as `bin/jarn` (no extension) and mark it executable — Windows
      can execute PE binaries without an extension if explicitly invoked via
      `spawnSync(binPath, argv, {stdio: 'inherit'})` (already how the launcher
      calls it; works without `.exe`).
   b. Ship as `bin/jarn.exe` and update the resolver to try both paths.
   Option (a) is simpler and aligns with the existing pattern on Linux/macOS.

### 3.3 Release workflow additions summary

| Change | File | Size |
|---|---|---|
| Add `win32-x64` build job to binaries matrix | `.github/workflows/release.yml` | +~15 lines |
| Add PowerShell/inline build step | `.github/workflows/release.yml` or `scripts/` | +~10 lines |
| Add `win32-x64` to `TARGETS` | `npm/build-packages.mjs` | +1 line |
| Add `win32-x64` to `PLATFORM_PACKAGES` | `npm/jarn-cli/bin/jarn.js` | +1 line |
| Add `jarn-cli-win32-x64` to optionalDeps | `npm/jarn-cli/package.json` | +1 line |

---

## 4 · Test-Matrix Cost

### 4.1 Current state

The CI matrix (`ci.yml:14`) **already includes `windows-latest`** with Python
3.12 and 3.13. The `windows-latest` job:
- Skips `mypy` (line 27–29, comment: "POSIX-only APIs (fcntl, killpg, …)
  are not modeled on win32").
- Runs the full `pytest -q` suite with coverage.

The `test_ci.py::test_ci_has_windows_matrix` test (line 68) asserts
`windows-latest` is present — so the matrix is already a hard requirement.

### 4.2 Tests already guarded for Windows

| Test file | Guard mechanism | What is skipped |
|---|---|---|
| `test_keyfix.py:10–11` | `pytest.skip(..., allow_module_level=True)` if win32 | Entire module (linux_driver import) |
| `test_local_backend.py:30` | `@pytest.mark.skipif(sys.platform == "win32", ...)` | `test_terminate_all_kills_spawned_process_tree` |
| `test_os_sandbox.py:31–33` | `_POSIX_SANDBOX_POLICY` mark | Sandbox policy string tests |
| `test_secrets.py:48,206` | `@pytest.mark.skipif(sys.platform == "win32", ...)` | chmod-based permission tests (2 tests) |
| `test_doc_sync.py:50–51` | `@pytest.mark.skipif(sys.platform == "win32", ...)` | 1 test |
| `test_docker_integration.py:33` | `skipif(sys.platform == "win32", ...)` | Docker integration test |

### 4.3 Suites that would need attention for a Stage-2 port

| Suite | Windows concern | Required action |
|---|---|---|
| `test_checkpoint.py` | No win32 guards; uses `fcntl` no-op path | Verify locking tests pass; add win32 skip or fix lock helper |
| `test_background.py` | `terminate_process_group` fallback is untested on win32 | Add windows-process-tree test or mark skip until §1.2 is fixed |
| `test_local_backend.py` | Process-group kill test already guarded; basic execution test adapted (line 23) | Add `test_terminate_all_kills_spawned_process_tree` win32 equivalent after §1.2 fix |
| `test_git_commands.py` | No win32 guards seen; git subprocess tests use POSIX paths | Verify path separator handling; git on windows-latest runner uses forward slashes via Git for Windows |
| `test_secrets.py` | Two tests skip on win32; chmod behaviour silent no-op | Add win32 `icacls` verification test when §1.6 hardened |
| `test_packaging.py` | Already handles win32 (line 69–70: `Scripts/python.exe`) | Extend to `test_win32_package_assembly` for the npm platform package |

### 4.4 mypy skip

The `if: runner.os != 'Windows'` skip on mypy is because `fcntl` and
`os.killpg` are not in the Windows stub. After Stage 2 (where those call sites
get `if sys.platform == "win32"` branches), the mypy skip can likely be dropped
if the branches satisfy the type checker. However, `os.killpg` and
`os.getpgid` are not in `typeshed` for Windows at all — so the skip may need to
remain unless the POSIX branch is inside a `if sys.platform != "win32":` type
guard.

### 4.5 CI minutes estimate

A `windows-latest` runner takes roughly 2–3× longer than `ubuntu-latest` for
the same pytest suite (Windows I/O and process startup overhead). The existing
matrix already pays this cost for the pytest run. Stage 2 adds one more
release-workflow `windows-latest` build job (~8–12 min for PyInstaller) and
one test job. Cost: approximately +15 CI-minutes per release tag.

---

## 5 · GO/NO-GO Recommendation

**Recommendation: GO-with-conditions**

### Rationale

1. **The infrastructure base is further along than expected.** CI already runs
   `windows-latest`. The terminal subsystem (termbg, keyfix) is already safely
   no-op on Windows. The sandbox already returns None. The packaging tooling
   (PyInstaller spec, npm build script) requires additive changes only, not
   structural rewrites.

2. **Two real blockers remain**, both fixable in Stage 2:
   - **Process-group kill** (§1.2): The current Windows fallback kills only the
     top-level process. LLM-spawned shell trees would leave orphan processes.
     This is the highest-risk functional gap and must be fixed before declaring
     a working Windows port.
   - **Checkpoint lock no-op** (§1.1): The current no-op locking is a latent
     race. Low probability in normal single-user use, but should be fixed.

3. **The market-reach upside is real.** Windows is ~72 % of desktop OS market
   share. The WSL fallback is a friction point for non-developer users. A native
   binary installable via `npm i -g jarn-cli` removes that friction.

4. **The maintenance cost is permanent** — a `windows-latest` release build
   job, platform-specific code paths in `process_util.py`, and ongoing CI
   minutes. This is manageable because the existing CI already carries the
   Windows pytest matrix; the release-time addition is additive.

5. **Conditions for GO:**
   - Owner accepts the Stage-2 effort estimate of **L** (approx 3–5 days of
     focused engineering).
   - Stage 2 is gated on a working Windows smoke test in CI before any npm
     publish (binary exits 0 on `jarn --version`).
   - The security degradation on `chmod` (§1.6) is documented in the README
     install matrix and a `jarn doctor` warning is emitted on win32. Full ACL
     hardening is deferred to a follow-up.

### Cost Table

| Work item | Effort | Risk | Notes |
|---|---|---|---|
| Process-group kill win32 branch (`process_util.py`) | M | Medium | Core functional blocker; taskkill or CTRL_BREAK |
| Spawn site flags (`local_backend`, `background`, `docker_backend`) | S | Low | Companion to above |
| Checkpoint file lock win32 branch | S | Low | `msvcrt.locking` or `filelock` |
| `chmod` security warning in `jarn doctor` | S | Low | Defers full ACL to follow-up |
| PyInstaller `windows-x64` release build job | S–M | Medium | UPX + PS build script; binary smoke test |
| npm `jarn-cli-win32-x64` package + launcher entry | S | Low | Pure config changes |
| Windows pytest matrix (already in CI) | None | None | Already running |
| Drop mypy skip on Windows | S | Low | Requires type guards; may stay for POSIX stubs |
| README install matrix update | S | None | Doc only |
| **Total Stage-2** | **L** | **Medium** | — |

### Biggest Risks

1. **Process-tree kill reliability on Windows.** `taskkill /T /F` is the
   standard approach but has known edge cases with short-lived processes and
   Windows job objects. Should be validated with an integration test on a
   Windows runner before declaring done.

2. **PyInstaller on `windows-latest`.** PyInstaller 6.21 (current pinned
   version) supports Windows but the build bootstrap is slower (~8–12 min),
   UPX may need explicit installation, and DLL bundling can introduce
   antivirus false-positives. The UPX flag in `packaging/jarn.spec:61` may
   need to be conditionally disabled on win32.

3. **prompt_toolkit Windows console quirks.** prompt_toolkit has a Windows
   console backend (`win32` or `Win32Conapi`) that behaves differently from the
   VT100 backend. The jarn REPL relies on prompt_toolkit; basic operation
   should work but edge cases (Unicode, mouse support, title setting) need
   manual smoke testing on a real Windows terminal (Windows Terminal + PowerShell).

4. **Docker-on-Windows sandbox reliability.** The Docker backend
   (`docker_backend.py`) works on Windows with Docker Desktop, but Docker
   Desktop's `windows-latest` GitHub runner availability is limited. The
   `test_docker_integration.py` suite skips on win32; end-to-end Docker sandbox
   validation would require a self-hosted Windows runner.

5. **Git subprocess tests on Windows paths.** `test_git_commands.py` and
   `test_checkpoint.py` spawn `git` subprocesses. Git for Windows uses forward
   slashes in most output, which is compatible, but absolute Windows paths
   (e.g. `C:\Users\...`) in error messages could break path-parsing assertions
   in tests that currently assume POSIX paths.

---

## 6 · Stage-2 Implementation Checklist (execute only on GO)

The following is the concrete file-change list for Stage-2 implementation.

### 6.1 Portable file-lock helper

- **File:** `src/jarn/agent/checkpoint.py:244–258` (`_checkpoint_lock`)
- **Change:** Replace the raw `fcntl.flock` calls with a helper that branches
  on `sys.platform`:
  ```python
  def _file_lock(fd: int) -> None:
      if sys.platform == "win32":
          import msvcrt
          msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
      elif fcntl is not None:
          fcntl.flock(fd, fcntl.LOCK_EX)

  def _file_unlock(fd: int) -> None:
      if sys.platform == "win32":
          import msvcrt
          msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
      elif fcntl is not None:
          fcntl.flock(fd, fcntl.LOCK_UN)
  ```
- **Test:** `tests/test_checkpoint.py` — verify locking still serialises
  concurrent calls on win32 (mock platform or run in CI).

### 6.2 `process_util.terminate_process_group` Windows branch

- **File:** `src/jarn/agent/process_util.py:52–59` (the `else` branch)
- **Change:** Replace `os.kill(pid, signal.SIGTERM)` with:
  ```python
  else:
      # Windows: use taskkill to kill the whole process tree
      import subprocess as _sp
      _sp.run(
          ["taskkill", "/T", "/F", "/PID", str(pid)],
          capture_output=True,
      )
  ```
- **Spawn sites:** `local_backend.py:195,207`, `background.py:159`,
  `docker_backend.py:431` — optionally add
  `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP` on `sys.platform == "win32"`
  alongside `start_new_session=True` (which is ignored by Python on Windows;
  `CREATE_NEW_PROCESS_GROUP` is the Windows equivalent).
- **Test:** Add a `windows-latest` variant of
  `test_terminate_all_kills_spawned_process_tree` in `test_local_backend.py`.

### 6.3 Keyfix — already a no-op on Windows

- **File:** `src/jarn/tui/keyfix.py` — no change required.
- Confirm `test_keyfix.py` module-level skip remains in place.

### 6.4 Windows pytest matrix — already present

- **File:** `.github/workflows/ci.yml` — no change required; `windows-latest`
  is already in the matrix (line 14).
- **Drop mypy skip:** Update line 27–29 once §6.1 and §6.2 are typed correctly
  (i.e., `if sys.platform != "win32": os.killpg(...)` satisfies the type
  checker). If `os.killpg` remains untyped on Windows stubs, keep the skip with
  an updated comment.

### 6.5 Release workflow: Windows build job

- **File:** `.github/workflows/release.yml:53–94` (the `binaries` matrix)
- **Add entry:**
  ```yaml
  - os: windows-latest
    target: win32-x64
    gh_asset: jarn-windows-x64
  ```
- **Build step:** Replace `./scripts/build-binary.sh` with a conditional:
  ```yaml
  - name: Build binary
    run: |
      if [ "${{ runner.os }}" = "Windows" ]; then
        uv sync --extra dev --extra build
        cd packaging && uv run pyinstaller jarn.spec --noconfirm --distpath ../dist --workpath ../build/pyinstaller
      else
        ./scripts/build-binary.sh
      fi
    shell: bash  # Git Bash available on windows-latest
  ```
  Or write a `scripts/build-binary.ps1` mirroring the bash script.
- **Smoke test:** Change `./dist/jarn --version` to
  `./dist/jarn.exe --version` on Windows (or use `dist/jarn` — Git Bash finds
  `.exe` automatically).
- **UPX:** Add `choco install upx` before the build step, or conditionally set
  `upx=False` in `jarn.spec` when `sys.platform == "win32"` (PyInstaller spec
  runs on the build host).

### 6.6 npm platform package + launcher

- **`npm/build-packages.mjs:43`** — add `'win32-x64': { os: 'win32', cpu: 'x64' }` to `TARGETS`.
- **`npm/jarn-cli/bin/jarn.js:18`** — add `'win32-x64': 'jarn-cli-win32-x64'` to `PLATFORM_PACKAGES`.
- **`npm/jarn-cli/package.json`** — add `"jarn-cli-win32-x64": "0.0.0"` under `optionalDependencies`.
- **Launcher message:** Update `unsupportedMessage` (line 43–49) to remove the WSL-only suggestion (or make it conditional on the host key).
- **Binary name:** Ship the Windows binary as `bin/jarn` (no `.exe` extension) in the npm package. PyInstaller on Windows produces `jarn.exe`; rename it during the assemble step in `build-packages.mjs`.
- **Test:** Update `npm/test/build.test.mjs` to cover the `win32-x64` target.

### 6.7 README install matrix

- **File:** `README.md` (and `README-TH.md`)
- **Change:** Add a Windows row to the install matrix:
  ```
  | Windows (native) | npm i -g jarn-cli | npm ≥ 16 + Windows 10/11 |
  | Windows (WSL2)   | (as Linux)         | WSL2 recommended for Docker sandbox |
  ```
- **Note:** Add a callout that OS-level sandbox is unavailable on Windows
  (Docker sandbox works if Docker Desktop is installed) and that file
  permissions on `~/.jarn/secrets/` are not enforced via ACLs in v1.

### 6.8 `jarn doctor` warning for Windows security degradation

- **File:** `src/jarn/doctor/` (relevant check module)
- **Change:** Add a `win32` platform check that emits a warning if the running
  platform is Windows, noting that secret file permissions are not enforced and
  recommending Docker Desktop for sandboxed execution.

---

## Appendix: Confirmed Non-Blockers

The following items were checked and confirmed to be safe on Windows without
any code change:

- `os.fork` — **not used** in `src/`. No hits from `grep -rn "os.fork" src/`.
- `import pwd` / `import grp` — **not used** in `src/`. No hits.
- `os.getuid` / `os.geteuid` — **not used** in `src/`. No hits.
- `os.getaddrinfo` / sockets — standard library, works on Windows.
- `Path.as_posix()` — used in `repomap.py:521,541` and `app.py:741` for
  constructing `@`-mention strings; forward slashes are used intentionally for
  display/protocol purposes, not filesystem access — safe on Windows.
- `signal.SIGTERM` / `signal.SIGKILL` — imported in `process_util.py` but
  `SIGKILL` is POSIX-only. On Windows, `signal.SIGKILL` does not exist;
  however, it is only referenced inside the `if os.name == "posix":` branch
  (lines 46–51), so no `AttributeError` occurs at runtime.
- `clipboard.py:151–169` (`_grab_windows`) — already implemented and tested
  (see `test_image_paste.py:75–92`). Windows clipboard support is already
  present.

---

*Spike authored 2026-07-07. Owner GO/NO-GO decision to be recorded in this
document before Stage-2 work begins.*
