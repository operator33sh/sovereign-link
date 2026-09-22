"""Music Control Bridge — playerctl interface for Luna.

Supported actions: play_pause, next, previous, status, play, pause, stop.
Communicates only with the local playerctl daemon (no network).
"""
from __future__ import annotations

import os
import shutil
import subprocess


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _dbus_env() -> dict[str, str]:
    """Return an environment dict with DBUS_SESSION_BUS_ADDRESS resolved.

    When the bot runs as a systemd service it has no D-Bus session address.
    We probe the most common locations so playerctl can reach the user's
    running media player without needing a display.
    """
    env = os.environ.copy()

    # Already set — nothing to do
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return env

    # systemd user bus socket (most modern distros)
    uid = os.getuid()
    socket_path = f"/run/user/{uid}/bus"
    if os.path.exists(socket_path):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={socket_path}"
        return env

    # Fallback: scan /proc for a running process that has the variable set
    try:
        for pid_entry in os.listdir("/proc"):
            if not pid_entry.isdigit():
                continue
            environ_file = f"/proc/{pid_entry}/environ"
            try:
                with open(environ_file, "rb") as f:
                    for item in f.read().split(b"\x00"):
                        if item.startswith(b"DBUS_SESSION_BUS_ADDRESS="):
                            env["DBUS_SESSION_BUS_ADDRESS"] = item[len(b"DBUS_SESSION_BUS_ADDRESS="):].decode()
                            return env
            except (PermissionError, FileNotFoundError):
                continue
    except Exception:
        pass

    return env


def _playerctl(*args: str, capture: bool = False) -> tuple[int, str]:
    """Run a playerctl command. Returns (returncode, stdout/stderr)."""
    if not shutil.which("playerctl"):
        return -1, (
            "playerctl is niet geïnstalleerd. "
            "Installeer het via: sudo apt install playerctl"
        )
    try:
        result = subprocess.run(
            ["playerctl", *args],
            capture_output=True,
            text=True,
            timeout=5,
            env=_dbus_env(),
        )
        output = (result.stdout or result.stderr or "").strip()
        return result.returncode, output
    except subprocess.TimeoutExpired:
        return -1, "playerctl reageerde niet binnen 5 seconden."
    except Exception as exc:
        return -1, f"Fout bij uitvoeren van playerctl: {exc}"


def _no_player_msg(output: str) -> bool:
    return "No players found" in output or "No player could handle" in output


# ---------------------------------------------------------------------------
# Public tool function
# ---------------------------------------------------------------------------

def control_music(args: dict) -> str:
    action = args.get("action", "status").lower().strip()

    # ── play_pause ──────────────────────────────────────────────────────────
    if action == "play_pause":
        code, out = _playerctl("play-pause")
        if code != 0:
            if _no_player_msg(out):
                return "Geen actieve muziekspeler gevonden. Start RhythmBox eerst."
            return f"Kon niet schakelen: {out}"
        return "▶/⏸ Afspelen/pauzeren gewisseld."

    # ── play ─────────────────────────────────────────────────────────────────
    if action == "play":
        code, out = _playerctl("play")
        if code != 0:
            return f"Kan niet afspelen: {out}"
        return "▶ Muziek gestart."

    # ── pause ────────────────────────────────────────────────────────────────
    if action == "pause":
        code, out = _playerctl("pause")
        if code != 0:
            return f"Kan niet pauzeren: {out}"
        return "⏸ Muziek gepauzeerd."

    # ── stop ─────────────────────────────────────────────────────────────────
    if action == "stop":
        code, out = _playerctl("stop")
        if code != 0:
            return f"Kan niet stoppen: {out}"
        return "⏹ Muziek gestopt."

    # ── next ─────────────────────────────────────────────────────────────────
    if action == "next":
        code, out = _playerctl("next")
        if code != 0:
            if _no_player_msg(out):
                return "Geen actieve muziekspeler gevonden."
            return f"Volgende nummer mislukt: {out}"
        # Show what's playing now
        _, status = _playerctl("metadata", "--format", "{{ artist }} - {{ title }}")
        if status:
            return f"⏭ Volgend nummer: {status}"
        return "⏭ Volgende nummer."

    # ── previous ─────────────────────────────────────────────────────────────
    if action == "previous":
        code, out = _playerctl("previous")
        if code != 0:
            if _no_player_msg(out):
                return "Geen actieve muziekspeler gevonden."
            return f"Vorig nummer mislukt: {out}"
        _, status = _playerctl("metadata", "--format", "{{ artist }} - {{ title }}")
        if status:
            return f"⏮ Vorig nummer: {status}"
        return "⏮ Vorig nummer."

    # ── status ────────────────────────────────────────────────────────────────
    if action == "status":
        code, play_state = _playerctl("status")
        if code != 0:
            if _no_player_msg(play_state):
                return "Geen actieve muziekspeler gevonden. Start RhythmBox eerst."
            return f"Kan status niet ophalen: {play_state}"

        _, artist = _playerctl("metadata", "xesam:artist")
        _, title  = _playerctl("metadata", "xesam:title")
        _, album  = _playerctl("metadata", "xesam:album")

        state_icon = {"Playing": "▶", "Paused": "⏸", "Stopped": "⏹"}.get(play_state, "?")
        lines = [f"{state_icon} **Status:** {play_state}"]
        if title:
            lines.append(f"🎵 **Nummer:** {title}")
        if artist:
            lines.append(f"🎤 **Artiest:** {artist}")
        if album:
            lines.append(f"💿 **Album:** {album}")
        return "\n".join(lines)

    return f"Onbekende actie '{action}'. Kies uit: play, pause, play_pause, stop, next, previous, status."


# ---------------------------------------------------------------------------
# Public helper — used by euphoria_engine for somatic activation
# ---------------------------------------------------------------------------

def start_music() -> str:
    """Start playback. Returns a short status string."""
    return control_music({"action": "play"})


# ---------------------------------------------------------------------------
# Quick validation
# ---------------------------------------------------------------------------

def _test() -> None:
    print("=== Music Control Bridge — test ===")
    for action in ("status", "play_pause", "next", "previous", "play_pause"):
        print(f"\n[{action}]")
        print(control_music({"action": action}))


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "control_music",
            "description": (
                "Bestuur de lokale muziekspeler (RhythmBox) via playerctl. "
                "Acties: play, pause, play_pause, stop, next, previous, status. "
                "Gebruik 'status' om te zien wat er speelt. "
                "Vereist dat playerctl is geïnstalleerd en RhythmBox actief is."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play", "pause", "play_pause", "stop", "next", "previous", "status"],
                        "description": "De uit te voeren actie.",
                    },
                },
                "required": ["action"],
            },
        },
    },
]

HANDLERS = {
    "control_music": control_music,
}


if __name__ == "__main__":
    _test()
