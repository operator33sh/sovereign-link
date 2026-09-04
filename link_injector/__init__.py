#!/usr/bin/env python3
"""
Mechanical Link Injector — standalone CLI
=========================================
Pipeline:
  1. Scan vault → vault_map.json  (razendsnel, pure Python)
  2. LLM-analyse in chunks        (één LLM-call per chunk, retry-logica ingebouwd)
  3. Mechanische injectie         (directe filesystem-writes, geen LLM per bestand)

Gebruik:
  python -m link_injector [opties]
  python -m link_injector --dry-run
  python -m link_injector --directory 10_Kern --chunk-size 30
  python -m link_injector --from-matrix link_matrix.json  # sla stap 1+2 over

Omgevingsvariabelen (zelfde als agent):
  VAULT_PATH, OLLAMA_BASE_URL, OLLAMA_API_KEY, OLLAMA_MODEL
"""

import argparse
import json
import os
import sys

from link_injector.scanner import (
    AGENT_TEMP_PATH,
    VAULT_PATH,
    _load_dotenv,
    build_vault_map,
    section,
    info,
    warn,
    error,
    strip_all_links,
    TERM_WIDTH,
)
from link_injector.analyzer import analyze_vault_map, analyze_for_mocs
from link_injector.injector import inject_links
from link_injector.moc_builder import inject_moc_links, _assign_uncovered_files, _create_home_moc

_load_dotenv()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mechanical Link Injector — scan vault, analyseer met LLM, injecteer [[wikilinks]]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Voorbeelden:
  python -m link_injector                          # volledige vault
  python -m link_injector --dry-run                # preview, geen schrijven
  python -m link_injector --directory 10_Kern      # alleen submap
  python -m link_injector --chunk-size 30          # kleinere LLM-chunks
  python -m link_injector --from-matrix matrix.json  # hergebruik bestaande matrix
""",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview wat er geïnjecteerd zou worden — schrijft niets naar de vault",
    )
    parser.add_argument(
        "--directory", default="",
        help="Scan alleen deze submap (relatief pad in vault, bijv. '10_Kern')",
    )
    parser.add_argument(
        "--chunk-size", type=int, default=50, metavar="N",
        help="Bestanden per LLM-chunk (standaard: 50)",
    )
    parser.add_argument(
        "--preview-words", type=int, default=0, metavar="N",
        help="Max woorden per bestand in de vault map (0 = volledig, standaard: 0)",
    )
    parser.add_argument(
        "--max-retries", type=int, default=3, metavar="N",
        help="Max LLM-retries per chunk bij 429/503/timeout (standaard: 3)",
    )
    parser.add_argument(
        "--strip-links", action="store_true",
        help="Strip eerst alle bestaande [[wikilinks]] uit de vault vóór nieuwe injectie",
    )
    parser.add_argument(
        "--uncovered-only", action="store_true",
        help="Analyseer alleen bestanden die nog geen [[wikilinks]] bevatten",
    )
    parser.add_argument(
        "--create-mocs", action="store_true",
        help="MOC-modus: groepeer vault in clusters, maak MOC-bestanden aan, injecteer bidirectionele links",
    )
    parser.add_argument(
        "--moc-count", type=int, default=5, metavar="N",
        help="Aantal thematische MOCs (klaverblad-bladen, standaard: 5)",
    )
    parser.add_argument(
        "--moc-depth", type=int, default=1, metavar="N",
        help="MOC-hiërarchiediepte: 1=flat (standaard), 2=top-MOC → sub-MOCs → notes",
    )
    parser.add_argument(
        "--sub-moc-count", type=int, default=3, metavar="N",
        help="Aantal sub-MOCs per top-MOC bij --moc-depth 2 (standaard: 3)",
    )
    parser.add_argument(
        "--from-matrix", metavar="FILE",
        help="Sla scan+LLM over en gebruik een bestaand link_matrix.json bestand",
    )
    parser.add_argument(
        "--save-matrix", metavar="FILE",
        default=os.path.join(AGENT_TEMP_PATH, "link_matrix.json"),
        help="Pad om link matrix op te slaan (standaard: .agent_temp/link_matrix.json)",
    )
    return parser.parse_args()


def print_summary(
    n_files: int,
    n_links: int,
    injected: int,
    skipped: int,
    errors: int,
    matrix_path: str,
    dry_run: bool,
    dry_log: list[dict],
):
    section("SAMENVATTING")
    info(f"Vault-bestanden gescand : {n_files}")
    info(f"Links in matrix         : {n_links}")
    info(f"Links geïnjecteerd      : {injected}" + (" (DRY-RUN, geen wijzigingen)" if dry_run else ""))
    info(f"Duplicaten geskipt      : {skipped}")
    info(f"Fouten                  : {errors}")
    info(f"Link matrix opgeslagen  : {matrix_path}")

    if dry_run and dry_log:
        print()
        info("DRY-RUN preview (eerste 20):")
        for entry in dry_log[:20]:
            info(f"  {entry['source']}  ←  {entry['wikilink']}")
        if len(dry_log) > 20:
            info(f"  ... en {len(dry_log)-20} meer")

    if not dry_run and errors == 0:
        info("\n  ✓ Klaar — vault bijgewerkt.")
    elif not dry_run and errors > 0:
        warn(f"{errors} injecties mislukt. Controleer bovenstaande output.")


def main():
    args = parse_args()

    print(f"\n{'═'*TERM_WIDTH}")
    print("  MECHANICAL LINK INJECTOR")
    print(f"  Vault : {VAULT_PATH}")
    from link_injector.scanner import BASE_URL, MODEL
    print(f"  Model : {MODEL} @ {BASE_URL}")
    if args.dry_run:
        print("  Modus : DRY-RUN (geen wijzigingen in vault)")
    print(f"{'═'*TERM_WIDTH}")

    # ── Stap 0: Strip bestaande links (optioneel) ─────────────────────────────
    if args.strip_links:
        section("STAP 0: Bestaande [[wikilinks]] strippen" + (" [DRY-RUN]" if args.dry_run else ""))
        n_stripped = strip_all_links(dry_run=args.dry_run, directory=args.directory)
        info(f"{n_stripped} bestanden ontdaan van wikilinks")

    # ── Stap 1: Scan of laad bestaande matrix ─────────────────────────────────
    entries: list[dict] = []
    if args.from_matrix:
        section("STAP 1+2: Bestaande link matrix laden")
        try:
            with open(args.from_matrix, 'r', encoding='utf-8') as f:
                matrix = json.load(f)
            info(f"Matrix geladen: {len(matrix)} links uit {args.from_matrix}")
            n_files: int | str = "?"
        except Exception as e:
            error(f"Kan matrix niet laden: {e}")
            sys.exit(1)
    else:
        section(f"STAP 1: Vault scannen{'  [' + args.directory + ']' if args.directory else ''}")
        try:
            entries = build_vault_map(
                directory=args.directory,
                max_preview_words=args.preview_words,
                uncovered_only=args.uncovered_only,
            )
        except Exception as e:
            error(f"Scan mislukt: {e}")
            sys.exit(1)

        n_files = len(entries)
        n_chunks = (n_files + args.chunk_size - 1) // args.chunk_size
        est_tokens = n_files * args.preview_words * 1.3
        info(f"{n_files} bestanden → {n_chunks} chunks × {args.chunk_size} bestanden")
        info(f"Geschat ~{est_tokens/1000:.0f}k tokens totaal voor LLM-analyse")

        # ── Stap 2: LLM-analyse ───────────────────────────────────────────────
        section("STAP 2: LLM-analyse (chunks)")
        try:
            matrix = analyze_vault_map(
                entries,
                chunk_size=args.chunk_size,
                max_retries=args.max_retries,
            )
        except Exception as e:
            error(f"LLM-analyse afgebroken: {e}")
            sys.exit(1)

        os.makedirs(os.path.dirname(args.save_matrix), exist_ok=True)
        try:
            with open(args.save_matrix, 'w', encoding='utf-8') as f:
                json.dump(matrix, f, ensure_ascii=False, indent=2)
            info(f"Link matrix opgeslagen: {args.save_matrix}")
        except Exception as e:
            warn(f"Opslaan matrix mislukt: {e}")

    # ── Stap 3: Injectie ──────────────────────────────────────────────────────
    section("STAP 3: Mechanische injectie" + (" [DRY-RUN]" if args.dry_run else ""))
    injected, skipped, errors_count, dry_log = inject_links(matrix, dry_run=args.dry_run)

    print_summary(
        n_files=n_files,
        n_links=len(matrix),
        injected=injected,
        skipped=skipped,
        errors=errors_count,
        matrix_path=args.save_matrix if not args.from_matrix else args.from_matrix,
        dry_run=args.dry_run,
        dry_log=dry_log,
    )
    print(f"{'═'*TERM_WIDTH}\n")

    # ── MOC-pipeline (optioneel) ───────────────────────────────────────────────
    if args.create_mocs:
        section("MOC-PIPELINE: Clusters analyseren")

        if args.from_matrix:
            info("Vault opnieuw scannen voor MOC-analyse (geen entries van --from-matrix)...")
            try:
                entries = build_vault_map(max_preview_words=args.preview_words)
            except Exception as e:
                error(f"Scan mislukt: {e}")
                sys.exit(1)

        clusters = analyze_for_mocs(entries, max_retries=args.max_retries, moc_count=args.moc_count)
        clusters = _assign_uncovered_files(clusters, entries)

        if not clusters:
            warn("Geen MOC-clusters gevonden — MOC-pipeline overgeslagen")
        else:
            moc_matrix_path = os.path.join(AGENT_TEMP_PATH, "moc_clusters.json")
            os.makedirs(AGENT_TEMP_PATH, exist_ok=True)
            try:
                with open(moc_matrix_path, 'w', encoding='utf-8') as f:
                    json.dump(clusters, f, ensure_ascii=False, indent=2)
                info(f"{len(clusters)} clusters opgeslagen: {moc_matrix_path}")
            except Exception as e:
                warn(f"Opslaan clusters mislukt: {e}")

            section(f"MOC-PIPELINE: Aanmaken + linken{'  [DRY-RUN]' if args.dry_run else ''}")
            mocs_created, mocs_updated, spoke_injected, moc_errors, _ = inject_moc_links(
                clusters,
                dry_run=args.dry_run,
                moc_depth=args.moc_depth,
                sub_count=args.sub_moc_count,
                max_retries=args.max_retries,
                all_entries=entries,
            )

            _create_home_moc(clusters, dry_run=args.dry_run)

            section("MOC SAMENVATTING")
            info(f"MOC-bestanden aangemaakt : {mocs_created}")
            info(f"MOC-bestanden bijgewerkt : {mocs_updated}")
            info(f"Spoke-links geïnjecteerd : {spoke_injected}")
            info(f"Fouten                   : {moc_errors}")
            from link_injector.scanner import MOC_DIR
            info(f"MOC-map                  : {os.path.join(VAULT_PATH, MOC_DIR)}/")
            info(f"Home MOC                 : {os.path.join(VAULT_PATH, MOC_DIR, 'MOC_Home.md')}")
            if moc_errors > 0:
                warn(f"{moc_errors} fouten in MOC-pipeline.")
            errors_count += moc_errors

        print(f"{'═'*TERM_WIDTH}\n")

    sys.exit(1 if errors_count > 0 else 0)


if __name__ == "__main__":
    main()
