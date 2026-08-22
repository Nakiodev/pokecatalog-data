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

Ogni lingua produce un catalogo suo, che sulla release convive con gli altri:

    --lang en   ->  catalog.json.gz     + manifest.json
    --lang ja   ->  catalog-ja.json.gz  + manifest-ja.json

Non sono traduzioni l'uno dell'altro: le carte giapponesi escono in espansioni
loro e numerate a modo loro, quindi sono cataloghi distinti anche come dati.
L'inglese tiene i nomi senza suffisso perche' sono quelli che le versioni
dell'app gia' installate vanno a cercare.

Al catalogo occidentale si aggiungono **nome e illustrazione delle carte nelle
altre lingue** (`--names it`), presi dalle stesse cartelle. Non e' un'altra
traduzione del catalogo: sono le stesse carte: il nome serve a riconoscerle
quando la copia in mano non e' in inglese, l'illustrazione a mostrare la carta
com'e' stampata davvero. Vedi `load_translations` e `image_langs`.

I nomi stanno nei dati; le illustrazioni no, e vanno chieste al CDN. Con
`--names` **e** `--fill-images` il generatore fa quindi un secondo giro di
richieste — una per carta e per lingua, tutto il catalogo, non solo le carte
senza immagine — per sapere quali hanno una scansione italiana. Vedi
`fill_image_langs`: e' il giro piu' lungo di tutto il build, e quanto duri lo
decide `--workers`.

Uso:
    python build_catalog_tcgdex.py --source ./source/en --output ./dist --names it
    python build_catalog_tcgdex.py --source ./source/ja --output ./dist --lang ja
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
# arriva dai quattro campi in piu' per il riconoscimento (vedi slim_card), la 3
# dai tipi della carta (Fuoco, Acqua...), che servono alle statistiche, la 4 da
# nomi e illustrazioni nelle altre lingue (vedi --names).
SCHEMA_VERSION = 4

# La lingua che tiene i nomi storici degli asset ("catalog.json.gz",
# "manifest.json"). Tutte le altre prendono un suffisso, cosi' i cataloghi
# convivono sulla stessa release senza sovrascriversi.
DEFAULT_LANG = "en"

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
# Nota: la serie "Pokemon TCG Pocket" (il gioco per cellulare) resta nel
# catalogo anche se quelle carte non esistono su cartoncino. Si possono
# consultare, semplicemente l'app le raggruppa in fondo: la scelta di dove
# metterle e' della UI, non di chi genera i dati.
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
) -> tuple[list[str], int]:
    """Completa immagini di carte, simboli e loghi mancanti.

    Torna gli id delle carte recuperate e quanti simboli/loghi.
    """
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

    # Gli id recuperati, non solo quanti: sono le carte la cui immagine esiste
    # sul CDN ma che i dati non dichiarano, cioe' un buco nel database e non
    # nell'archivio delle scansioni. E' la meta' della segnalazione che TCGdex
    # puo' chiudere senza che nessuno debba scansionare niente.
    recuperate: list[str] = []
    for (card, base), trovata in zip(candidate_cards, card_hits):
        if trovata:
            card["img"], card["imgHi"] = card_images(base)
            recuperate.append(card["id"])

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
#
# `types` invece non serve al riconoscimento: e' il tipo del Pokemon (Fire,
# Water, Grass...), quello del simbolo in alto a destra, e sta nel bundle perche'
# le statistiche dell'app dividono la collezione per tipo. Le carte Allenatore ed
# Energia non ce l'hanno, e li' l'app ripiega su `supertype`.
def card_types(raw: dict) -> list[str] | None:
    """I tipi della carta, o None per chi non ne ha (Allenatore, Energia).

    Lista e non stringa perche' TCGdex la da' cosi' e qualche carta ne ha piu'
    d'uno: chi legge decide cosa farne, il bundle non sceglie per lui.
    """
    types = raw.get("types")
    if not isinstance(types, list):
        return None
    kept = [str(t) for t in types if t]
    return kept or None


# --- Nomi nelle altre lingue ----------------------------------------------
#
# Il catalogo e' in inglese, ma le carte in mano a chi usa l'app spesso no: una
# carta italiana si chiama "Cyndaquil di Armonio" dove il catalogo dice "Ethan's
# Cyndaquil", e le due stringhe non si somigliano affatto. Per il riconoscimento
# e' il caso peggiore possibile, perche' il nome e' meta' del punteggio: il
# matcher finiva per preferire una carta qualsiasi con il nome inglese piu'
# simile alla lettura.
#
# Qui si aggiungono quindi i nomi delle altre lingue, presi dalle stesse cartelle
# `generated/<lingua>` da cui esce tutto il resto. Due scelte che valgono la pena
# di essere dette:
#
#   - si tiene un nome **solo se e' diverso** da quello inglese. Meta' delle
#     carte si chiamano uguale in tutte le lingue ("Pikachu" e' "Pikachu"), e
#     ripetere quella meta' vorrebbe dire pagare peso per un dato che l'app ha
#     gia'. A differire sono soprattutto Allenatori, Energie e carte intestate a
#     un personaggio, cioe' proprio quelle che oggi il matcher sbaglia;
#   - l'aggancio e' l'**id della carta**, che in TCGdex e' lo stesso in tutte le
#     lingue occidentali. Se un giorno non lo fosse piu', la copertura stampata
#     in fondo al run crollerebbe a zero: e' li' che ce ne si accorge, prima di
#     pubblicare.
#
# Il catalogo giapponese non c'entra: quelle carte sono stampate in giapponese e
# il loro nome ce l'hanno gia'.
def load_translations(source: Path, langs: list[str]) -> dict[str, dict[str, dict]]:
    """Per ogni lingua, nome e immagine delle carte per id.

    {"it": {"sv10-032": {"name": "Cyndaquil di Armonio", "image": "https://..."}}}
    """
    translations: dict[str, dict[str, dict]] = {}
    for lang in langs:
        cards_file = source / lang / "cards.json"
        if not cards_file.is_file():
            sys.exit(f"Lingua '{lang}': file non trovato ({cards_file})")
        raw = json.loads(cards_file.read_text(encoding="utf-8"))
        translations[lang] = {
            c["id"]: {"name": c.get("name"), "image": c.get("image")}
            for c in raw
            if c.get("id")
        }
    return translations


def other_names(
    card_id: str,
    english: str,
    translations: dict[str, dict[str, dict]],
) -> dict[str, str] | None:
    """I nomi diversi da quello inglese, per lingua, o None se non ce ne sono."""
    kept = {
        lang: nome
        for lang, per_id in translations.items()
        if (nome := (per_id.get(card_id) or {}).get("name")) and nome != english
    }
    return kept or None


# --- L'illustrazione nella lingua della carta ------------------------------
#
# Una carta italiana e' *stampata* in italiano: il nome sull'illustrazione, il
# testo, il riquadro degli attacchi. Mostrare la scansione inglese a chi ha in
# mano quella italiana e' come mostrare un'altra carta, e il CDN di TCGdex le ha
# tutte e due — cambia un solo segmento dell'URL:
#
#   https://assets.tcgdex.net/en/sv/sv10/032   ->   .../it/sv/sv10/032
#
# Nel bundle non finiscono quindi due URL per carta, che sarebbero un megabyte e
# mezzo di stringhe quasi identiche: resta l'URL di sempre piu' l'elenco delle
# **altre** lingue che hanno una scansione loro. L'app fa lo scambio del segmento,
# che e' una regola sola e vale per tutte.
#
# Quando l'inglese non ce l'ha e un'altra lingua si', e' quell'altra a finire
# nell'URL: e' l'unica immagine che esiste, e mostrarla e' meglio del segnaposto.
# Per questo la lingua dell'URL non si da' per scontata da nessuna parte.
def image_langs(
    card_id: str,
    chosen: str,
    translations: dict[str, dict[str, dict]],
) -> list[str] | None:
    """Le altre lingue con una scansione propria, esclusa quella gia' in `img`.

    Guarda quello che i dati *dichiarano*. Oggi non dichiarano niente: nel
    `cards.json` italiano estratto dall'immagine di TCGdex il campo `image` non
    c'e' per nessuna carta, mentre il nome c'e' per migliaia. Il bundle usciva
    quindi senza nemmeno un `imgLangs`, e l'app — che scambia il segmento della
    lingua solo per le lingue dichiarate — mostrava l'illustrazione inglese anche
    a chi legge l'app in italiano.

    Le scansioni pero' esistono: `assets.tcgdex.net/it/me/me05/001/low.png`
    risponde. A trovarle ci pensa [fill_image_langs], che le chiede al CDN come
    gia' si fa per le immagini mancanti. Questa funzione resta perche' e' la
    strada buona — se un giorno i dati le dichiarassero, non servirebbe chiedere
    niente a nessuno.
    """
    kept = sorted(
        lang
        for lang, per_id in translations.items()
        if lang != chosen and (per_id.get(card_id) or {}).get("image")
    )
    return kept or None


def fill_image_langs(
    cards: list[dict],
    series_by_set: dict[str, str],
    langs: list[str],
    chosen: str,
    workers: int,
) -> dict[str, int]:
    """Chiede al CDN quali carte hanno una scansione nelle altre lingue.

    Una richiesta per carta e per lingua, che e' molto piu' del giro di
    [fill_missing_images] — quello prova solo le carte che un'immagine non ce
    l'hanno affatto, qui si prova tutto il catalogo. E' il prezzo per sapere una
    cosa che i dati non dicono, e la copertura non e' indovinabile: le espansioni
    recenti hanno la scansione italiana, il Set Base del 1999 no. Con `--workers`
    si decide quanto in fretta.

    Quando la carta un'immagine non ce l'ha in nessuna lingua e questa esiste, e'
    questa a diventare la sua: l'unica scansione che c'e' e' meglio del
    segnaposto, ed e' il motivo per cui la lingua di `img` non si da' per
    scontata da nessuna parte.

    Torna quante carte ha trovato per lingua: e' il numero da guardare nel log
    quando la copertura cambia.
    """
    others = [lang for lang in langs if lang != chosen]
    if not others:
        return {}

    candidates = [
        (
            card,
            lang,
            f"{CDN}/{lang}/{series_by_set.get(card['set'], '')}/{card['set']}/{card['number']}",
        )
        for lang in others
        for card in cards
        if series_by_set.get(card["set"])
        and card["number"]
        and lang not in (card.get("imgLangs") or [])
    ]

    print(
        f"verifico sul CDN {len(candidates)} illustrazioni nelle lingue "
        f"{', '.join(others)}..."
    )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        hits = list(pool.map(lambda triple: exists(triple[2] + IMG_LARGE_SUFFIX), candidates))

    found = {lang: 0 for lang in others}
    for (card, lang, base), trovata in zip(candidates, hits):
        if not trovata:
            continue
        found[lang] += 1
        if not card["img"]:
            # L'unica scansione che esiste diventa quella della carta, e la sua
            # lingua non va fra le "altre": e' gia' quella dell'URL.
            card["img"], card["imgHi"] = card_images(base)
            continue
        card["imgLangs"] = sorted({*(card.get("imgLangs") or []), lang})
    return found


def printed_rarity(raw: str | None) -> str | None:
    """La rarita' stampata, o None se la carta non ne ha una."""
    if not raw or raw.strip().lower() == "none":
        return None
    return raw


def slim_card(
    raw: dict,
    lang: str = DEFAULT_LANG,
    translations: dict[str, dict[str, dict]] | None = None,
) -> dict:
    hp = raw.get("hp")
    english = raw.get("name") or ""

    # L'illustrazione: quella della lingua che si sta generando, e se non c'e'
    # la prima delle altre che ce l'ha. La lingua scelta si porta dietro se'
    # stessa, perche' da li' in poi tutto — l'elenco delle altre lingue, lo
    # scambio che fara' l'app — si misura sull'URL che e' finito nel bundle.
    base = raw.get("image")
    chosen = lang
    if not base and translations:
        for other, per_id in sorted(translations.items()):
            candidate = (per_id.get(raw["id"]) or {}).get("image")
            if candidate:
                base, chosen = candidate, other
                break
    small, large = card_images(base)

    card = {
        "id": raw["id"],
        "set": (raw.get("set") or {}).get("id") or "",
        "name": english,
        # `localId` e' il numero come stampato sulla carta, zero incluso:
        # "001" sulle promo, "136" nelle espansioni normali. Lo teniamo
        # letterale, la normalizzazione per il confronto OCR la fa l'app.
        "number": str(raw.get("localId") or ""),
        # "None" non e' una rarita': e' la sorgente che dice che la carta non ne
        # ha una stampata (energie base, promo senza simbolo). Sul catalogo
        # giapponese sono duemila carte su dodicimila, e tenerlo per buono
        # vorrebbe dire un nome di rarita' che non esiste su un sesto delle
        # carte. Assente e' l'unica cosa vera, ed e' un caso che l'app gia'
        # conosce: sono le stesse carte che qui hanno il campo vuoto.
        "rarity": printed_rarity(raw.get("rarity")),
        # In TCGdex si chiama `category` e vale Pokemon/Trainer/Energy (senza
        # accento, a differenza di pokemontcg che scriveva "Pokémon").
        "supertype": raw.get("category"),
        "types": card_types(raw),
        "img": small,
        "imgHi": large,
        "hp": int(hp) if isinstance(hp, (int, float)) else None,
        "regulationMark": raw.get("regulationMark"),
        "illustrator": raw.get("illustrator"),
        "stage": raw.get("stage"),
    }

    # Le due chiavi delle altre lingue ci sono **solo quando hanno qualcosa
    # dentro**, al contrario di tutte le altre che portano null quando il dato
    # manca. La differenza e' che queste sarebbero nulle quasi sempre:
    # ventitremila `"names":null` sono un terzo di megabyte di niente, e
    # l'assenza della chiave l'app la legge esattamente come la legge nulla.
    if translations:
        altri = other_names(card["id"], english, translations)
        if altri:
            card["names"] = altri
        lingue = image_langs(card["id"], chosen, translations)
        if lingue:
            card["imgLangs"] = lingue
    return card


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
    name_langs: list[str] | None = None,
    names_source: Path | None = None,
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

    # Le altre lingue stanno in cartelle sorelle di quella che si sta generando:
    # `source/en` per il catalogo, `source/it` per nomi e illustrazioni italiane.
    translations = (
        load_translations(names_source or source.parent, name_langs)
        if name_langs
        else {}
    )

    cards: list[dict] = []
    orphans: set[str] = set()
    for raw in raw_cards:
        card = slim_card(raw, lang, translations)
        if card["set"] not in known_set_ids:
            # Carta che punta a un set assente dall'indice: senza i conteggi del
            # set non e' utilizzabile per il matching, quindi la saltiamo.
            orphans.add(card["set"] or "(senza set)")
            continue
        cards.append(card)

    cards.sort(key=lambda c: (c["set"], number_sort_key(c["number"]), c["id"]))

    if orphans:
        print(f"Set ignorati (assenti da sets.json): {', '.join(sorted(orphans))}")

    recuperate: list[str] = []
    if fill_images:
        recuperate, recuperati_set = fill_missing_images(
            cards, sets, series_by_set, lang, workers
        )
        print(
            f"immagini recuperate dal CDN -> carte: {len(recuperate)}, "
            f"simboli/loghi: {recuperati_set}"
        )

        # Dopo, non prima: il giro qui sopra puo' aver dato un'immagine a una
        # carta che non ne aveva, e da li' in poi la domanda su questa carta
        # cambia — non "quale scansione esiste" ma "ne esiste anche un'altra".
        if name_langs:
            per_lingua = fill_image_langs(cards, series_by_set, name_langs, lang, workers)
            if per_lingua:
                print(
                    "illustrazioni trovate per lingua -> "
                    + ", ".join(f"{k}: {v}" for k, v in sorted(per_lingua.items()))
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
    # I cataloghi pubblicati sono uno per lingua e stanno sulla stessa release,
    # quindi il nome dell'asset deve dire di quale si tratta. L'inglese conserva
    # il nome storico senza suffisso: e' l'asset che cercano le installazioni
    # gia' in giro, e rinominarlo vorrebbe dire lasciarle senza catalogo.
    suffix = "" if lang == DEFAULT_LANG else f"-{lang}"
    bundle_path = output / f"catalog{suffix}.json.gz"
    # mtime=0 e filename vuoto: due esecuzioni con gli stessi dati producono lo
    # stesso .gz byte per byte (gzip normalmente ci scrive dentro data e nome).
    with bundle_path.open("wb") as raw_out:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw_out, mtime=0) as gz:
            gz.write(payload)

    bundle_bytes = bundle_path.stat().st_size
    manifest = {
        "schema": SCHEMA_VERSION,
        # La lingua dei dati. L'app la controlla prima di importare: un catalogo
        # giapponese finito nel database occidentale non somiglierebbe a un
        # errore ma a un riconoscimento impazzito, che propone carte plausibili e
        # sbagliate. Meglio accorgersene qui.
        "lang": lang,
        "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "contentHash": content_hash,
        "sets": len(sets),
        "cards": len(cards),
        "bundle": bundle_path.name,
        "bundleBytes": bundle_bytes,
        "bundleSha256": hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
    }
    (output / f"manifest{suffix}.json").write_text(
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
    # I tipi non disambiguano niente, ma se sparissero dalla sorgente le
    # statistiche dell'app si svuoterebbero senza dirlo a nessuno: il conto sta
    # qui perche' un crollo si veda al run, non a valle.
    con_tipo = sum(1 for c in cards if c["types"])
    print(f"carte con almeno un tipo: {con_tipo} ({con_tipo * 100 / totale:.0f}%)")

    # I nomi nelle altre lingue: quante carte ne hanno uno e quante lo hanno
    # davvero diverso dall'inglese.
    #
    # Il primo numero e' anche la prova che gli id combacino fra una lingua e
    # l'altra: se TCGdex li numerasse diversamente uscirebbe zero, e quello che
    # si sta per pubblicare sarebbe un catalogo senza nessun nome tradotto. Il
    # secondo e' quello che finisce nel bundle, e spiega il peso qui sopra.
    for lingua in sorted(translations):
        trovati = sum(1 for c in cards if c["id"] in translations[lingua])
        diversi = sum(1 for c in cards if lingua in (c.get("names") or {}))
        illustrate = sum(1 for c in cards if lingua in (c.get("imgLangs") or []))
        print(
            f"nomi {lingua}: {trovati}/{len(cards)} carte "
            f"({trovati * 100 / totale:.0f}%), di cui {diversi} diversi "
            f"dall'inglese ({diversi * 100 / totale:.0f}% del catalogo) | "
            f"illustrazioni {lingua}: {illustrate} ({illustrate * 100 / totale:.0f}%)"
        )
        if not trovati:
            print(
                f"::warning::Nessun nome '{lingua}' agganciato: gli id di quella "
                f"lingua non corrispondono a quelli inglesi."
            )

    # Le carte la cui unica illustrazione non e' nella lingua del catalogo: e'
    # il ripiego al contrario, e va detto perche' e' l'unico caso in cui il
    # bundle porta un URL in una lingua che non e' la sua.
    if translations:
        altrui = sum(
            1
            for c in cards
            if c["img"] and f"/{lang}/" not in c["img"]
        )
        if altrui:
            print(
                f"carte illustrate solo in un'altra lingua: {altrui} "
                f"({altrui * 100 / totale:.1f}%)"
            )

    if translations:
        # Quanto costano davvero, compressi: e' l'unico numero che conta per chi
        # scarica, e non si ricava da quello in chiaro. Si ricomprime il
        # catalogo senza le due chiavi e si guarda la differenza.
        senza = json.dumps(
            {
                **catalog,
                "cards": [
                    {k: v for k, v in c.items() if k not in ("names", "imgLangs")}
                    for c in cards
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        costo = bundle_bytes - len(gzip.compress(senza))
        print(
            f"peso delle altre lingue: {costo / 1024:+.0f} KB compressi "
            f"({costo * 100 / max(bundle_bytes - costo, 1):+.1f}%)"
        )

    # Tutte le rarita' che compaiono, con quante carte ciascuna.
    #
    # L'app le traduce, le colora e le ordina con una tabella scritta a mano
    # (Rarities.kt), e per un catalogo in un'altra lingua quella tabella si puo'
    # compilare solo sapendo *quali stringhe* la sorgente usa davvero. Stamparle
    # qui vuol dire che il log di un run e' anche l'elenco delle voci da
    # aggiungere: quelle che non compaiono nella tabella non rompono niente, ma
    # si leggono cosi' come sono e finiscono d'ufficio fra le rare.
    rarita: dict[str, int] = {}
    for c in cards:
        chiave = c["rarity"] or "(senza rarita')"
        rarita[chiave] = rarita.get(chiave, 0) + 1
    print("rarita' presenti: " + ", ".join(
        f"{nome} ({n})" for nome, n in sorted(rarita.items(), key=lambda kv: -kv[1])
    ))

    # Le carte senza immagine, raggruppate per set: e' la lista da cui partire
    # se un giorno si vogliono tappare i buchi (vedi README).
    buchi: dict[str, list[str]] = {}
    for c in cards:
        if not c["img"]:
            buchi.setdefault(c["set"], []).append(c["id"])
    if buchi:
        peggiori = sorted(buchi.items(), key=lambda kv: -len(kv[1]))[:10]
        print("set con piu' carte senza immagine: " + ", ".join(
            f"{set_id} ({len(ids)})" for set_id, ids in peggiori
        ))

    # Il rapporto completo, che il log non puo' contenere: serve a segnalare i
    # buchi a chi i dati li mantiene. Distingue i due casi, che si chiudono in
    # modi diversi:
    #
    #   recovered  l'immagine sul CDN c'e', i dati non la dichiarano. E' un buco
    #              del database, si chiude senza scansionare niente.
    #   missing    l'immagine non c'e' da nessuna parte: serve una scansione.
    #
    # Non finisce sulla release: e' un allegato per una segnalazione, non un
    # pezzo del catalogo. Il workflow lo carica come artifact.
    nomi_set = {s["id"]: s["name"] for s in sets}
    report = {
        "lang": lang,
        "generatedAt": manifest["generatedAt"],
        "cards": len(cards),
        "recoveredFromCdn": sorted(recuperate),
        "missingBySet": {
            set_id: {
                "name": nomi_set.get(set_id, ""),
                "count": len(ids),
                "cards": sorted(ids),
            }
            for set_id, ids in sorted(buchi.items(), key=lambda kv: -len(kv[1]))
        },
        "missingTotal": sum(len(ids) for ids in buchi.values()),
    }
    report_path = output / f"immagini-mancanti{suffix}.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"rapporto immagini -> {report_path.name}: {report['missingTotal']} carte "
        f"senza immagine in {len(buchi)} set, {len(recuperate)} recuperate dal CDN"
    )
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
        "--names",
        default="",
        help=(
            "Lingue di cui aggiungere nome e illustrazione delle carte, "
            "separate da virgola (es. 'it'). Servono a riconoscere e a mostrare "
            "le carte non inglesi: vedi load_translations. "
            "Vuoto = solo quello che c'e' in --lang"
        ),
    )
    parser.add_argument(
        "--names-source",
        type=Path,
        default=None,
        help=(
            "Cartella che contiene le lingue di --names, una sottocartella "
            "ciascuna con dentro cards.json (default: la cartella che contiene "
            "--source)"
        ),
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
    name_langs = [lang for lang in args.names.replace(" ", ",").split(",") if lang]
    build(
        args.source,
        args.output,
        args.lang,
        args.fill_images,
        args.workers,
        name_langs=name_langs,
        names_source=args.names_source,
    )


if __name__ == "__main__":
    main()
