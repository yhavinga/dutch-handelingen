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

## Historical proceedings (1814–1995)

`download_handelingen.py -s historical --formats ocr` crawls the Staten-Generaal Digitaal OCR
archive (~377k documents). This part is not complete and not published; most of it is already
included in the `officiele-bekendmakingen` subset of the
[GPT-NL Public Corpus](https://huggingface.co/datasets/GPT-NL/GPT-NL_Public_Corpus).
