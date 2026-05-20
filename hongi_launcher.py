"""
HONGI Launcher — Windows SSH launcher for Mac dev environment.
Single-file, tkinter-only, no third-party dependencies.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import font as tkfont

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
MAC_HOST = "100.79.34.119"
MAC_USER = "jeongjintaek"
PROJECT_PATH = "/Users/jeongjintaek/hongi"
SSH_TARGET = f"{MAC_USER}@{MAC_HOST}"

LOCK_PORT = 19283

# Colors
BG = "#0a0a0a"
PANEL = "#131313"
ACCENT = "#5fafff"
FG = "#e0e0e0"
MUTED = "#555555"
DANGER = "#ff4444"
OK = "#44ff88"
BORDER = "#2a2a2a"

# Button accent colors
CLR_VSCODE = "#4fc3f7"
CLR_SHELL = "#5fafff"
CLR_LLAMA = "#88cc88"
CLR_DANGER = "#ff6666"
CLR_GEMINI = "#ffca44"

WIN32 = sys.platform == "win32"


# ---------------------------------------------------------------------------
# Font helper
# ---------------------------------------------------------------------------
def _make_font(size: int, weight: str = "normal") -> tuple[str, int, str]:
    """Return (family, size, weight). Falls back to Consolas on non-Windows."""
    primary = "Cascadia Mono" if WIN32 else "Consolas"
    return (primary, size, weight)


# ---------------------------------------------------------------------------
# Subprocess helper
# ---------------------------------------------------------------------------
def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    flags: dict = {}
    if WIN32:
        flags["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    return subprocess.run(cmd, capture_output=True, text=True, **flags, **kwargs)


# ---------------------------------------------------------------------------
# Single-instance lock
# ---------------------------------------------------------------------------
_lock_socket: socket.socket | None = None


def acquire_lock() -> bool:
    """Try to bind LOCK_PORT. Returns True if this is the first instance."""
    global _lock_socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
        s.bind(("127.0.0.1", LOCK_PORT))
        s.listen(5)
        _lock_socket = s
        return True
    except OSError:
        return False


def signal_first_instance() -> None:
    """Send SHOW to the already-running instance."""
    try:
        with socket.create_connection(("127.0.0.1", LOCK_PORT), timeout=2) as s:
            s.sendall(b"SHOW")
    except OSError:
        pass


def listen_for_signals(app: "HongiLauncher") -> None:
    """Background thread: accept SHOW signals and bring window to front."""
    assert _lock_socket is not None
    while True:
        try:
            conn, _ = _lock_socket.accept()
            with conn:
                data = conn.recv(16)
            if data == b"SHOW":
                app.after(0, _bring_to_front, app)
        except OSError:
            break


def _bring_to_front(app: "HongiLauncher") -> None:
    app.deiconify()
    app.lift()
    app.focus_force()


# ---------------------------------------------------------------------------
# Prereq checks
# ---------------------------------------------------------------------------
def _tailscale_bin() -> str:
    """Return the tailscale CLI path, with OS-specific fallbacks."""
    import shutil
    if shutil.which("tailscale"):
        return "tailscale"
    if WIN32:
        for p in [
            r"C:\Program Files\Tailscale\tailscale.exe",
            os.path.expandvars(r"%PROGRAMFILES%\Tailscale\tailscale.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Programs\Tailscale\tailscale.exe"),
        ]:
            if os.path.isfile(p):
                return p
    else:
        mac_bin = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
        if os.path.isfile(mac_bin):
            return mac_bin
    return "tailscale"  # will raise FileNotFoundError downstream


def check_tailscale() -> tuple[bool, str]:
    try:
        result = _run([_tailscale_bin(), "status", "--json"], timeout=8)
        if result.returncode != 0:
            return False, f"tailscale status failed (exit {result.returncode})"
        if not result.stdout:
            return False, "Tailscale daemon not responding"
        data = json.loads(result.stdout)
        state = data.get("BackendState", "")
        if state == "Running":
            return True, "Tailscale OK"
        return False, f"Tailscale state: {state or 'unknown'}"
    except FileNotFoundError:
        return False, "Tailscale not installed"
    except (json.JSONDecodeError, ValueError):
        return False, "Tailscale output parse error"
    except subprocess.TimeoutExpired:
        return False, "Tailscale check timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def check_ssh_nopass() -> tuple[bool, str]:
    try:
        result = _run(
            [
                "ssh",
                "-o", "BatchMode=yes",
                "-o", "ConnectTimeout=5",
                "-o", "StrictHostKeyChecking=no",
                SSH_TARGET,
                "echo ok",
            ],
            timeout=12,
        )
        if "ok" in result.stdout:
            return True, "SSH OK"
        stderr = result.stderr.strip()
        return False, f"SSH auth failed: {stderr[:120]}" if stderr else "SSH auth failed"
    except FileNotFoundError:
        return False, "ssh command not found"
    except subprocess.TimeoutExpired:
        return False, "SSH check timed out"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


# ---------------------------------------------------------------------------
# Installer helpers
# ---------------------------------------------------------------------------
def _winget_available() -> bool:
    try:
        r = _run(["winget", "--version"], timeout=6)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def install_tailscale(log_cb) -> None:
    """Install Tailscale via winget with fallback to direct download."""
    threading.Thread(target=_install_tailscale_bg, args=(log_cb,), daemon=True).start()


def _install_tailscale_bg(log_cb) -> None:
    if _winget_available():
        log_cb("Running: winget install Tailscale.Tailscale ...")
        r = _run(["winget", "install", "Tailscale.Tailscale", "-e", "--silent"], timeout=120)
        if r.returncode == 0:
            log_cb("Tailscale installed via winget.")
            return
        log_cb(f"winget failed (exit {r.returncode}). Trying direct download...")
    else:
        log_cb("winget not available. Trying direct download...")

    # Fallback: direct installer
    url = "https://pkgs.tailscale.com/stable/tailscale-setup-latest.exe"
    dest = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "tailscale-setup.exe")
    log_cb(f"Downloading {url} ...")
    try:
        if WIN32:
            r = _run(
                ["powershell", "-NoProfile", "-Command",
                 f"Invoke-WebRequest -Uri '{url}' -OutFile '{dest}' -UseBasicParsing"],
                timeout=120,
            )
        else:
            log_cb(f"Manual download: {url}")
            return
        if r.returncode == 0:
            log_cb(f"Download complete. Launching installer: {dest}")
            if WIN32:
                os.startfile(dest)  # type: ignore[attr-defined]
        else:
            log_cb(f"Download failed. Please visit:\n{url}")
    except Exception as exc:  # noqa: BLE001
        log_cb(f"Error: {exc}\nManual download: {url}")


def install_windows_terminal(log_cb) -> None:
    threading.Thread(target=_install_wt_bg, args=(log_cb,), daemon=True).start()


def _install_wt_bg(log_cb) -> None:
    if _winget_available():
        log_cb("Running: winget install Microsoft.WindowsTerminal ...")
        r = _run(
            ["winget", "install", "Microsoft.WindowsTerminal", "-e", "--silent"],
            timeout=180,
        )
        if r.returncode == 0:
            log_cb("Windows Terminal installed via winget.")
            return
        log_cb(f"winget failed (exit {r.returncode}). Trying Add-AppxPackage fallback...")
    else:
        log_cb("winget not available. Trying Add-AppxPackage fallback...")

    # Fallback 1: re-register AppxPackage
    if WIN32:
        log_cb("Attempting Add-AppxPackage re-register...")
        r = _run(
            ["powershell", "-NoProfile", "-Command",
             "Get-AppxPackage Microsoft.WindowsTerminal | "
             "ForEach-Object { Add-AppxPackage -DisableDevelopmentMode -Register ($_.InstallLocation + '\\AppxManifest.xml') }"],
            timeout=60,
        )
        if r.returncode == 0:
            log_cb("Windows Terminal re-registered.")
            return

    # Fallback 2: GitHub API → latest .msixbundle
    log_cb("Fetching latest Windows Terminal release from GitHub...")
    try:
        api_url = "https://api.github.com/repos/microsoft/terminal/releases/latest"
        if WIN32:
            r = _run(
                ["powershell", "-NoProfile", "-Command",
                 f"(Invoke-RestMethod -Uri '{api_url}' -UseBasicParsing).assets | "
                 "Where-Object {{$_.name -like '*.msixbundle'}} | "
                 "Select-Object -First 1 -ExpandProperty browser_download_url"],
                timeout=30,
            )
            dl_url = r.stdout.strip()
        else:
            log_cb("Cannot auto-install outside Windows.")
            return

        if not dl_url:
            log_cb("Could not find .msixbundle. Visit: https://github.com/microsoft/terminal/releases")
            return

        dest = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "WindowsTerminal.msixbundle")
        log_cb(f"Downloading {dl_url} ...")
        r = _run(
            ["powershell", "-NoProfile", "-Command",
             f"Invoke-WebRequest -Uri '{dl_url}' -OutFile '{dest}' -UseBasicParsing"],
            timeout=300,
        )
        if r.returncode != 0:
            log_cb(f"Download failed.\nManual: {dl_url}")
            return

        log_cb(f"Installing {dest} ...")
        r = _run(
            ["powershell", "-NoProfile", "-Command",
             f"Add-AppxPackage -Path '{dest}'"],
            timeout=120,
        )
        if r.returncode == 0:
            log_cb("Windows Terminal installed via msixbundle.")
        else:
            log_cb(f"Installation failed (exit {r.returncode}).\nManual: {dl_url}")
    except Exception as exc:  # noqa: BLE001
        log_cb(f"Error: {exc}")


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------
def open_vscode() -> None:
    uri = (
        f"vscode://vscode-remote/ssh-remote+{SSH_TARGET}"
        f"/Users/jeongjintaek/hongi"
    )
    folder_uri = (
        f"vscode-remote://ssh-remote+{SSH_TARGET}"
        f"/Users/jeongjintaek/hongi"
    )

    # Stage 1: os.startfile (opens default handler)
    if WIN32:
        try:
            os.startfile(uri)  # type: ignore[attr-defined]
            return
        except Exception:
            pass

    # Stage 2: cmd /c start
    try:
        _run(["cmd", "/c", "start", "", uri], timeout=10)
        return
    except Exception:
        pass

    # Stage 3: code --folder-uri
    try:
        _run(["code", "--folder-uri", folder_uri], timeout=10)
        return
    except Exception:
        pass

    # Stage 4: code --remote
    try:
        _run(["code", "--remote", f"ssh-remote+{SSH_TARGET}", PROJECT_PATH], timeout=10)
    except Exception:
        pass


def open_shell() -> None:
    try:
        subprocess.Popen(["wt.exe", "new-tab", "--title", "Hong-Shell", "ssh", "hongi"])
    except FileNotFoundError:
        # wt.exe 없으면 PowerShell로 폴백
        subprocess.Popen(["powershell.exe", "-NoExit", "-Command", "ssh hongi"])


def open_llama() -> None:
    _run([
        "wt.exe", "new-tab", "--title", "Llama",
        "ssh", "-t", SSH_TARGET, "ollama run qwen3:8b",
    ])


def open_danger() -> None:
    cmd = f"cd {PROJECT_PATH} && ~/.local/bin/claude --dangerously-skip-permissions"
    _run(["wt.exe", "new-tab", "--title", "Claude", "ssh", "-t", SSH_TARGET, cmd])


def open_gemini() -> None:
    cmd = f"cd {PROJECT_PATH} && GEMINI_CLI_TRUST_WORKSPACE=true gemini"
    _run(["wt.exe", "new-tab", "--title", "Gemini", "ssh", "-t", SSH_TARGET, cmd])


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------
def _card(parent, **kwargs) -> tk.Frame:
    defaults = dict(bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
    defaults.update(kwargs)
    return tk.Frame(parent, **defaults)


class HoverButton(tk.Frame):
    """Button card with border-color hover effect."""

    def __init__(
        self,
        parent,
        label: str,
        sublabel: str,
        accent: str,
        command,
        **kwargs,
    ):
        super().__init__(
            parent,
            bg=PANEL,
            highlightbackground=BORDER,
            highlightthickness=1,
            cursor="hand2",
            **kwargs,
        )
        self._accent = accent
        self._command = command

        inner = tk.Frame(self, bg=PANEL, padx=14, pady=10)
        inner.pack(fill="x")

        lbl = tk.Label(
            inner,
            text=label,
            bg=PANEL,
            fg=accent,
            font=_make_font(11, "bold"),
            anchor="w",
        )
        lbl.pack(fill="x")

        sub = tk.Label(
            inner,
            text=sublabel,
            bg=PANEL,
            fg=MUTED,
            font=_make_font(9),
            anchor="w",
        )
        sub.pack(fill="x")

        # Bind hover + click to all children
        for widget in (self, inner, lbl, sub):
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
            widget.bind("<Button-1>", self._on_click)

    def _on_enter(self, _event=None):
        self.config(highlightbackground=self._accent)

    def _on_leave(self, _event=None):
        self.config(highlightbackground=BORDER)

    def _on_click(self, _event=None):
        threading.Thread(target=self._command, daemon=True).start()


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
class HeaderBar(tk.Frame):
    def __init__(self, parent, on_gear):
        super().__init__(parent, bg=BG, pady=12)
        self.pack(fill="x", padx=16)

        title = tk.Label(
            self,
            text="HONGI LAUNCHER",
            bg=BG,
            fg=ACCENT,
            font=_make_font(14, "bold"),
        )
        title.pack(side="left")

        gear = tk.Label(
            self,
            text="⚙",
            bg=BG,
            fg=MUTED,
            font=_make_font(16),
            cursor="hand2",
        )
        gear.pack(side="right")
        gear.bind("<Button-1>", lambda _: on_gear())
        gear.bind("<Enter>", lambda _: gear.config(fg=FG))
        gear.bind("<Leave>", lambda _: gear.config(fg=MUTED))

        sep = tk.Frame(parent, bg=BORDER, height=1)
        sep.pack(fill="x")


class MainView(tk.Frame):
    BUTTONS = [
        ("VS Code", CLR_VSCODE, "Remote-SSH  /Users/jeongjintaek/hongi", open_vscode),
        ("Shell", CLR_SHELL, "Windows Terminal — SSH session", open_shell),
        ("Llama", CLR_LLAMA, "SSH  ollama run qwen3:8b", open_llama),
        ("Danger", CLR_DANGER, "SSH  claude --dangerously-skip-permissions", open_danger),
        ("Gemini", CLR_GEMINI, "SSH  GEMINI_CLI_TRUST_WORKSPACE=true gemini", open_gemini),
    ]

    def __init__(self, parent):
        super().__init__(parent, bg=BG)
        self.pack(fill="both", expand=True, padx=16, pady=(10, 16))

        for label, accent, sublabel, cmd in self.BUTTONS:
            btn = HoverButton(self, label, sublabel, accent, cmd)
            btn.pack(fill="x", pady=4)


class SetupItemCard(tk.Frame):
    """Single prereq row: icon + title + status + guide + action button."""

    def __init__(
        self,
        parent,
        title: str,
        guide_text: str,
        install_fn=None,
        install_label: str = "Install",
    ):
        super().__init__(parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        self.pack(fill="x", padx=16, pady=5)

        self._install_fn = install_fn
        self._log_lines: list[str] = []

        top = tk.Frame(self, bg=PANEL, padx=14, pady=10)
        top.pack(fill="x")

        self._status_dot = tk.Label(top, text="●", bg=PANEL, fg=MUTED, font=_make_font(12))
        self._status_dot.pack(side="left", padx=(0, 8))

        right = tk.Frame(top, bg=PANEL)
        right.pack(side="left", fill="x", expand=True)

        self._title_lbl = tk.Label(right, text=title, bg=PANEL, fg=FG, font=_make_font(10, "bold"), anchor="w")
        self._title_lbl.pack(fill="x")

        self._status_lbl = tk.Label(right, text="Checking...", bg=PANEL, fg=MUTED, font=_make_font(9), anchor="w")
        self._status_lbl.pack(fill="x")

        # Guide area (shown on FAIL)
        self._guide_frame = tk.Frame(self, bg=PANEL, padx=14, pady=0)

        self._guide_lbl = tk.Label(
            self._guide_frame,
            text=guide_text,
            bg=PANEL,
            fg=MUTED,
            font=_make_font(8),
            anchor="w",
            justify="left",
            wraplength=400,
        )
        self._guide_lbl.pack(fill="x", pady=(0, 6))

        if install_fn:
            self._install_btn = tk.Button(
                self._guide_frame,
                text=install_label,
                bg=ACCENT,
                fg=BG,
                font=_make_font(9, "bold"),
                relief="flat",
                padx=10,
                pady=4,
                cursor="hand2",
                command=self._do_install,
            )
            self._install_btn.pack(anchor="w")

            self._log_text = tk.Label(
                self._guide_frame,
                text="",
                bg=PANEL,
                fg=MUTED,
                font=_make_font(8),
                anchor="w",
                justify="left",
                wraplength=400,
            )
            self._log_text.pack(fill="x", pady=(4, 0))

    def set_status(self, ok: bool | None, msg: str = "") -> None:
        if ok is None:
            self._status_dot.config(fg=MUTED)
            self._status_lbl.config(text="Checking...", fg=MUTED)
            self._guide_frame.pack_forget()
        elif ok:
            self._status_dot.config(fg=OK)
            self._status_lbl.config(text=msg or "OK", fg=OK)
            self._guide_frame.pack_forget()
        else:
            self._status_dot.config(fg=DANGER)
            self._status_lbl.config(text=msg or "FAIL", fg=DANGER)
            self._guide_frame.pack(fill="x", pady=(0, 10))

    def _do_install(self) -> None:
        self._install_btn.config(state="disabled", text="Installing...")
        self._log_lines.clear()
        if self._install_fn:
            self._install_fn(self._append_log)

    def _append_log(self, line: str) -> None:
        self._log_lines.append(line)
        combined = "\n".join(self._log_lines[-6:])
        self._log_text.after(0, lambda: self._log_text.config(text=combined))


SSH_GUIDE = (
    "No password login required.\n"
    "PowerShell steps:\n"
    "  ssh-keygen   (skip if key exists)\n"
    f"  ssh-copy-id {SSH_TARGET}"
)

TAILSCALE_GUIDE = (
    "Tailscale is a VPN that routes traffic to your Mac.\n"
    "Click 'Install' to auto-install, then sign in at tailscale.com."
)


class SetupView(tk.Frame):
    def __init__(self, parent, on_recheck):
        super().__init__(parent, bg=BG)
        self._on_recheck = on_recheck
        self.pack(fill="both", expand=True)

        info = tk.Label(
            self,
            text="Prerequisites",
            bg=BG,
            fg=MUTED,
            font=_make_font(9),
        )
        info.pack(anchor="w", padx=16, pady=(10, 4))

        self._tailscale_card = SetupItemCard(
            self,
            title="Tailscale",
            guide_text=TAILSCALE_GUIDE,
            install_fn=install_tailscale,
            install_label="Install Tailscale",
        )

        self._ssh_card = SetupItemCard(
            self,
            title="SSH (no password)",
            guide_text=SSH_GUIDE,
            install_fn=None,
        )

        recheck_btn = tk.Button(
            self,
            text="Check again",
            bg=PANEL,
            fg=ACCENT,
            font=_make_font(9),
            relief="flat",
            padx=12,
            pady=6,
            cursor="hand2",
            highlightbackground=BORDER,
            highlightthickness=1,
            command=on_recheck,
        )
        recheck_btn.pack(pady=(12, 16))

    def set_tailscale(self, ok: bool | None, msg: str = "") -> None:
        self._tailscale_card.set_status(ok, msg)

    def set_ssh(self, ok: bool | None, msg: str = "") -> None:
        self._ssh_card.set_status(ok, msg)


# ---------------------------------------------------------------------------
# Main application window
# ---------------------------------------------------------------------------
class HongiLauncher(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("HONGI")
        self.configure(bg=BG)
        self.resizable(False, False)
        self._set_geometry(480, 420)

        # State
        self._current_view: tk.Frame | None = None
        self._setup_view: SetupView | None = None
        self._main_view: MainView | None = None
        self._in_setup = False

        # Header
        self._header = HeaderBar(self, on_gear=self._show_setup)

        # Start prereq check after window is drawn
        self.after(100, self._start_prereq_check)

    def _set_geometry(self, w: int, h: int) -> None:
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        x = (sw - w) // 2
        y = (sh - h) // 2
        self.geometry(f"{w}x{h}+{x}+{y}")

    # ------------------------------------------------------------------
    # View management
    # ------------------------------------------------------------------
    def _clear_view(self) -> None:
        if self._current_view:
            self._current_view.destroy()
            self._current_view = None
        self._setup_view = None
        self._main_view = None

    def _show_setup(self) -> None:
        self._in_setup = True
        self._clear_view()
        self._set_geometry(480, 580)
        sv = SetupView(self, on_recheck=self._start_prereq_check)
        self._current_view = sv
        self._setup_view = sv
        self._start_prereq_check()

    def _show_main(self) -> None:
        self._in_setup = False
        self._clear_view()
        self._set_geometry(480, 420)
        mv = MainView(self)
        self._current_view = mv
        self._main_view = mv

    # ------------------------------------------------------------------
    # Prereq check
    # ------------------------------------------------------------------
    def _start_prereq_check(self) -> None:
        # Ensure setup view is visible while checking
        if self._setup_view is None and self._main_view is None:
            self._show_setup()
            return  # show_setup calls _start_prereq_check again

        if self._setup_view:
            self._setup_view.set_tailscale(None)
            self._setup_view.set_ssh(None)

        threading.Thread(target=self._run_prereq_checks, daemon=True).start()

    def _run_prereq_checks(self) -> None:
        ts_ok, ts_msg = check_tailscale()
        self.after(0, self._on_tailscale_result, ts_ok, ts_msg)

        ssh_ok, ssh_msg = check_ssh_nopass()
        self.after(0, self._on_ssh_result, ssh_ok, ssh_msg, ts_ok)

    def _on_tailscale_result(self, ok: bool, msg: str) -> None:
        if self._setup_view:
            self._setup_view.set_tailscale(ok, msg)

    def _on_ssh_result(self, ssh_ok: bool, ssh_msg: str, ts_ok: bool) -> None:
        if self._setup_view:
            self._setup_view.set_ssh(ssh_ok, ssh_msg)

        if ts_ok and ssh_ok and not self._in_setup:
            self._show_main()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    if not acquire_lock():
        signal_first_instance()
        sys.exit(0)

    app = HongiLauncher()

    # Start socket listener thread
    listener = threading.Thread(target=listen_for_signals, args=(app,), daemon=True)
    listener.start()

    app.mainloop()


if __name__ == "__main__":
    main()
