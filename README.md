# pokecatalog-data

Il catalogo delle carte che l'app **PokeCatalog** scarica al primo avvio.

Qui non c'è nessuna carta: ci sono lo script che *costruisce* il catalogo e la
GitHub Action che lo rigenera ogni lunedì. Il risultato — un unico file
compresso più il suo manifest — viene pubblicato come asset di una release, e
quella release è l'unica cosa che l'app va a prendere. Poi l'app funziona
offline: nel telefono il catalogo finisce in un database locale, e serve la rete
solo per le illustrazioni.

I file di questa cartella sono la copia pubblicata di `catalog-data/` nel
repository dell'app: le due copie vanno tenute allineate a mano.

## Cosa pubblica

Sulla release con tag fisso **`catalog-tcgdex`**, quindi con URL che non cambiano
mai:

```
https://github.com/<owner>/<repo>/releases/download/catalog-tcgdex/manifest.json
https://github.com/<owner>/<repo>/releases/download/catalog-tcgdex/catalog.json.gz
```

L'app scarica prima `manifest.json`, che pesa poche centinaia di byte:

```json
{
  "schema": 3,
  "generatedAt": "2026-08-18T05:31:04Z",
  "contentHash": "…",
  "sets": 218,
  "cards": 23500,
  "bundle": "catalog.json.gz",
  "bundleBytes": 1234567,
  "bundleSha256": "…"
}
```

Se `contentHash` è uguale a quello già importato nel telefono, il bundle non
viene nemmeno richiesto: un avvio a catalogo aggiornato costa una richiesta e
mezzo kilobyte. `contentHash` è l'hash del JSON in chiaro, quindi dipende dai
dati e non dalla compressione.

## Come funziona il workflow

`.github/workflows/build-catalog-tcgdex.yml`, un lunedì su l'altro alle 05:30 UTC
oppure a mano da **Actions → build-catalog-tcgdex → Run workflow**:

1. scarica l'immagine Docker ufficiale `tcgdex/server` ed estrae con
   `docker export` la cartella `/usr/src/app/generated/<lingua>/`, dove TCGdex
   tiene i suoi dati già compilati. Servono due file soli: `sets.json` e
   `cards.json`. Nessuna richiesta all'API pubblica, nessuna toolchain Bun da
   mantenere;
2. lancia `build_catalog_tcgdex.py`, che tiene solo i campi utili, ricostruisce
   le immagini mancanti (`--fill-images`) e scrive `dist/catalog.json.gz` +
   `dist/manifest.json`;
3. confronta il `contentHash` con quello della release già pubblicata: **se i
   dati non sono cambiati non pubblica niente**;
4. carica i due asset sulla release, sostituendo i precedenti.

Il bundle è riproducibile: a parità di dati in ingresso i byte in uscita sono gli
stessi (liste ordinate, gzip senza data né nome del file dentro). L'eccezione è
`--fill-images`, che interroga il CDN: se nel frattempo è comparsa
un'illustrazione che prima mancava, l'hash cambia — ed è proprio quello che si
vuole pubblicare.

## Il formato del bundle

`catalog.json.gz` è un JSON solo, con le chiavi in quest'ordine: `schema`,
`sets`, `cards`. L'ordine conta, perché l'app lo legge in streaming e vuole
sapere lo schema prima di importare qualsiasi cosa.

Per ogni espansione: `id`, `name`, `series`, `printedTotal`, `total`,
`ptcgoCode`, `releaseDate`, `symbol`, `logo`.

Per ogni carta: `id`, `set`, `name`, `number`, `rarity`, `supertype`, `types`,
`img`, `imgHi`, `hp`, `regulationMark`, `illustrator`, `stage`. Niente attacchi,
testi o abilità: non si leggono da una fotografia e peserebbero moltissimo.

I quattro campi in fondo sono lì per un motivo solo: sono **stampati sulla
carta**, quindi l'OCR può leggerli e usarli per distinguere due carte che
condividono nome e numero (le ristampe promo, soprattutto). `types` invece —
`["Fire"]`, `["Water"]`, undici valori in tutto — non serve al riconoscimento ma
alle statistiche dell'app, che dividono la collezione per tipo; è una lista
perché qualche carta ne ha più d'uno, ed è `null` per Allenatori ed Energie, che
un tipo non ce l'hanno.

### Lo schema

| schema | cosa aggiunge |
|---|---|
| 1 | il bundle originale, generato da pokemon-tcg-data |
| 2 | `hp`, `regulationMark`, `illustrator`, `stage` |
| 3 | `types` |

L'app dichiara un **intervallo** di schemi accettati, non un numero solo: un
bundle di schema precedente si importa lo stesso, semplicemente senza i campi
nuovi. Quindi non conta l'ordine fra il run di questa action e l'aggiornamento
dell'app — conta solo non pubblicare mai uno schema **più alto** di quello che
l'app conosce, perché quello viene rifiutato.

## Lanciarlo in locale

Serve Python 3 e la cartella `generated/<lingua>` di TCGdex (o i due file
`sets.json` e `cards.json` presi da lì):

```bash
python build_catalog_tcgdex.py --source ./source --output ./dist --lang en --fill-images
```

Alla fine stampa i numeri che vale la pena guardare a ogni run: quante carte sono
senza immagine, quanti set sono senza totale stampato, la copertura dei campi che
servono al riconoscimento e quante carte hanno almeno un tipo. Se una di queste
percentuali crolla, è la sorgente che è cambiata.

## Immagini mancanti

Circa il 7% delle carte non ha il campo `image` nei dati TCGdex, ma il file sul
CDN spesso **esiste lo stesso**: manca solo il riferimento. Gli URL seguono una
convenzione (`assets.tcgdex.net/<lingua>/<serie>/<set>/<localId>`), quindi con
`--fill-images` il generatore ricostruisce gli indirizzi mancanti, chiede al CDN
se rispondono e tiene solo quelli che esistono davvero: nel bundle non finiscono
mai URL inventati, e qui non si ospita nessuna immagine.

## La variante storica (pokemon-tcg-data)

`build_catalog.py` e `build-catalog.yml` sono il generatore precedente: stessi
asset, ma partendo da
[`PokemonTCG/pokemon-tcg-data`](https://github.com/PokemonTCG/pokemon-tcg-data) e
sul tag `catalog`. Restano qui perché tornare indietro sia una riga di
configurazione, ma l'app non li usa più.

Il passaggio a TCGdex è stato fatto per le promo: pokemon-tcg-data non ha nessun
set promozionale di Mega Evolution e si ferma a 72 carte per `svp` contro le 225
reali, mentre TCGdex ha 218 set e circa 23.500 carte, promo comprese. In più
TCGdex dichiara una licenza (MIT) e pokemon-tcg-data no.

Attenzione, gli **id** delle due sorgenti sono diversi: cambiano sia quelli dei
set (`me1` → `me01`, `hsp` → `hgssp`) sia quelli delle carte (`sv3-136` →
`mep-001`, con lo zero davanti). La collezione dell'utente salva l'id della
carta, quindi passando da una sorgente all'altra le carte già possedute restano
leggibili ma scollegate dal catalogo.

## Dati, licenze, marchi

I dati vengono da [TCGdex](https://github.com/tcgdex/cards-database), un progetto
open source mantenuto dalla sua comunità, distribuito con **licenza MIT**. Il
testo della licenza e la nota di copyright sono in [NOTICE](NOTICE), come la MIT
richiede a chi ridistribuisce, e sono riportati anche dentro l'app
(Impostazioni → Crediti → Licenze).

Il bundle contiene metadati fattuali sulle carte — nome, numero, rarità, totale
del set — e gli **URL** delle illustrazioni, che restano servite da
`assets.tcgdex.net`: qui non si ridistribuisce nessuna immagine.

Questo progetto non è prodotto, approvato né affiliato a Nintendo, The Pokémon
Company, Creatures Inc. o GAME FREAK. Nomi, immagini e marchi delle carte
appartengono ai rispettivi proprietari.
