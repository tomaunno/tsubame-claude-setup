# /// script
# requires-python = ">=3.11"
# ///
"""TSUBAME 4.0 setup steps, driven by Claude Code (or Codex) following SETUP.md.

Every subcommand does one step, prints what it did, and ends with exactly one
line `RESULT {json}` for the caller to parse. Nothing here reads from stdin:
a key passphrase is typed by the person in a separate window this script
opens, and never passes through this process.

    uv run --managed-python --python 3.12 setup/t4setup.py <subcommand>

    check
    key --passphrase yes|no     then:  key --wait
    portal [--no-browser]
    connect --user ux12345      (or --existing ALIAS)
    install --group G --workdir DIR [--create-workdir] [--allow]
    smoke                       (or --watch JOBID)
    cleanup

Values chosen in one step are kept in setup/state.json for the next ones.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import webbrowser
from pathlib import Path

LOGIN_HOST = "login.t4.gsic.titech.ac.jp"
HOST_PATTERN = re.compile(r"^login\d?\.t4\.(gsic\.titech|cii\.isct)\.ac\.jp$", re.I)
PORTAL_URL = "https://portal.t4.gsic.titech.ac.jp/"
TOOLKIT = "https://github.com/tomaunno/hpc-job-toolkit/archive/refs/tags/v0.2.0.zip"
PYTHON = "3.12"                       # uv-managed; SETUP.md runs this script on the same version
CHECK_JOB = "t4check.sh"
CHECK_DIR = "tsubame-setup-check"     # under the workdir on TSUBAME; removed by `cleanup`
# hpcjob subcommands that may run without a prompt when the person agrees (`install --allow`).
# `run` (any command on the login node) and `cancel` keep asking.
ALLOWED_SUBCOMMANDS = ("sync", "submit", "status", "log", "watch", "pull", "push", "usage")
AGENT_LABEL = "Claude"                # "Codex" with --agent codex; named in the passphrase window

REPO = Path(__file__).resolve().parent.parent
HOME = Path.home()
SSH_DIR = HOME / ".ssh"
IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"


# ---- small helpers ----------------------------------------------------------------
def result(status: str, **data) -> None:
    print("RESULT " + json.dumps({"status": status, **data}, ensure_ascii=True))


def run(argv: list[str], timeout: int | None = 120, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **kw)


def posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def openssh(tool: str) -> str:
    """Path of ssh / scp / ssh-keygen / ssh-add.

    On Windows always the built-in OpenSSH, never Git Bash's copy: only the
    built-in one talks to the Windows ssh-agent, and keys it creates get the
    file permissions it later insists on.
    """
    if IS_WIN:
        p = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / f"{tool}.exe"
        if p.exists():
            return str(p)
    return shutil.which(tool) or tool


class State:
    def __init__(self, path: Path):
        self.path = path
        self.data: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def __getitem__(self, k):
        return self.data[k]

    def get(self, k, default=None):
        return self.data.get(k, default)

    def set(self, **kw) -> None:
        self.data.update({k: v for k, v in kw.items() if v is not None})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def claude_dir(st: State) -> Path:
    return Path(st.get("claude_dir") or os.environ.get("CLAUDE_CONFIG_DIR") or HOME / ".claude")


def skills_root(st: State) -> Path:
    """Where the agent driving this setup loads skills from: Claude Code reads
    ~/.claude/skills, Codex (ChatGPT) reads ~/.agents/skills (same SKILL.md format)."""
    if st.get("skills_dir"):            # testing
        return Path(st["skills_dir"])
    return HOME / ".agents" / "skills" if st.get("agent") == "codex" else claude_dir(st) / "skills"


def key_path(st: State) -> Path:
    return Path(st.get("key_path") or SSH_DIR / st.get("key_name", "id_ed25519_tsubame"))


def undo_msys(path: str) -> str:
    """Git Bash turns '/gs/fs/x' into 'C:/.../Git/gs/fs/x' before Python sees it; undo that."""
    if not IS_WIN or "MSYSTEM" not in os.environ or not shutil.which("cygpath"):
        return path
    root = run([shutil.which("cygpath"), "-m", "/"]).stdout.strip().rstrip("/")
    norm = path.replace("\\", "/")
    return norm[len(root):] if root and norm.lower().startswith(root.lower() + "/") else path


def find_uv() -> str | None:
    for c in (os.environ.get("UV"), shutil.which("uv"),
              str(HOME / ".local" / "bin" / ("uv.exe" if IS_WIN else "uv"))):
        if c and Path(c).exists():
            return c
    return None


def hpcjob_path() -> str | None:
    uv = find_uv()
    if uv:
        r = run([uv, "tool", "dir", "--bin"])
        if r.returncode == 0:
            p = Path(r.stdout.strip()) / ("hpcjob.exe" if IS_WIN else "hpcjob")
            if p.exists():
                return str(p)
    return shutil.which("hpcjob")


# ---- ~/.ssh/config -----------------------------------------------------------------
BLOCK_MARK = "# TSUBAME 4.0 (added by tsubame-claude-setup"
SECTION_START = re.compile(r"^\s*(host|match)(\s|=)", re.I)


def ensure_ssh_dir() -> None:
    # On Windows, Python 3.13+ turns mode=0o700 into an owner-only ACL that OpenSSH
    # rejects ("Bad owner or permissions"); the profile's inherited ACL is what it expects.
    SSH_DIR.mkdir(exist_ok=True) if IS_WIN else SSH_DIR.mkdir(mode=0o700, exist_ok=True)


def read_config() -> tuple[str, bool]:
    """Text of ~/.ssh/config and whether it starts with a UTF-8 BOM (kept on write)."""
    cfg = SSH_DIR / "config"
    if not cfg.exists():
        return "", False
    raw = cfg.read_bytes()
    return raw.decode("utf-8-sig", errors="replace"), raw.startswith(b"\xef\xbb\xbf")


def write_config(text: str, bom: bool) -> None:
    ensure_ssh_dir()
    cfg = SSH_DIR / "config"
    cfg.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    if not IS_WIN:
        os.chmod(cfg, 0o600)


def ssh_hosts() -> dict[str, dict[str, str]]:
    """Host blocks of ~/.ssh/config: {alias: {option(lowercase): value}} (Include not followed)."""
    hosts: dict[str, dict[str, str]] = {}
    current: list[str] = []
    for raw in read_config()[0].splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        key, val = parts[0].lower(), (parts[1] if len(parts) > 1 else "").strip().strip('"')
        if key == "host":
            current = val.split()
            for h in current:
                hosts.setdefault(h, {})
        elif key == "match":
            current = []
        else:
            for h in current:
                hosts[h].setdefault(key, val)
    return hosts


SSH_G_EXTRA: list[str] = []      # tests point `ssh -G` at a sample file with ["-F", path]


def effective(alias: str) -> dict[str, list[str]]:
    """What ssh itself will use for `alias` (`ssh -G`), Include and `Host *` applied."""
    r = run([openssh("ssh"), *SSH_G_EXTRA, "-G", alias], timeout=30)
    out: dict[str, list[str]] = {}
    if r.returncode != 0:            # e.g. a config file with bad permissions: unknown, not "free"
        return {"_error": [r.stderr.strip()[-300:]]}
    for line in r.stdout.splitlines():
        k, _, v = line.partition(" ")
        out.setdefault(k.lower(), []).append(v.strip())
    return out


def name_is_free(alias: str) -> bool:
    # Unconfigured names resolve to themselves; anything else is set somewhere (maybe in an Include).
    # When ssh -G itself fails, only the file's own Host lines can be checked.
    eff = effective(alias)
    return alias not in ssh_hosts() and ("_error" in eff or eff.get("hostname") == [alias])


def new_alias(st: State) -> str:
    """Alias for a new setup: the one chosen earlier, else the first free name.

    A name already used by another Host block (even an old TSUBAME one that no
    longer works) is never reused, so the new key cannot end up behind an old block.
    """
    if st.get("alias"):
        return st["alias"]
    alias = free_alias()
    st.set(alias=alias)
    return alias


def free_alias() -> str:
    return next(a for a in ("tsubame", "tsubame-t4", *(f"tsubame-t4-{i}" for i in range(2, 99)))
                if name_is_free(a))


def tsubame_aliases() -> list[dict[str, str]]:
    return [{"alias": a, "user": o.get("user", ""), "identityfile": o.get("identityfile", "")}
            for a, o in ssh_hosts().items()
            if HOST_PATTERN.match(o.get("hostname", a)) and not any(c in a for c in "*?!")]


def drop_own_block(alias: str) -> bool:
    """Remove a Host block this script wrote earlier (e.g. after a mistyped login name)."""
    text, bom = read_config()
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines[:-1]):
        if line.startswith(BLOCK_MARK) and lines[i + 1].split() == ["Host", alias]:
            end = i + 2
            while end < len(lines) and lines[end].startswith((" ", "\t")):
                end += 1
            if end < len(lines) and not lines[end].strip():       # the blank line add_host_block put after it
                end += 1
            elif i > 0 and not lines[i - 1].strip():               # or before it, at the end of the file
                i -= 1
            write_config("".join(lines[:i] + lines[end:]), bom)
            return True
    return False


def add_host_block(alias: str, user: str, key: Path) -> str:
    """Write the Host block. Returns 'added' | 'replaced' | 'present' | 'taken'.

    The block goes just before the first Host/Match line: ssh keeps the first value
    it reads for each option, so a later `Host *` cannot override it, while global
    lines at the top of the file stay global.
    """
    existing = ssh_hosts().get(alias)
    replaced = False
    if existing is not None:
        if (HOST_PATTERN.match(existing.get("hostname", "")) and existing.get("user") == user
                and existing.get("identityfile", "").replace("\\", "/") == posix(key)):
            return "present"
        if not drop_own_block(alias):
            return "taken"
        replaced = True
    block = [f"{BLOCK_MARK}, {dt.date.today()})",
             f"Host {alias}",
             f"    HostName {LOGIN_HOST}",
             f"    User {user}",
             f'    IdentityFile "{posix(key)}"',
             "    IdentitiesOnly yes",
             "    ServerAliveInterval 30",
             "    ServerAliveCountMax 4",
             "    StrictHostKeyChecking accept-new"]
    if IS_MAC:   # passphrase comes from the login keychain, also after a reboot
        block[3:3] = ["    IgnoreUnknown UseKeychain", "    UseKeychain yes", "    AddKeysToAgent yes"]
    text, bom = read_config()
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith(("\n", "\r")):
        lines[-1] += nl
    at = next((i for i, l in enumerate(lines) if SECTION_START.match(l)), len(lines))
    if at < len(lines):
        while at > 0 and lines[at - 1].lstrip().startswith("#"):   # comments above a Host line belong to it
            at -= 1
    new = nl.join(block) + nl
    if at < len(lines):
        new += nl                                   # blank line between our block and the next one
    elif lines and lines[-1].strip():
        new = nl + new                              # appended after global lines only
    write_config("".join(lines[:at]) + new + "".join(lines[at:]), bom)
    return "replaced" if replaced else "added"


def shadowed_by(alias: str, user: str, key: Path) -> list[str]:
    """Settings that ssh resolves differently from our block (empty when ours wins)."""
    eff = effective(alias)
    if "_error" in eff:              # ssh cannot read the config at all; `probe` reports why
        return []
    problems = []
    if not HOST_PATTERN.match((eff.get("hostname") or [""])[0]):
        problems.append(f"hostname={eff.get('hostname')}")
    if (eff.get("user") or [""])[0] != user:
        problems.append(f"user={eff.get('user')}")
    norm = lambda p: os.path.normcase(os.path.expanduser(p.strip('"'))).replace("\\", "/")
    if norm(str(key)) not in {norm(p) for p in eff.get("identityfile", [])}:
        problems.append(f"identityfile={eff.get('identityfile')}")
    if (eff.get("stricthostkeychecking") or [""])[0] not in ("accept-new", "no", "false"):
        problems.append(f"stricthostkeychecking={eff.get('stricthostkeychecking')}")
    return problems


# ---- ssh-agent -----------------------------------------------------------------------
def windows_agent() -> dict[str, str]:
    r = run(["powershell.exe", "-NoProfile", "-Command",
             "$s = Get-Service ssh-agent -ErrorAction SilentlyContinue; "
             "if ($s) { \"$($s.Status) $($s.StartType)\" } else { 'Missing Missing' }"])
    status, _, start = (r.stdout.strip() or "Unknown Unknown").partition(" ")
    return {"status": status, "start_type": start}


def agent_has(key: Path) -> bool:
    fp = run([openssh("ssh-keygen"), "-lf", str(key) + ".pub"])
    parts = fp.stdout.split()           # "256 SHA256:... comment (ED25519)"
    return fp.returncode == 0 and len(parts) > 1 and parts[1] in run([openssh("ssh-add"), "-l"]).stdout


# ---- check ---------------------------------------------------------------------------
def cmd_check(st: State, ns) -> None:
    keys = sorted(p.name[:-4] for p in SSH_DIR.glob("*.pub")) if SSH_DIR.exists() else []
    info = {
        "os": "windows" if IS_WIN else "mac" if IS_MAC else platform.system().lower(),
        # Windows: only the built-in OpenSSH counts (Git Bash's ssh cannot use the Windows ssh-agent)
        "ssh": openssh("ssh"),
        "ssh_found": (Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "OpenSSH" / "ssh.exe").exists()
                     if IS_WIN else shutil.which("ssh") is not None,
        "tsubame_aliases": tsubame_aliases(),
        "alias_for_new_setup": st.get("alias") or free_alias(),
        "keys": keys,
        "uv": find_uv(), "hpcjob": hpcjob_path(),
        "agent": st.get("agent", "claude"),
        "skills_root": str(skills_root(st)),
        "skill_installed": (skills_root(st) / "tsubame" / "SKILL.md").exists(),
    }
    if IS_WIN:
        info["windows_ssh_agent"] = windows_agent()
    for k, v in info.items():
        print(f"{k}: {v}")
    result("ok", **info)


# ---- key -----------------------------------------------------------------------------
def write_windows_window(script: Path, key: Path, create: bool, marker: Path, comment: str) -> None:
    keygen, add = openssh("ssh-keygen"), openssh("ssh-add")
    body = [
        "$Host.UI.RawUI.WindowTitle = 'TSUBAME の鍵'",
        "Write-Host ''",
    ]
    if create:
        body += [
            "Write-Host 'TSUBAME に入るための鍵を作ります。' -ForegroundColor Cyan",
            "Write-Host 'パスフレーズ(鍵にかけるパスワード)を決めて打ち、Enter を押してください。'",
            "Write-Host '打っても画面には何も出ませんが、ちゃんと入っています。同じものをもう一度聞かれたら、もう一度打って Enter。'",
            "Write-Host '(英語で Enter passphrase と出ます。それが入力する場所です)'",
            "Write-Host ''",
            f"& '{keygen}' -t ed25519 -f '{key}' -C '{comment}'",
            f"if ($LASTEXITCODE -ne 0) {{ Write-Host '鍵を作れませんでした。この窓を閉じて {AGENT_LABEL} に伝えてください。' -ForegroundColor Red;"
            f" Set-Content -Path '{marker}' -Value 'failed'; Read-Host 'Enter で閉じます'; exit 1 }}",
            "Write-Host ''",
        ]
    body += [
        "Write-Host 'パソコンにパスフレーズを覚えさせます。いま決めたパスフレーズをもう一度打って Enter を押してください。' -ForegroundColor Cyan",
        "Write-Host '(英語で Enter passphrase と出ます)'",
        f"& '{add}' '{key}'",
        f"if ($LASTEXITCODE -ne 0) {{ Write-Host 'うまくいきませんでした。この窓を閉じて {AGENT_LABEL} に伝えてください。' -ForegroundColor Red;"
        f" Set-Content -Path '{marker}' -Value 'failed'; Read-Host 'Enter で閉じます'; exit 1 }}",
        f"Set-Content -Path '{marker}' -Value 'done'",
        "Write-Host ''",
        f"Write-Host '終わりました。この窓を閉じて、{AGENT_LABEL} の画面に戻ってください。' -ForegroundColor Green",
        "Read-Host 'Enter で閉じます'",
    ]
    # Windows PowerShell 5.1 reads a script as UTF-8 only when it has a BOM.
    script.write_text("\r\n".join(body) + "\r\n", encoding="utf-8-sig")


def write_mac_window(script: Path, key: Path, create: bool, marker: Path, comment: str) -> None:
    body = ["#!/bin/bash", "clear", "echo"]
    if create:
        body += [
            "echo 'TSUBAME に入るための鍵を作ります。'",
            "echo 'パスフレーズ(鍵にかけるパスワード)を決めて打ち、return を押してください。'",
            "echo '打っても画面には何も出ませんが、ちゃんと入っています。同じものをもう一度聞かれたら、もう一度打って return。'",
            "echo '(英語で Enter passphrase と出ます。それが入力する場所です)'", "echo",
            f"if ! ssh-keygen -t ed25519 -f '{key}' -C '{comment}'; then echo failed > '{marker}';"
            f" echo '鍵を作れませんでした。この窓を閉じて {AGENT_LABEL} に伝えてください。'; exit 1; fi", "echo",
        ]
    body += [
        "echo 'パソコン(キーチェーン)にパスフレーズを覚えさせます。いま決めたパスフレーズをもう一度打って return を押してください。'",
        f"if ssh-add --apple-use-keychain '{key}' 2>/dev/null || ssh-add -K '{key}'; then echo done > '{marker}';"
        f" else echo failed > '{marker}'; echo 'うまくいきませんでした。この窓を閉じて {AGENT_LABEL} に伝えてください。'; exit 1; fi",
        "echo", f"echo '終わりました。この窓を閉じて、{AGENT_LABEL} の画面に戻ってください。'",
    ]
    script.write_text("\n".join(body) + "\n", encoding="utf-8")
    os.chmod(script, 0o755)


def key_unlocked(key: Path) -> bool:
    """True when the private key has no passphrase."""
    return run([openssh("ssh-keygen"), "-y", "-P", "", "-f", str(key)]).returncode == 0


def fresh_key_path() -> Path:
    return next(p for p in (SSH_DIR / f"id_ed25519_tsubame_{i}" for i in range(2, 99))
                if not p.exists() and not Path(str(p) + ".pub").exists())


def cmd_key(st: State, ns) -> None:
    marker = st.path.parent / "key_window.done"
    if ns.wait:
        return key_wait(st, key_path(st), marker, ns.minutes)
    if not ns.passphrase:
        raise SystemExit("key: pass --passphrase yes|no, or --wait")
    key = fresh_key_path() if ns.new else key_path(st)
    comment = f"tsubame-{dt.date.today():%Y%m%d}"
    have_key = key.exists() and Path(str(key) + ".pub").exists()

    if ns.passphrase == "no" and have_key and not key_unlocked(key):
        # A key with a passphrase is already there (e.g. the window path was tried first).
        if st.get("key_created") == str(key):          # made by this setup: replace it
            key.unlink()
            Path(str(key) + ".pub").unlink()
        else:                                          # someone's own key: leave it, use a new name
            key = fresh_key_path()
        have_key = False
    st.set(key_path=str(key), passphrase=ns.passphrase)

    if ns.passphrase == "no":
        if not have_key:
            ensure_ssh_dir()
            r = run([openssh("ssh-keygen"), "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", comment])
            if r.returncode != 0:
                return result("error", reason="keygen_failed", detail=r.stderr.strip())
            st.set(key_created=str(key))
        print(f"key ready (no passphrase): {key}")
        return result("ok", key=str(key), created=not have_key, comment=pub_comment(key))

    # passphrase: the person types it in a window of their own
    if IS_WIN:
        agent = windows_agent()
        if agent["status"] != "Running":
            helper = REPO / "setup" / "enable-ssh-agent.cmd"
            # one string: a list would quote "/select,<path with spaces>" as a whole, which Explorer ignores
            subprocess.Popen(f'explorer.exe /select,"{helper}"')
            print(f"Windows ssh-agent is {agent['status']} ({agent['start_type']}). Opened Explorer at {helper}")
            return result("need_agent", helper=str(helper), agent=agent)
    elif not IS_MAC:
        return result("unsupported", reason="passphrase window is implemented for Windows and macOS only",
                      manual=[f"ssh-keygen -t ed25519 -f {key} -C {comment}", f"ssh-add {key}"])
    remembered = agent_has(key) or (IS_MAC and marker.exists() and marker.read_text().strip() == "done")
    if have_key and remembered:
        print(f"key ready and remembered: {key}")
        return result("ok", key=str(key), created=False, comment=pub_comment(key))

    ensure_ssh_dir()
    marker.unlink(missing_ok=True)
    if not have_key:
        st.set(key_created=str(key))
    if IS_WIN:
        script = st.path.parent / "key_window.ps1"
        write_windows_window(script, key, not have_key, marker, comment)
        proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                creationflags=subprocess.CREATE_NEW_CONSOLE)
        st.set(window_pid=proc.pid)
    else:
        script = st.path.parent / "key_window.command"
        write_mac_window(script, key, not have_key, marker, comment)
        subprocess.Popen(["open", "-a", "Terminal", str(script)])
    print("opened a window for the passphrase; run `key --wait` next")
    return result("window_opened", key=str(key), creating=not have_key)


def windows_pid_alive(pid: int) -> bool:
    r = run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"])
    return f'"{pid}"' in r.stdout


def key_wait(st: State, key: Path, marker: Path, minutes: float) -> None:
    deadline = time.monotonic() + minutes * 60
    while time.monotonic() < deadline:
        if marker.exists():
            state = marker.read_text(encoding="utf-8", errors="replace").strip()
            if state != "done":
                return result("error", reason="window_failed")
            if IS_WIN and not agent_has(key):
                return result("error", reason="not_in_agent")
            print(f"key ready and remembered: {key}")
            return result("ok", key=str(key), comment=pub_comment(key))
        if IS_WIN and st.get("window_pid") and not windows_pid_alive(st["window_pid"]):
            return result("window_closed", hint="the window was closed before it finished; run `key --passphrase yes` again")
        time.sleep(3)
    return result("still_waiting", hint="the window is still open; run `key --wait` again")


def pub_comment(key: Path) -> str:
    """Last field of the public key, e.g. 'tsubame-20261021': how the key shows up in the portal list."""
    pub = Path(str(key) + ".pub")
    parts = pub.read_text(encoding="ascii", errors="replace").split() if pub.exists() else []
    return parts[2] if len(parts) > 2 else ""


# ---- portal --------------------------------------------------------------------------
def cmd_portal(st: State, ns) -> None:
    pub = Path(str(key_path(st)) + ".pub")
    if not pub.exists():
        return result("error", reason="no_public_key", path=str(pub))
    text = pub.read_text(encoding="ascii").strip()
    try:
        if IS_WIN:
            copied = run(["clip.exe"], input=text).returncode == 0
        elif IS_MAC:
            copied = run(["pbcopy"], input=text).returncode == 0
        else:
            copied = False
    except OSError:
        copied = False
    if not ns.no_browser:
        webbrowser.open(PORTAL_URL)
    print(text)
    result("ok", public_key=text, comment=pub_comment(key_path(st)), clipboard=copied, portal_url=PORTAL_URL,
           browser_opened=not ns.no_browser)


# ---- connect -------------------------------------------------------------------------
REMOTE_PROBE = r"""echo T4OK; echo "USER=$(id -un)"; echo "HOME=$HOME";
t4-user-info group point 2>/dev/null | awk '$1 ~ /^[0-9]+$/ {print "POINT", $2, $NF}';
for g in $(id -Gn); do for b in /gs/fs /gs/bs; do [ -d "$b/$g" ] || continue; echo "GDIR $g $b/$g";
find "$b/$g" -mindepth 1 -maxdepth 1 -type d -user "$(id -un)" 2>/dev/null | sed "s|^|OWN $g |"; done; done"""


def diagnose(stderr: str) -> str:
    s = stderr.lower()
    if "permission denied" in s:
        return "key_not_accepted"        # not registered yet, wrong login name, or agent lost the key
    if "could not resolve" in s or "timed out" in s or "network is unreachable" in s:
        return "network"
    if "host key verification failed" in s or "remote host identification has changed" in s:
        return "host_key"
    if "bad owner or permissions" in s or "unprotected private key" in s:
        return "file_permissions"
    return "unknown"


def probe(alias: str) -> dict:
    r = run([openssh("ssh"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", alias, REMOTE_PROBE], timeout=90)
    if "T4OK" not in r.stdout:
        noise = ("post-quantum", "store now", "upgraded", "openssh.com/pq", "conda", "your_shell", "please do")
        err = "\n".join(l for l in r.stderr.splitlines() if l.strip() and not any(n in l.lower() for n in noise))
        return {"ok": False, "reason": diagnose(r.stderr), "detail": err[-800:]}
    groups: dict[str, dict] = {}
    info: dict = {"ok": True}
    for line in r.stdout.splitlines():
        if line.startswith("USER="):
            info["user"] = line[5:]
        elif line.startswith("HOME="):
            info["home"] = line[5:]
        elif line.startswith(("POINT ", "GDIR ", "OWN ")):
            tag, g, val = (line.split(" ", 2) + [""])[:3]
            e = groups.setdefault(g, {"name": g, "points": None, "group_dirs": [], "own_dirs": []})
            if tag == "POINT":
                e["points"] = val
            elif tag == "GDIR":
                e["group_dirs"].append(val)
            else:
                e["own_dirs"].append(val)
    info["groups"] = [g for g in groups.values() if g["points"] is not None or g["group_dirs"]]
    if info.get("home"):
        info["work"] = info["home"].replace("/home/", "/work/", 1)
    return info


def cmd_connect(st: State, ns) -> None:
    if ns.existing:
        alias = ns.existing
        user = ssh_hosts().get(alias, {}).get("user", "")
    else:
        if not ns.user:
            raise SystemExit("connect: pass --user LOGIN_NAME or --existing ALIAS")
        user = ns.user.strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{2,31}", user):
            return result("error", reason="bad_login_name", user=user)
        alias = new_alias(st)
        state = add_host_block(alias, user, key_path(st))
        if state == "taken":
            return result("error", reason="alias_taken", alias=alias,
                          hint="pass another name with the global --alias option")
        print(f"~/.ssh/config: Host {alias} {state}")
        shadow = shadowed_by(alias, user, key_path(st))
        if shadow:      # another line of the config (an Include, a Host * above ...) wins over ours
            return result("error", alias=alias, reason="alias_shadowed", detail="; ".join(shadow),
                          hint="ssh -G <alias> shows what ssh really uses; find the line in ~/.ssh/config that sets it")
    info = probe(alias)
    if not info["ok"]:
        print(f"connection failed: {info['reason']}\n{info['detail']}")
        return result("error", alias=alias, reason=info["reason"], detail=info["detail"])
    st.set(alias=alias, user=info.get("user") or user, home_remote=info.get("home"),
           work_remote=info.get("work"), groups=info["groups"])
    for g in info["groups"]:
        print(f"group {g['name']}: points={g['points']} own_dirs={g['own_dirs']} group_dirs={g['group_dirs']}")
    status = "ok" if info["groups"] else "no_group"     # logged in, but not a member of any TSUBAME group yet
    result(status, alias=alias, user=info.get("user"), home=info.get("home"), work=info.get("work"),
           groups=info["groups"])


# ---- install -------------------------------------------------------------------------
def render(template: str, values: dict[str, str]) -> str:
    out = template
    for k, v in values.items():
        out = out.replace("{{" + k + "}}", v)
    left = re.findall(r"\{\{[A-Z_]+\}\}", out)
    if left:
        raise SystemExit(f"template placeholders left unfilled: {sorted(set(left))}")
    return out


def permission_rules() -> list[str]:
    tools = ("Bash", "PowerShell") if IS_WIN else ("Bash",)
    return [f"{t}(hpcjob {s} *)" for t in tools for s in ALLOWED_SUBCOMMANDS]


def add_permissions(settings: Path) -> dict:
    untouched = {"changed": False, "reason": "settings.json not readable; left untouched"}
    data: dict = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return {**untouched, "detail": str(exc)}
    if not isinstance(data, dict) or not isinstance(data.setdefault("permissions", {}), dict) \
            or not isinstance(data["permissions"].setdefault("allow", []), list):
        return {**untouched, "detail": "unexpected shape (permissions.allow is not a list)"}
    allow = data["permissions"]["allow"]
    new = [r for r in permission_rules() if r not in allow]
    if not new:
        return {"changed": False, "reason": "already present"}
    backup = None
    if settings.exists():
        backup = settings.with_name(f"settings.json.bak-{dt.datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(settings, backup)
    allow.extend(new)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"changed": True, "added": new, "backup": str(backup) if backup else None}


def cmd_install(st: State, ns) -> None:
    alias, user = st.get("alias"), st.get("user")
    if not alias or not user or not st.get("home_remote"):
        return result("error", reason="run `connect` first")
    group, workdir = ns.group, undo_msys(ns.workdir).rstrip("/")
    known = {g["name"] for g in st.get("groups", [])}
    if group not in known:
        return result("error", reason="unknown_group", group=group, known=sorted(known))
    if not workdir.startswith("/"):
        return result("error", reason="workdir_not_absolute", workdir=workdir)

    ssh, scp = openssh("ssh"), openssh("scp")
    test = f"test -d '{workdir}' && test -w '{workdir}' && echo WD_OK"
    if ns.create_workdir:
        test = f"mkdir -p '{workdir}' && " + test
    r = run([ssh, "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", alias, test], timeout=90)
    if "WD_OK" not in r.stdout:
        return result("error", reason="workdir_unusable", workdir=workdir,
                      hint="pass --create-workdir to create it" if not ns.create_workdir else "",
                      detail=r.stderr.strip()[-400:])
    st.set(group=group, workdir=workdir)

    uv = find_uv()
    if not uv:
        return result("error", reason="uv_not_found")
    toolkit = st.get("toolkit", TOOLKIT)
    # The same uv-managed Python as `uv run --managed-python --python 3.12` in SETUP.md,
    # so the setup leaves one Python behind, not two.
    r = run([uv, "tool", "install", "--force", "--managed-python", "--python", PYTHON, toolkit], timeout=900)
    hp = hpcjob_path()
    if r.returncode != 0 or not hp:
        return result("error", reason="hpcjob_install_failed", detail=(r.stderr or r.stdout).strip()[-800:])
    print(f"hpcjob installed: {hp}")
    st.set(hpcjob=hp)

    values = {
        "ALIAS": alias, "USER": user, "GROUP": group, "WORKDIR": workdir,
        "HOME_REMOTE": st["home_remote"], "WORK_REMOTE": st.get("work_remote", ""),
        "SSH": posix(Path(ssh)) if IS_WIN else ssh, "SCP": posix(Path(scp)) if IS_WIN else scp,
        "SSH_ADD": posix(Path(openssh("ssh-add"))) if IS_WIN else openssh("ssh-add"),
        "HPCJOB": posix(Path(hp)), "DATE": f"{dt.date.today()}",
        "OS": "Windows" if IS_WIN else "macOS" if IS_MAC else platform.system(),
    }
    dest = skills_root(st) / "tsubame"
    if dest.exists():
        skill = dest / "SKILL.md"
        ours = skill.exists() and "tsubame-claude-setup" in skill.read_text(encoding="utf-8", errors="replace")
        if ours:                    # an earlier run of this setup: rewrite it
            shutil.rmtree(dest)
        else:                       # someone else's skill of the same name: keep it, outside skills/ so it stops loading
            backup = skills_root(st).parent / f"tsubame-skill-backup-{dt.datetime.now():%Y%m%d%H%M%S}"
            shutil.move(str(dest), str(backup))
            print(f"an existing 'tsubame' skill was moved to {backup}")
    dest.mkdir(parents=True)
    for name in ("SKILL.md", "reference.md", "hpcjob.toml.example"):
        text = (REPO / "skill" / name).read_text(encoding="utf-8")
        (dest / name).write_text(render(text, values), encoding="utf-8", newline="\n")
    if IS_WIN:   # so the fix in reference.md still works after the setup folder is deleted
        for helper in ("enable-ssh-agent.cmd", "enable-ssh-agent.ps1"):
            shutil.copy2(REPO / "setup" / helper, dest / helper)
    print(f"skill written: {dest}")

    if st.get("agent") == "codex":      # Codex asks by its own approval mode; there is no allow list to add to
        perms = {"changed": False, "reason": "codex: approvals follow the Codex approval mode"}
    elif ns.allow:
        perms = add_permissions(claude_dir(st) / "settings.json")
    else:
        perms = {"changed": False, "reason": "not requested"}
    st.set(settings_backup=perms.get("backup"))
    print(f"permissions: {perms}")
    result("ok", hpcjob=hp, skill=str(dest), permissions=perms, group=group, workdir=workdir)


# ---- smoke ---------------------------------------------------------------------------
def cmd_smoke(st: State, ns) -> None:
    hp = st.get("hpcjob") or hpcjob_path()
    if not hp or not st.get("workdir"):
        return result("error", reason="run `install` first")
    proj = st.path.parent / "check-project"
    if not ns.watch:
        (proj / "jobs").mkdir(parents=True, exist_ok=True)
        (proj / "src").mkdir(exist_ok=True)
        example = (skills_root(st) / "tsubame" / "hpcjob.toml.example").read_text(encoding="utf-8")
        toml = example.replace(f'{st["workdir"]}/PROJECT', f'{st["workdir"]}/{CHECK_DIR}')
        (proj / "hpcjob.toml").write_text(toml, encoding="utf-8", newline="\n")
        (proj / "jobs" / CHECK_JOB).write_text(
            "#!/bin/sh\n#$ -cwd\n"
            "echo \"T4CHECK start $(date '+%F %T') host=$(hostname) user=$(id -un) cores=$(nproc)\"\n"
            "echo T4CHECK OK\n", encoding="utf-8", newline="\n")
        r = run([hp, "submit", CHECK_JOB, "-r", "cpu_4=1", "-t", "0:10:00", "-n", "t4check"], timeout=300, cwd=proj)
        print(r.stdout.strip())
        m = re.search(r"JOBID=(\d+)", r.stdout)
        if r.returncode != 0 or not m:
            return result("error", reason="submit_failed", detail=(r.stderr or r.stdout).strip()[-800:])
        jobid = m.group(1)
        st.set(check_jobid=jobid)
    else:
        jobid = ns.watch
    r = run([hp, "watch", jobid, "--max-minutes", "8"], timeout=600, cwd=proj)
    print(r.stdout.strip()[-1500:])
    if r.returncode == 3:
        return result("still_running", jobid=jobid, hint=f"run `smoke --watch {jobid}` again")
    if r.returncode == 0 and "T4CHECK OK" in r.stdout:
        return result("ok", jobid=jobid)
    return result("error", reason="check_job_failed", jobid=jobid, detail=(r.stderr or r.stdout).strip()[-800:])


# ---- cleanup -------------------------------------------------------------------------
def cmd_cleanup(st: State, ns) -> None:
    """Remove what only the setup needed. The setup folder itself is deleted by the
    caller from outside it (a running process cannot remove its own folder on Windows)."""
    removed, kept = [], []
    if st.get("alias") and st.get("workdir"):
        d = f'{st["workdir"]}/{CHECK_DIR}'
        # Only a folder holding this script's check job is removed.
        test = (f"d='{d}'; if [ -f \"$d/auto/jobs/{CHECK_JOB}\" ]; then rm -rf \"$d\" && echo GONE; "
                f"elif [ -e \"$d\" ]; then echo FOREIGN; else echo ABSENT; fi")
        r = run([openssh("ssh"), "-o", "BatchMode=yes", "-o", "ConnectTimeout=20", st["alias"], test], timeout=120)
        if "GONE" in r.stdout:
            removed.append(f"tsubame:{d}")
        elif "FOREIGN" in r.stdout:
            kept.append(f"tsubame:{d} (not created by this setup)")
        elif "ABSENT" not in r.stdout:
            kept.append(f"tsubame:{d} (could not reach TSUBAME: {r.stderr.strip()[-200:]})")
    backup = st.get("settings_backup")
    if backup and Path(backup).exists():
        try:
            data = json.loads((claude_dir(st) / "settings.json").read_text(encoding="utf-8"))
            good = all(rule in data["permissions"]["allow"] for rule in permission_rules())
        except (OSError, ValueError, KeyError, TypeError):
            good = False
        if good:                    # the edited settings.json reads back fine, so the copy is not needed
            Path(backup).unlink()
            removed.append(backup)
        else:
            kept.append(f"{backup} (settings.json did not read back as expected)")
    for line in removed:
        print(f"removed: {line}")
    for line in kept:
        print(f"kept: {line}")
    result("ok", removed=removed, kept=kept, setup_folder=str(REPO))


# ---- main ----------------------------------------------------------------------------
def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(errors="replace")
    p = argparse.ArgumentParser(prog="t4setup", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--state", default=str(REPO / "setup" / "state.json"), help="where chosen values are kept")
    p.add_argument("--alias", help="ssh alias to create (default: tsubame)")
    p.add_argument("--key-name", help="key file name in ~/.ssh (default: id_ed25519_tsubame)")
    p.add_argument("--agent", choices=("claude", "codex"),
                   help="the AI agent following SETUP.md: Claude Code (default) or Codex (ChatGPT)")
    p.add_argument("--claude-dir", help="Claude Code config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)")
    p.add_argument("--skills-dir", help="testing: write the skill here instead of the agent's skills folder")
    p.add_argument("--toolkit", help=f"hpc-job-toolkit source for `uv tool install` (default: {TOOLKIT})")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("check", help="report what is already set up")
    k = sub.add_parser("key", help="create the key (and remember its passphrase)")
    k.add_argument("--passphrase", choices=("yes", "no"))
    k.add_argument("--wait", action="store_true", help="wait for the passphrase window to finish")
    k.add_argument("--new", action="store_true", help="make a key under a new file name (e.g. passphrase forgotten)")
    k.add_argument("--minutes", type=float, default=8)
    po = sub.add_parser("portal", help="copy the public key and open the TSUBAME portal")
    po.add_argument("--no-browser", action="store_true", help="do not open the default browser")
    c = sub.add_parser("connect", help="add the ssh alias, test the login, list groups and folders")
    c.add_argument("--user", help="TSUBAME login name, e.g. ux12345")
    c.add_argument("--existing", metavar="ALIAS", help="use an ssh alias that is already configured")
    i = sub.add_parser("install", help="install hpcjob, write the agent skill, optionally allow hpcjob (Claude Code)")
    i.add_argument("--group", required=True)
    i.add_argument("--workdir", required=True)
    i.add_argument("--create-workdir", action="store_true")
    i.add_argument("--allow", action="store_true", help="let hpcjob sync/submit/status/log/watch/pull/push/usage run without a prompt")
    s = sub.add_parser("smoke", help="submit a tiny cpu_4 job and wait for it")
    s.add_argument("--watch", metavar="JOBID", help="keep waiting for an earlier check job")
    sub.add_parser("cleanup", help="remove the check job folder on TSUBAME and the settings backup")
    ns = p.parse_args()

    st = State(Path(ns.state))
    st.set(alias=ns.alias, key_name=ns.key_name, claude_dir=ns.claude_dir, toolkit=ns.toolkit,
           agent=ns.agent, skills_dir=ns.skills_dir)
    global AGENT_LABEL
    AGENT_LABEL = "Codex" if st.get("agent") == "codex" else "Claude"
    try:
        {"check": cmd_check, "key": cmd_key, "portal": cmd_portal, "connect": cmd_connect,
         "install": cmd_install, "smoke": cmd_smoke, "cleanup": cmd_cleanup}[ns.command](st, ns)
    except subprocess.TimeoutExpired as exc:     # the caller always gets a RESULT line to read
        result("error", reason="timeout", detail=f"{' '.join(map(str, exc.cmd))[:300]} took over {exc.timeout} s")
    except Exception as exc:
        result("error", reason="crash", detail=f"{type(exc).__name__}: {exc}"[-800:])
        raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
