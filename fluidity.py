"""
fluidity.py — Fluidity Layer for Sovereign-Link

Eliminates AI staccato by combining four mechanisms:
  1. Narrative formatting directive  — injected into the system prompt
  2. Contextual Rhythm / Mirroring   — mirrors user sentence length/energy
  3. Anti-Cliché Filter              — post-processes common AI phrases out
  4. Token Stream Pacing             — SSE chunks with variable delays

All behaviour scales with `strength` (0.0 – 1.0).
Adjust per session via `set_strength()` or the `set_fluidity` tool.
"""

import os
import random
import re
from dataclasses import dataclass
from typing import Iterator

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class FluidityConfig:
    """Runtime configuration for the Fluidity Layer."""

    enabled: bool = True
    strength: float = 0.7          # 0.0 = off, 1.0 = maximum

    # Feature toggles (each can be disabled independently)
    narrative_formatting: bool = True
    mirroring: bool = True
    anti_cliche: bool = True
    stream_pacing: bool = True

    # Stream pacing — base delay + random jitter, in milliseconds, scaled by strength
    base_delay_ms: float = 6.0
    jitter_ms: float = 10.0
    chunk_words: int = 2           # target words per SSE chunk


def _default_strength() -> float:
    try:
        return float(os.environ.get("FLUIDITY_STRENGTH", "0.7"))
    except ValueError:
        return 0.7


_config = FluidityConfig(strength=_default_strength())


def get_config() -> FluidityConfig:
    return _config


def set_strength(strength: float) -> str:
    """Set fluidity strength (0.0–1.0). Effective immediately for this session."""
    _config.strength = max(0.0, min(1.0, float(strength)))
    _config.enabled = _config.strength > 0.0
    return f"Fluidity strength ingesteld op {_config.strength:.2f}."


def set_feature(
    *,
    narrative: bool | None = None,
    mirroring: bool | None = None,
    anti_cliche: bool | None = None,
    stream_pacing: bool | None = None,
) -> str:
    """Toggle individual Fluidity features on/off."""
    if narrative is not None:
        _config.narrative_formatting = narrative
    if mirroring is not None:
        _config.mirroring = mirroring
    if anti_cliche is not None:
        _config.anti_cliche = anti_cliche
    if stream_pacing is not None:
        _config.stream_pacing = stream_pacing
    return (
        f"Fluidity features: narrative={_config.narrative_formatting}, "
        f"mirroring={_config.mirroring}, anti_cliche={_config.anti_cliche}, "
        f"stream_pacing={_config.stream_pacing}."
    )


# ---------------------------------------------------------------------------
# 1. Narrative Formatting Directive — injected into system prompt
# ---------------------------------------------------------------------------

def build_system_directive() -> str:
    """Return a system-prompt block that steers narrative, fluid formatting."""
    if not _config.enabled or not _config.narrative_formatting:
        return ""

    s = _config.strength

    if s < 0.35:
        label = "licht"
        extras = ""
    elif s < 0.7:
        label = "matig"
        extras = (
            "\n- Verbind ideeën met overgangszinnen: "
            "'Dit betekent dat…', 'Vandaar…', 'In dat licht…'."
        )
    else:
        label = "volledig"
        extras = (
            "\n- Verbind ideeën met vloeiende overgangszinnen."
            "\n- Wissel korte en langere zinnen bewust af voor een natuurlijk ritme."
            "\n- Splits geen enkelvoudige gedachte op in bullet-punten."
        )

    return (
        "\n\n---\n\n"
        f"## Vloeiende Communicatiestijl (Fluidity Layer — {label})\n\n"
        "Schrijf in narratieve, vloeiende zinnen. "
        "Gebruik bullet-points of genummerde lijsten ALLEEN bij echte opsommingen "
        "(drie of meer parallelle items zonder causale samenhang). "
        "Vermijd onnodige kopjes bij korte antwoorden.\n"
        "Vermijd standaard-AI-zinnen zoals: "
        "'Het is belangrijk om te onthouden dat', 'Uiteraard', 'Zeker!', "
        "'Absoluut', 'Natuurlijk', 'Als AI-assistent', 'Ik ben hier om je te helpen', "
        "'Laat me weten als je nog vragen hebt'."
        + extras
    )


# ---------------------------------------------------------------------------
# 2. Contextual Rhythm / Mirroring Protocol
# ---------------------------------------------------------------------------

def build_mirror_hint(user_text: str) -> str:
    """Analyse user message rhythm; return a system-prompt block to mirror it."""
    if not _config.enabled or not _config.mirroring or _config.strength < 0.3:
        return ""
    if not user_text or not user_text.strip():
        return ""

    words = user_text.split()
    word_count = len(words)
    sentences = [s.strip() for s in re.split(r'[.!?]+', user_text) if s.strip()]
    avg_sentence_len = (
        sum(len(s.split()) for s in sentences) / len(sentences)
        if sentences else word_count
    )

    # Length guidance
    if word_count < 12:
        length_hint = (
            "De gebruiker schrijft kort en to-the-point — "
            "houd je antwoord beknopt (2–4 zinnen tenzij meer echt nodig is)."
        )
    elif word_count < 50:
        length_hint = "Stem de antwoordlengte af op de vraag — niet te uitgebreid."
    else:
        length_hint = (
            "De gebruiker schrijft uitgebreid — "
            "een meer uitgewerkt antwoord past hier."
        )

    # Rhythm guidance
    if avg_sentence_len < 7:
        rhythm_hint = (
            "De gebruiker schrijft in korte, puntige zinnen — "
            "gebruik ook relatief directe zinnen."
        )
    elif avg_sentence_len > 22:
        rhythm_hint = (
            "De gebruiker schrijft in langere zinnen — "
            "varieer gerust meer in zinsbouw."
        )
    else:
        rhythm_hint = ""

    parts = [h for h in [length_hint, rhythm_hint] if h]
    if not parts:
        return ""

    return (
        "\n\n---\n\n"
        "## Contextual Rhythm Hint (Mirroring Protocol)\n\n"
        + " ".join(parts)
    )


# ---------------------------------------------------------------------------
# 3. Anti-Cliché Filter — post-processing pass on LLM output
# ---------------------------------------------------------------------------

# Each entry: (compiled pattern, replacement string)
# Ordered: most specific first to avoid partial matches.
_CLICHE_RULES: list[tuple[re.Pattern, str]] = [
    # Opening filler phrases
    (re.compile(r'\bHet is belangrijk om te (onthouden|weten|begrijpen|vermelden) dat\b[,]?\s*', re.I), ''),
    (re.compile(r'\bHet is de moeite waard om (te )?vermelden dat\b[,]?\s*', re.I), ''),
    (re.compile(r'\bHet is goed om te weten dat\b[,]?\s*', re.I), ''),
    # AI self-reference
    (re.compile(r'\bAls (grote |krachtige |geavanceerde )?AI[- ]?(taalmodel|assistent|systeem|model)?\b', re.I), 'Als assistent'),
    # Hollow affirmations at sentence start
    (re.compile(r'(?:^|\.\s+)(Natuurlijk|Uiteraard|Absoluut|Zeker|Jazeker)[!,]?\s+', re.I | re.M), lambda m: m.group(0).split(m.group(1))[0]),
    # Closing filler (end of string)
    (re.compile(r'\bGraag gedaan[.!]?\s*$', re.I | re.M), ''),
    (re.compile(r'\bIk ben hier om (je )?te helpen[.!]?\s*$', re.I | re.M), ''),
    (re.compile(r'\bIk sta (altijd )?voor je klaar[.!]?\s*$', re.I | re.M), ''),
    (re.compile(r'\bHoop dat dit helpt[.!]?\s*$', re.I | re.M), ''),
    (re.compile(r'\bLaat (me|het me) weten als je (nog )?(meer )?vragen hebt[.!]?\s*$', re.I | re.M), ''),
    (re.compile(r'\bMocht je nog vragen hebben[,.]?\s*(dan\s+)?hoor ik het graag[.!]?\s*$', re.I | re.M), ''),
]

# Regex to collapse blank lines left by removals
_MULTI_BLANK = re.compile(r'\n{3,}')
_LEADING_SPACE = re.compile(r'^ +', re.M)


def filter_output(text: str) -> str:
    """Remove/replace common AI clichés from the LLM's output text."""
    if not _config.enabled or not _config.anti_cliche or not text:
        return text

    for pattern, replacement in _CLICHE_RULES:
        if callable(replacement):
            text = pattern.sub(replacement, text)
        else:
            text = pattern.sub(replacement, text)

    text = _MULTI_BLANK.sub('\n\n', text)
    text = _LEADING_SPACE.sub('', text)
    return text.strip()


# ---------------------------------------------------------------------------
# 4. Token Stream Pacing — yields (chunk, delay_seconds) for SSE
# ---------------------------------------------------------------------------

def _split_into_chunks(text: str, target_words: int) -> list[str]:
    """Split text into chunks of ~target_words words, keeping whitespace attached."""
    # Tokenise on whitespace boundaries, preserving the separators
    tokens = re.split(r'(\s+)', text)
    chunks: list[str] = []
    current: list[str] = []
    wcount = 0

    for token in tokens:
        current.append(token)
        if token.strip():           # it's a word, not whitespace
            wcount += 1
        if wcount >= target_words and token.strip():
            chunks.append(''.join(current))
            current = []
            wcount = 0

    if current:
        chunks.append(''.join(current))

    return [c for c in chunks if c]


def iter_stream_chunks(text: str) -> Iterator[tuple[str, float]]:
    """Yield (chunk, delay_seconds) for perceptual-pacing SSE output.

    Delays are variable; slightly longer after sentence-ending punctuation
    to mimic the natural cadence of composed human text.
    Delay of 0.0 means yield immediately (when pacing is disabled).
    """
    if not _config.enabled or not _config.stream_pacing or _config.strength <= 0.0:
        yield text, 0.0
        return

    s = _config.strength
    base = (_config.base_delay_ms * s) / 1000.0
    jitter = (_config.jitter_ms * s) / 1000.0

    for chunk in _split_into_chunks(text, _config.chunk_words):
        delay = base + random.uniform(0.0, jitter)
        # Pause longer after sentence endings — mirrors natural typing rhythm
        stripped = chunk.rstrip()
        if stripped and stripped[-1] in '.!?…':
            delay += base * 1.8
        yield chunk, delay
