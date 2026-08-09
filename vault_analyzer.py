"""vault_analyzer.py — Fractalisme Vault usage & growth analyzer."""

import os
import re
import sys
from collections import Counter
from pathlib import Path


def load_vault_path() -> Path:
    path = os.environ.get("VAULT_PATH")
    if not path:
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("VAULT_PATH="):
                    path = line.split("=", 1)[1].strip()
                    break
    if not path:
        print("Error: VAULT_PATH not set. Set it as an env var or in .env.")
        sys.exit(1)
    p = Path(path)
    if not p.exists():
        print(f"Error: vault path does not exist: {p}")
        sys.exit(1)
    return p


def collect_files(vault: Path) -> list[Path]:
    return sorted(vault.rglob("*.md"))


def read_safe(path: Path) -> str:
    for enc in ("utf-8", "latin-1"):
        try:
            return path.read_text(encoding=enc)
        except Exception:
            continue
    return ""


TAG_PATTERN = re.compile(r"#[A-Za-z0-9_\-]+")
MONTH_TAG_PATTERN = re.compile(r"#(\d{4}-\d{2})\b")
ISO_DATE_PATTERN = re.compile(r"\b(\d{4}-\d{2})-\d{2}")


def analyze(vault: Path, save_report: bool = True) -> None:
    files = collect_files(vault)
    total_files = len(files)

    sizes = [f.stat().st_size for f in files]
    total_bytes = sum(sizes)
    avg_bytes = total_bytes / total_files if total_files else 0

    small = sum(1 for s in sizes if s < 10_000)
    medium = sum(1 for s in sizes if 10_000 <= s <= 100_000)
    large = sum(1 for s in sizes if s > 100_000)

    tag_counter: Counter = Counter()
    month_counter: Counter = Counter()

    for f in files:
        content = read_safe(f)
        if not content:
            continue
        for tag in TAG_PATTERN.findall(content):
            tag_counter[tag] += 1
        for m in MONTH_TAG_PATTERN.findall(content):
            month_counter[m] += 1
        if not MONTH_TAG_PATTERN.search(content):
            for m in ISO_DATE_PATTERN.findall(content):
                month_counter[m] += 1

    top10_files = sorted(zip(files, sizes), key=lambda x: x[1], reverse=True)[:10]

    month_tag_counts = {
        k: v for k, v in tag_counter.items() if re.match(r"#\d{4}-\d{2}$", k)
    }
    other_tags = {
        k: v for k, v in tag_counter.items() if not re.match(r"#\d{4}-\d{2}$", k)
    }
    top_other_tags = Counter(other_tags).most_common(20)

    lines = []
    lines.append("=" * 60)
    lines.append("  FRACTALISME VAULT — ANALYSE RAPPORT")
    lines.append("=" * 60)

    lines.append("\n── DATA VOLUME ──────────────────────────────────────────")
    lines.append(f"  Totaal aantal bestanden : {total_files}")
    lines.append(f"  Totale grootte          : {total_bytes / 1_048_576:.2f} MB")
    lines.append(f"  Gemiddelde bestandsgrootte: {avg_bytes / 1024:.1f} KB")
    lines.append(f"\n  Verdeling:")
    lines.append(f"    Klein  (<10 KB)  : {small} bestanden")
    lines.append(f"    Medium (10–100 KB): {medium} bestanden")
    lines.append(f"    Groot  (>100 KB)  : {large} bestanden")

    lines.append("\n── GROEI PER MAAND ──────────────────────────────────────")
    if month_counter:
        for month in sorted(month_counter):
            bar = "█" * min(month_counter[month], 50)
            lines.append(f"  {month}  {bar} ({month_counter[month]})")
    else:
        lines.append("  Geen maand-tags of ISO-datums gevonden.")

    lines.append("\n── MEEST GEBRUIKTE MAAND-TAGS ───────────────────────────")
    if month_tag_counts:
        for tag, count in sorted(month_tag_counts.items(), key=lambda x: x[0]):
            lines.append(f"  {tag:<14} {count}x")
    else:
        lines.append("  Geen #YYYY-MM tags gevonden in content.")

    lines.append("\n── TOP 20 OVERIGE TAGS ──────────────────────────────────")
    if top_other_tags:
        for tag, count in top_other_tags:
            lines.append(f"  {tag:<30} {count}x")
    else:
        lines.append("  Geen tags gevonden.")

    lines.append("\n── TOP 10 GROOTSTE BESTANDEN ────────────────────────────")
    for path, size in top10_files:
        rel = path.relative_to(vault)
        lines.append(f"  {size / 1024:>8.1f} KB  {rel}")

    lines.append("\n" + "=" * 60)

    report = "\n".join(lines)
    print(report)

    if save_report:
        out = Path(__file__).parent / "stats" / "vault_stats_report.txt"
        out.parent.mkdir(exist_ok=True)
        out.write_text(report, encoding="utf-8")
        print(f"\nRapport opgeslagen als: {out}")


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
    save = "--no-save" not in sys.argv
    analyze(load_vault_path(), save_report=save)
