#!/usr/bin/env python3
"""Genera il bundle del catalogo carte usato da PokeCatalog.

Legge i dati grezzi del progetto PokemonTCG/pokemon-tcg-data (checkout locale)
e produce due file:

  catalog.json.gz  bundle compresso con solo i campi che servono all'app
  manifest.json    metadati leggeri (hash del contenuto, conteggi, dimensioni)

L'app scarica prima il manifest: se il campo contentHash coincide con quello
gia' importato, il bundle non viene nemmeno richiesto.

Uso:
    python build_catalog.py --source ./source --output ./dist
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Versione dello schema del bundle. Va incrementata solo se cambia la FORMA dei
# dati (campi aggiunti/rinominati): l'app rifiuta gli schemi che non conosce.
SCHEMA_VERSION = 1

# Campi tenuti per ogni set. Tutto il resto (legalities, updatedAt, ...) viene
# scartato: l'app non lo usa e peserebbe solo sul download.
def slim_set(raw: dict) -> dict:
    images = raw.get("images") or {}
    return {
        "id": raw["id"],
        "name": raw.get("name") or raw["id"],
        "series": raw.get("series") or "",
        "printedTotal": int(raw.get("printedTotal") or 0),
        "total": int(raw.get("total") or raw.get("printedTotal") or 0),
        "ptcgoCode": raw.get("ptcgoCode"),
        "releaseDate": raw.get("releaseDate"),
        "symbol": images.get("symbol"),
        "logo": images.get("logo"),
    }


# Campi tenuti per ogni carta. Niente attacchi, testi, abilita': per il
# riconoscimento servono nome, numero e set, il resto e' per mostrarla a video.
def slim_card(raw: dict, set_id: str) -> dict:
    images = raw.get("images") or {}
    return {
        "id": raw["id"],
        "set": set_id,
        "name": raw.get("name") or "",
        "number": str(raw.get("number") or ""),
        "rarity": raw.get("rarity"),
        "supertype": raw.get("supertype"),
        "img": images.get("small"),
        "imgHi": images.get("large"),
    }


_NUMBER_PARTS = re.compile(r"^([A-Za-z]*)(\d*)(.*)$")


def number_sort_key(number: str) -> tuple:
    """Ordina i numeri carta come farebbe un umano: 2 prima di 10, TG1 prima di TG12."""
    letters, digits, rest = _NUMBER_PARTS.match(number or "").groups()
    return (letters.upper(), int(digits) if digits else -1, rest.upper())


def build(source: Path, output: Path) -> dict:
    sets_file = source / "sets" / "en.json"
    cards_dir = source / "cards" / "en"

    if not sets_file.is_file():
        sys.exit(f"File dei set non trovato: {sets_file}")
    if not cards_dir.is_dir():
        sys.exit(f"Cartella delle carte non trovata: {cards_dir}")

    raw_sets = json.loads(sets_file.read_text(encoding="utf-8"))
    sets = sorted(
        (slim_set(s) for s in raw_sets),
        key=lambda s: (s["releaseDate"] or "", s["id"]),
    )
    known_set_ids = {s["id"] for s in sets}

    cards: list[dict] = []
    skipped: list[str] = []
    for card_file in sorted(cards_dir.glob("*.json")):
        set_id = card_file.stem
        if set_id not in known_set_ids:
            # Un file di carte senza il set corrispondente nell'indice: senza
            # printedTotal non e' utilizzabile per il matching, quindi lo saltiamo.
            skipped.append(set_id)
            continue
        raw_cards = json.loads(card_file.read_text(encoding="utf-8"))
        cards.extend(slim_card(c, set_id) for c in raw_cards)

    cards.sort(key=lambda c: (c["set"], number_sort_key(c["number"]), c["id"]))

    if skipped:
        print(f"Set ignorati (assenti da sets/en.json): {', '.join(skipped)}")

    catalog = {
        "schema": SCHEMA_VERSION,
        "sets": sets,
        "cards": cards,
    }

    # separators compatto e nessun sort_keys: le chiavi restano nell'ordine in
    # cui le costruiamo qui ("sets" prima di "cards"), che e' quello che serve
    # all'app per importare in streaming senza tenere tutto in memoria.
    # L'output resta deterministico perche' ogni dict e' costruito sempre uguale
    # e le liste sono ordinate: l'hash cambia solo se cambiano i dati.
    payload = json.dumps(
        catalog, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()

    output.mkdir(parents=True, exist_ok=True)
    bundle_path = output / "catalog.json.gz"
    # mtime=0 e filename vuoto: due esecuzioni con gli stessi dati producono lo
    # stesso .gz byte per byte (gzip normalmente ci scrive dentro data e nome).
    with bundle_path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            gz.write(payload)

    bundle_bytes = bundle_path.stat().st_size
    manifest = {
        "schema": SCHEMA_VERSION,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contentHash": content_hash,
        "sets": len(sets),
        "cards": len(cards),
        "bundle": bundle_path.name,
        "bundleBytes": bundle_bytes,
        "bundleSha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    print(
        f"{len(sets)} set, {len(cards)} carte -> {bundle_bytes / 1024:.0f} KB compressi "
        f"({len(payload) / 1024 / 1024:.1f} MB in chiaro), hash {content_hash[:12]}"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("source"),
        help="Checkout di PokemonTCG/pokemon-tcg-data (default: ./source)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist"),
        help="Cartella in cui scrivere il bundle (default: ./dist)",
    )
    args = parser.parse_args()
    build(args.source, args.output)


if __name__ == "__main__":
    main()