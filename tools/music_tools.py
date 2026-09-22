"""Music Control Bridge — playerctl interface for Luna.

Supported actions: play_pause, next, previous, status, play, pause, stop.
Communicates only with the local playerctl daemon (no network).
"""
from __future__ import annotations

import shutil
import subprocess


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

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
