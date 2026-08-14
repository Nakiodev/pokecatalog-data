#!/usr/bin/env python3
"""Genera il bundle del catalogo carte di PokeCatalog partendo dai dati TCGdex.

Variante di `build_catalog.py`: cambia solo da dove arrivano i dati, il
formato di uscita e' identico byte per byte come struttura, quindi l'app non
va toccata (a parte gli id, che sono diversi: vedi README).

La sorgente e' l'albero JSON che TCGdex precompila con `bun run compile` e che
si trova gia' dentro l'immagine ufficiale `tcgdex/server`, in
`/usr/src/app/generated/<lingua>/`. Di quella cartella servono due soli file:

  sets.json   un record per espansione (id, nome, serie, conteggi, date)
  cards.json  TUTTE le carte, complete: rarita', categoria e set di appartenenza

Nota: `sets.json` contiene anche un elenco delle carte, ma ridotto ai soli
id/localId/nome. Le carte le leggiamo quindi da `cards.json`, che e' l'unico
posto dove ci sono `rarity` e `category`.

Uso:
    python build_catalog_tcgdex.py --source ./source --output ./dist
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

# Versione dello schema del bundle. Va incrementata solo se cambia la FORMA dei
# dati (campi aggiunti/rinominati): l'app rifiuta gli schemi che non conosce.
# Passare a TCGdex di per se' cambia i VALORI (gli id), non la forma; la 2
# arriva dai quattro campi in piu' per il riconoscimento (vedi slim_card).
SCHEMA_VERSION = 2

# TCGdex pubblica le immagini senza estensione: qualita' ed estensione si
# scelgono appendendole al path (".../mep/001" -> ".../mep/001/high.png").
# Usiamo png, che e' il formato sempre disponibile; webp peserebbe meno ma va
# verificato asset per asset prima di adottarlo.
IMG_SMALL_SUFFIX = "/low.png"
IMG_LARGE_SUFFIX = "/high.png"
ASSET_SUFFIX = ".png"


def card_images(base: str | None) -> tuple[str | None, str | None]:
    """Le due varianti usate dall'app, o (None, None) se la carta non ha immagine.

    Circa il 7% delle carte non ha `image` (promo recenti soprattutto): il
    bundle porta null e l'app mostra il segnaposto, come per qualsiasi altra
    carta senza anteprima.
    """
    if not base:
        return (None, None)
    return (base + IMG_SMALL_SUFFIX, base + IMG_LARGE_SUFFIX)


def asset_url(base: str | None) -> str | None:
    """Simbolo e logo del set: stessa storia delle immagini, manca l'estensione."""
    return base + ASSET_SUFFIX if base else None


# --- Recupero delle immagini mancanti -------------------------------------
#
# Circa il 7% delle carte non ha il campo `image`, ma il file sul CDN spesso
# c'e' lo stesso: manca solo il riferimento nei dati. Gli URL di TCGdex sono
# costruiti per convenzione
#
#   https://assets.tcgdex.net/<lingua>/<serie>/<set>/<localId>
#
# quindi possiamo ricostruirli e chiedere al CDN se esistono davvero. Si
# ricostruisce solo cio' che manca, e si tiene solo cio' che risponde: mai
# URL inventati nel bundle.
CDN = "https://assets.tcgdex.net"
USER_AGENT = "pokecatalog-build (+https://github.com/Nakiodev/pokecatalog-data)"


def exists(url: str, attempts: int = 3) -> bool:
    """True se il CDN serve questa risorsa.

    Un 404 e' una risposta definitiva (l'immagine non c'e'); qualsiasi altro
    intoppo viene ritentato, perche' un errore di rete non deve far sparire
    dal bundle un'immagine che invece esiste.
    """
    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    for tentativo in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.status == 200
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return False
            if e.code in (403, 405) and tentativo == 0:
                # CDN che non gradisce HEAD: ripiego su un GET del primo byte.
                request = urllib.request.Request(
                    url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
                )
        except Exception:
            pass
    return False


def fill_missing_images(
    cards: list[dict],
    sets: list[dict],
    series_by_set: dict[str, str],
    lang: str,
    workers: int,
) -> tuple[int, int]:
    """Completa immagini di carte, simboli e loghi mancanti. Torna (carte, set) recuperati."""
    candidate_cards = [
        (c, f"{CDN}/{lang}/{series_by_set.get(c['set'], '')}/{c['set']}/{c['number']}")
        for c in cards
        if not c["img"] and series_by_set.get(c["set"]) and c["number"]
    ]
    candidate_sets = [
        (s, f"{CDN}/univ/{series_by_set.get(s['id'], '')}/{s['id']}/symbol", "symbol")
        for s in sets
        if not s["symbol"] and series_by_set.get(s["id"])
    ] + [
        (s, f"{CDN}/{lang}/{series_by_set.get(s['id'], '')}/{s['id']}/logo", "logo")
        for s in sets
        if not s["logo"] and series_by_set.get(s["id"])
    ]

    print(
        f"verifico sul CDN {len(candidate_cards)} immagini di carte e "
        f"{len(candidate_sets)} fra simboli e loghi..."
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        card_hits = list(pool.map(
            lambda pair: exists(pair[1] + IMG_LARGE_SUFFIX), candidate_cards
        ))
        set_hits = list(pool.map(
            lambda triple: exists(triple[1] + ASSET_SUFFIX), candidate_sets
        ))

    recuperate = 0
    for (card, base), trovata in zip(candidate_cards, card_hits):
        if trovata:
            card["img"], card["imgHi"] = card_images(base)
            recuperate += 1

    recuperati_set = 0
    for (set_, base, campo), trovato in zip(candidate_sets, set_hits):
        if trovato:
            set_[campo] = base + ASSET_SUFFIX
            recuperati_set += 1

    return recuperate, recuperati_set


# Campi tenuti per ogni set. Tutto il resto (legalita', link a cardmarket, ...)
# viene scartato: l'app non lo usa e peserebbe solo sul download.
def slim_set(raw: dict) -> dict:
    count = raw.get("cardCount") or {}
    serie = raw.get("serie") or {}
    abbreviation = raw.get("abbreviation") or {}

    # `official` e' il numero stampato sulla carta (il "198" di 123/198),
    # `total` include le segrete. Attenzione: sui set promo `official` vale 0,
    # perche' su una promo il "/totale" non e' stampato. Zero e' il valore
    # giusto: dice al matcher che quel set non e' selezionabile per numero.
    printed_total = int(count.get("official") or 0)
    total = int(count.get("total") or printed_total)

    return {
        "id": raw["id"],
        "name": raw.get("name") or raw["id"],
        "series": serie.get("name") or "",
        "printedTotal": printed_total,
        "total": total,
        # In pokemontcg era `ptcgoCode`; qui la sigla ufficiale del set sta in
        # `abbreviation.official` ed e' quella che l'app cerca quando l'OCR
        # legge la sigla accanto al numero.
        "ptcgoCode": abbreviation.get("official"),
        "releaseDate": raw.get("releaseDate"),
        "symbol": asset_url(raw.get("symbol")),
        "logo": asset_url(raw.get("logo")),
    }


# Campi tenuti per ogni carta. Niente attacchi, testi, abilita': non si leggono
# da una foto e peserebbero moltissimo.
#
# Ai campi storici se ne aggiungono quattro scelti con un criterio solo: sono
# STAMPATI sulla carta, quindi l'OCR puo' leggerli e usarli per disambiguare
# quando nome e numero non bastano (holo rovinate, ristampe promo).
#
#   hp              in grande in alto a destra, cifra tonda e ben leggibile
#   regulationMark  la letterina in basso a sinistra: da sola taglia il
#                   catalogo per epoca di stampa
#   illustrator     "Illus. <nome>" a fondo carta: molto discriminante fra
#                   ristampe della stessa carta in set diversi
#   stage           "Basic" / "Stage 1" / "Stage 2" sopra il nome
def slim_card(raw: dict) -> dict:
    small, large = card_images(raw.get("image"))
    hp = raw.get("hp")
    return {
        "id": raw["id"],
        "set": (raw.get("set") or {}).get("id") or "",
        "name": raw.get("name") or "",
        # `localId` e' il numero come stampato sulla carta, zero incluso:
        # "001" sulle promo, "136" nelle espansioni normali. Lo teniamo
        # letterale, la normalizzazione per il confronto OCR la fa l'app.
        "number": str(raw.get("localId") or ""),
        "rarity": raw.get("rarity"),
        # In TCGdex si chiama `category` e vale Pokemon/Trainer/Energy (senza
        # accento, a differenza di pokemontcg che scriveva "Pokémon").
        "supertype": raw.get("category"),
        "img": small,
        "imgHi": large,
        "hp": int(hp) if isinstance(hp, (int, float)) else None,
        "regulationMark": raw.get("regulationMark"),
        "illustrator": raw.get("illustrator"),
        "stage": raw.get("stage"),
    }


_NUMBER_PARTS = re.compile(r"^([A-Za-z]*)(\d*)(.*)$")


def number_sort_key(number: str) -> tuple:
    """Ordina i numeri carta come farebbe un umano: 2 prima di 10, TG1 prima di TG12.

    Regge anche i localId che numeri non sono ("Museum" di mep, "!" di exu).
    """
    letters, digits, rest = _NUMBER_PARTS.match(number or "").groups()
    return (letters.upper(), int(digits) if digits else -1, rest.upper())


def build(
    source: Path,
    output: Path,
    lang: str = "en",
    fill_images: bool = False,
    workers: int = 8,
) -> dict:
    sets_file = source / "sets.json"
    cards_file = source / "cards.json"

    if not sets_file.is_file():
        sys.exit(f"File dei set non trovato: {sets_file}")
    if not cards_file.is_file():
        sys.exit(f"File delle carte non trovato: {cards_file}")

    raw_sets = json.loads(sets_file.read_text(encoding="utf-8"))
    sets = sorted(
        (slim_set(s) for s in raw_sets),
        key=lambda s: (s["releaseDate"] or "", s["id"]),
    )
    known_set_ids = {s["id"] for s in sets}
    # Serve a ricostruire gli URL del CDN: la serie sta solo qui, non dentro
    # il record `set` annidato nelle carte.
    series_by_set = {
        s["id"]: ((s.get("serie") or {}).get("id") or "") for s in raw_sets
    }

    # cards.json e' un unico array di ~23.500 carte complete: qualche decina di
    # MB, che stanno in memoria senza problemi sul runner.
    raw_cards = json.loads(cards_file.read_text(encoding="utf-8"))

    cards: list[dict] = []
    orphans: set[str] = set()
    for raw in raw_cards:
        card = slim_card(raw)
        if card["set"] not in known_set_ids:
            # Carta che punta a un set assente dall'indice: senza i conteggi del
            # set non e' utilizzabile per il matching, quindi la saltiamo.
            orphans.add(card["set"] or "(senza set)")
            continue
        cards.append(card)

    cards.sort(key=lambda c: (c["set"], number_sort_key(c["number"]), c["id"]))

    if orphans:
        print(f"Set ignorati (assenti da sets.json): {', '.join(sorted(orphans))}")

    if fill_images:
        recuperate, recuperati_set = fill_missing_images(
            cards, sets, series_by_set, lang, workers
        )
        print(
            f"immagini recuperate dal CDN -> carte: {recuperate}, "
            f"simboli/loghi: {recuperati_set}"
        )

    catalog = {
        "schema": SCHEMA_VERSION,
        "sets": sets,
        "cards": cards,
    }

    # separators compatto e nessun sort_keys: le chiavi restano nell'ordine in
    # cui le costruiamo qui ("sets" prima di "cards"), che e' quello che serve
    # all'app per importare in streaming senza tenere tutto in memoria.
    # L'output resta deterministico perche' ogni dict e' costruito sempre uguale
    # e le liste sono ordinate: l'hash cambia solo se cambiano i dati. Con
    # --fill-images entra in gioco anche il CDN, quindi l'hash puo' cambiare
    # perche' e' comparsa un'immagine nuova: e' proprio quello che vogliamo
    # pubblicare, ma e' il motivo per cui un run puo' differire dal precedente
    # anche a dati TCGdex invariati.
    payload = json.dumps(
        catalog, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    content_hash = hashlib.sha256(payload).hexdigest()

    output.mkdir(parents=True, exist_ok=True)
    bundle_path = output / "catalog.json.gz"
    # mtime=0 e filename vuoto: due esecuzioni con gli stessi dati producono lo
    # stesso .gz byte per byte (gzip normalmente ci scrive dentro data e nome).
    with bundle_path.open("wb") as raw_out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
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

    # Due numeri che vale la pena tenere d'occhio a ogni run, perche' sono i
    # punti in cui questa sorgente e' piu' debole della precedente.
    totale = max(len(cards), 1)
    senza_immagine = sum(1 for c in cards if not c["img"])
    senza_totale = sum(1 for s in sets if s["printedTotal"] == 0)
    print(
        f"{len(sets)} set, {len(cards)} carte -> {bundle_bytes / 1024:.0f} KB compressi "
        f"({len(payload) / 1024 / 1024:.1f} MB in chiaro), hash {content_hash[:12]}"
    )
    print(
        f"carte senza immagine: {senza_immagine} "
        f"({senza_immagine * 100 / totale:.1f}%) | "
        f"set senza totale stampato (promo): {senza_totale}"
    )
    # Copertura dei disambiguatori: se un campo crolla, il matcher che ci si
    # appoggia va tarato di conseguenza.
    coperture = " | ".join(
        f"{campo}: {sum(1 for c in cards if c[campo] is not None) * 100 / totale:.0f}%"
        for campo in ("hp", "regulationMark", "illustrator", "stage")
    )
    print(f"copertura disambiguatori -> {coperture}")

    # Le carte senza immagine, raggruppate per set: e' la lista da cui partire
    # se un giorno si vogliono tappare i buchi (vedi README).
    buchi: dict[str, int] = {}
    for c in cards:
        if not c["img"]:
            buchi[c["set"]] = buchi.get(c["set"], 0) + 1
    if buchi:
        peggiori = sorted(buchi.items(), key=lambda kv: -kv[1])[:10]
        print("set con piu' carte senza immagine: " + ", ".join(
            f"{set_id} ({n})" for set_id, n in peggiori
        ))
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("source"),
        help="Cartella con sets.json e cards.json di TCGdex (default: ./source)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("dist"),
        help="Cartella in cui scrivere il bundle (default: ./dist)",
    )
    parser.add_argument(
        "--lang",
        default="en",
        help="Lingua dei dati estratti, serve a ricostruire gli URL (default: en)",
    )
    parser.add_argument(
        "--fill-images",
        action="store_true",
        help="Cerca sul CDN le immagini che i dati non dichiarano e le aggiunge se esistono",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        help="Richieste in parallelo verso il CDN (default: 8)",
    )
    args = parser.parse_args()
    build(args.source, args.output, args.lang, args.fill_images, args.workers)


if __name__ == "__main__":
    main()
