# dutch-handelingen

Download the Dutch parliamentary proceedings (*Handelingen*) from the official open-data
repository of the Dutch government and turn them into a HuggingFace dataset.

Published dataset: [yhavinga/dutch-handelingen-modern](https://huggingface.co/datasets/yhavinga/dutch-handelingen-modern)
(Tweede Kamer, Eerste Kamer and joint sessions, 1994–2026, ~62k agenda items, ~452M tokens).

## Pipeline

1. **Index**: the document list comes from the SRU 2.0 API (`https://repository.overheid.nl/sru`),
   queried per session year (*vergaderjaar*), because SRU does not page beyond 10,000 results.
   Each record carries the direct file URLs and the metadata (date, title, meeting number).
   Result: `data/handelingen/index_modern_sru.json`.
2. **Download**: the XML of every document, from `repository.overheid.nl/frbr/officielepublicaties/`.
   Idempotent: already downloaded files are skipped, so a run can be stopped and restarted.
3. **Convert**: both XML schemas (older `<handeling>`, newer `op-xsd-2012`) are parsed into one
   row per agenda item, with speaker turns `{speaker, party, text}` and a flat `text` field.
4. **Publish**: push the converted dataset to the HuggingFace Hub.

## Output

One row per agenda item. A vote in the Tweede Kamer on 24 October 1995 (5 of the 11 blocks
shown; narrative blocks have no speaker):

```json
{
  "id": "h-tk-19951996-16-1044-1044-2",
  "chamber": "tweede_kamer",
  "session_year": "1995-1996",
  "meeting_number": 16,
  "date": "1995-10-24",
  "title": "Stemming over een motie, ingediend bij de behandeling van de begroting van Binnenlandse Zaken, te weten: de motie-Rehwinkel c.s. over kiesrecht voor niet-Nederlanders (24400-VII nr. 12)",
  "turns": [
    {"speaker": null, "party": null, "text": "Aan de orde is de stemming over een motie, ingediend bij de behandeling van de begroting van Binnenlandse Zaken, te weten: - de motie-Rehwinkel c.s. over kiesrecht voor niet-Nederlanders (24400-VII, nr. 12)."},
    {"speaker": "De voorzitter", "party": null, "text": "Ik constateer, dat de aanwezige leden van de fracties van de PvdA, GroenLinks, de SP, D66, de VVD, het CDA, de groep-Nijpels, het AOV, de CD en het lid Hendriks voor deze motie hebben gestemd en die van de overige fracties ertegen, zodat zij is aangenomen."},
    {"speaker": "De heer Rehwinkel", "party": "PvdA", "text": "Gezien de uitlatingen van de minister tijdens het debat van de vorige week, vraag ik hem wat de reactie van de regering is op het aanvaarden van deze motie."},
    {"speaker": "Minister Dijkstal", "party": null, "text": "Voorzitter! Mijn grondhouding is dat wij de motie zullen uitvoeren. Ik zal hierover in het kabinet nog beraad voeren en de Kamer op de hoogte stellen."},
    {"speaker": "De heer Gabor", "party": "CDA", "text": "Is het mogelijk dat wij vóór donderdag van de minister horen wat de reactie is?"}
  ],
  "text": "Stemmingen\n\nAan de orde is ...\n\nDe heer Rehwinkel (PvdA):\nGezien de uitlatingen van de minister ..."
}
```

## Usage

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1+2: build the SRU index and download the XML in a detached screen session
./scripts/start_handelingen_downloads.sh modern
./scripts/start_handelingen_downloads.sh status
./scripts/start_handelingen_downloads.sh stop

# or in the foreground
.venv/bin/python scripts/download_handelingen.py -o data/handelingen -s modern --formats xml -c 2 --delay 1

# 3: convert; stops with an error if an indexed XML file is missing
.venv/bin/python scripts/convert_handelingen_to_hf.py --modern-only -i data/handelingen -o data/hf_datasets
```

```python
# 4: publish
from datasets import load_from_disk
load_from_disk("data/hf_datasets/handelingen-debates").push_to_hub(
    "yhavinga/dutch-handelingen-modern", max_shard_size="500MB")
```

The full download is about 2 GB of XML and takes a few hours (about 5 documents/s). The repository rate limits
clients (HTTP 429, without a published limit; the site asks bulk users to use the API and keep
the load low), so the defaults are 2 concurrent requests with a 1 s delay, and rate-limited
requests are retried with backoff. Documents that still fail are logged as warnings; rerun the
download to fetch them.

## Notes on the source

Things learned while building the dataset (November 2026).

**Access**

- Use the SRU API, not the HTML directory listings under `repository.overheid.nl/frbr/`. Crawling
  those listings missed about 8% of the documents in every year, and most of 2013-14 and
  2024-26. SRU lists 62,598 Handelingen, of which 62,163 belong to the three chambers; the rest
  are appendices (`blg-*`) and a few legacy ids.
- SRU returns at most 1,000 records per request and times out (HTTP 504) when paging beyond
  10,000 results, hence one query per session year (each has fewer than 3,000).
- An SRU record has everything needed: direct URLs per format (xml, pdf, html, odt, metadata)
  and the date of the sitting, title, session year and meeting number. Separate `metadata.xml`
  downloads are not needed.
- Rate limiting: HTTP 429 without `Retry-After` or rate-limit headers, per client and also on
  the HTML index pages. The SRU manual only mentions a fair use policy. Occasional 504s occur;
  a second run fetches what failed.
- The older endpoint `zoek.officielebekendmakingen.nl/sru/Search` (SRU 1.2) returns HTTP 500.
- TLS verifies fine with the certifi CA bundle.

**Content and structure**

- One publication is one agenda item, not a whole sitting. Older ids encode page numbers
  (`h-tk-19981999-83-107`), newer ids sitting and item numbers (`h-tk-20202021-102-2`).
- Two XML schemas: the older `<handeling>` (until about 2010, occasionally later) and
  `op-xsd-2012` `<officiele-publicatie>`. The older XML contains no date of the sitting.
- Within `op-xsd-2012`, speakers appear in three ways: `<spreekbeurt>` with `<voorvoegsels>`;
  the title as plain text inside `<spreker>` (`<spreker>Staatssecretaris <naam>…`); or a whole
  dialogue in a single `<tekst>`, with bold printed labels (`De <nadruk>voorzitter</nadruk>:`).
- Quirks handled by the converter: surnames in sort order (`Campen van` → `Van Campen`); the
  printed label repeated as the first paragraph of a speech (2020s); soft hyphens; a change of
  chair stored as two elements (`<vrzlabel>Voorzitter:</vrzlabel><vrznaam>Tummers</vrznaam>`);
  inline markup without surrounding whitespace (`<actie>stemmingen</actie>in`,
  `CO<inf>2</inf>`, `<vetnr>24400</vetnr>-VII`).
- Not corrected: the party is missing for 32–46% of the speeches (by decade) and sometimes holds a
  function instead (`voorzitter van de commissie`); typos in speaker labels (`De her Zijlstra`,
  `Minster …`).
- One document (`h-tk-19951996-36-2868-2950`) is published as PDF only.

**Context**

- Officielebekendmakingen.nl states that, under article 11 of the Dutch Copyright Act
  (Auteurswet), its publications may be freely reused unless stated otherwise.
- The [GPT-NL Public Corpus](https://huggingface.co/datasets/GPT-NL/GPT-NL_Public_Corpus)
  already contains the historical Handelingen (until about 1994). In a sample, 1995–2007 was
  absent and 2008 onwards only partly present.
- The source keeps growing: session year 2026-2027 had 49 documents in November 2026.

## Historical proceedings (1814–1995)

`download_handelingen.py -s historical --formats ocr` crawls the Staten-Generaal Digitaal OCR
archive (~377k documents). This part is not complete and not published; most of it is already
included in the `officiele-bekendmakingen` subset of the
[GPT-NL Public Corpus](https://huggingface.co/datasets/GPT-NL/GPT-NL_Public_Corpus).

## License

The code is released under the [MIT License](LICENSE). The published dataset is CC0 1.0.
