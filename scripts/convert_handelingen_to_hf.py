#!/usr/bin/env python3
"""
Convert downloaded Handelingen to HuggingFace datasets format.

Creates two datasets:
1. Historical (1814-1995): Simple text format from OCR
2. Modern (1995-present): Structured debates with speaker turns

Usage:
    python convert_handelingen_to_hf.py --input-dir ./handelingen --output-dir ./hf_datasets
"""

import argparse
import json
import re
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import logging
from xml.etree import ElementTree as ET

from datasets import Dataset, DatasetDict
from tqdm import tqdm

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@dataclass
class HistoricalRecord:
    id: str
    year: str
    text: str


@dataclass
class SpeakerTurn:
    speaker: Optional[str]  # None for narrative text (headings, announcements, procedural notes)
    party: Optional[str]
    text: str


@dataclass
class ModernDebate:
    id: str
    date: str
    session_year: str
    meeting_number: Optional[int]
    chamber: str  # tweede_kamer, eerste_kamer, verenigd
    title: str
    turns: list[SpeakerTurn] = field(default_factory=list)


def parse_historical_text(file_path: Path) -> Optional[str]:
    """Read historical OCR text file."""
    try:
        return file_path.read_text(encoding='utf-8')
    except Exception as e:
        logger.debug(f"Error reading {file_path}: {e}")
        return None


CHAMBERS = {'h-tk': 'tweede_kamer', 'h-ek': 'eerste_kamer', 'h-vv': 'verenigd'}
CHAIR = "De voorzitter"


# Elements that start a new line in the flat text; everything else is treated as inline markup
LINE_TAGS = {'al', 'li', 'kop', 'titel', 'tussenkop'}


def normalize_line(pieces: list[str]) -> str:
    # Inline tags (<nadruk>, <stuknr>, ...) often sit flush against words without a separating
    # space in the source, so pieces are joined with spaces and the spaces this introduces
    # before punctuation are removed again.
    # Soft hyphens are invisible typesetting hints that split otherwise identical words/names
    text = ' '.join(' '.join(pieces).replace('\xad', '').split())
    text = re.sub(r'\s+([.,;:!?)\]])', r'\1', text)
    return re.sub(r'([(\[])\s+', r'\1', text)


def block_text(element: ET.Element, skip_tags: tuple = ()) -> str:
    """All text of an element, one line per paragraph-like child, skipping skip_tags subtrees.

    Walks mixed content so text that sits directly in a container next to <al> paragraphs
    (e.g. <toestnd>) is kept.
    """
    lines, buf = [], []

    def flush():
        line = normalize_line(buf)
        buf.clear()
        if line:
            lines.append(line)

    def walk(el):
        is_line = el.tag in LINE_TAGS
        if is_line:
            flush()
        if el.text:
            # Sub/superscripts set flush against the previous word belong to it: CO<inf>2</inf>,
            # m<sup>3</sup>, 3<sup>e</sup>
            if el.tag in ('inf', 'sup') and buf and buf[-1] and not buf[-1][-1].isspace():
                buf[-1] += el.text
            else:
                buf.append(el.text)
        for child in el:
            if child.tag not in skip_tags:
                walk(child)
            if child.tail:
                # A tail that starts right at the closing tag with '-' or '/' continues the word
                # ('<vetnr>24400</vetnr>-VII', 'woon</nadruk>- en'), so no space may be inserted
                if buf and child.tail[0] in '-/':
                    buf[-1] += child.tail
                else:
                    buf.append(child.tail)
        if is_line:
            flush()

    walk(element)
    flush()
    return '\n'.join(lines)


def child_text(element: Optional[ET.Element], tag: str) -> Optional[str]:
    if element is None:
        return None
    found = element.find(tag)
    if found is None:
        return None
    return block_text(found).replace('\n', ' ') or None


SURNAME_PARTICLES = {'van', 'de', 'der', 'den', 'het', 'ter', 'ten', 'te', 'in', 'op', "'t",
                     'le', 'la', 'du', 'von', 'zu'}


def display_surname(surname: str) -> str:
    """'Campen van' -> 'Van Campen'.

    The op-xsd-2012 XML stores surnames in sort order with the particles at the end, while the
    printed Handelingen write 'De heer Van Campen'.
    """
    words = surname.split()
    i = len(words)
    while i > 1 and words[i - 1].lower() in SURNAME_PARTICLES:
        i -= 1
    if i == len(words):
        return surname
    particles = ' '.join(words[i:])
    return f"{particles[0].upper()}{particles[1:]} {' '.join(words[:i])}"


def drop_printed_label(text: str, prefix: Optional[str], surname: Optional[str]) -> str:
    """Remove a leading 'De heer Van Campen (VVD):' line that repeats the speaker label.

    Newer XML (mostly 2020s) repeats the printed label as the first paragraph of the turn,
    which would show the speaker twice in the flat text.
    """
    if not prefix or not surname:
        return text
    first, _, rest = text.partition('\n')
    key = surname.split()[0]
    if len(first) <= 150 and first.endswith(':') and first.startswith(prefix) and key in first:
        return rest
    return text


def printed_label(al: ET.Element) -> Optional[tuple[str, Optional[str]]]:
    """(speaker, party) if this paragraph is a printed speaker label such as
    'De heer <nadruk>De Vries</nadruk> (CDA):', else None.

    Some op-xsd-2012 documents put a whole dialogue in one <tekst> without <spreekbeurt>
    elements. Labels are then recognized by the bold name and the trailing colon; requiring
    the bold markup avoids treating ordinary sentences ending in ':' as labels.
    """
    if al.find('nadruk') is None:
        return None
    line = block_text(al).replace('\n', ' ')
    if not line.endswith(':') or len(line) > 120:
        return None
    label = line[:-1].strip()
    # Bold procedural sentences ('Aan de orde is de stemming over ...:') are not labels
    if label.startswith(('Aan de orde', 'het ', 'Het ')):
        return None
    party = None
    match = re.search(r'\s*\(([^()]+)\)$', label)
    if match:
        party, label = match.group(1), label[:match.start()]
    # 'De heer De Vries, voorzitter der commissie' -> the role is not part of the name
    speaker = label.split(',', 1)[0].strip()
    if speaker.lower() == CHAIR.lower():
        speaker = CHAIR
    return (speaker, party) if speaker else None


def split_on_printed_labels(element: ET.Element) -> Optional[list[SpeakerTurn]]:
    """Split a narrative block at printed speaker labels; None if it contains no labels."""
    paragraphs = list(element.iter('al'))
    if not any(printed_label(al) for al in paragraphs):
        return None
    turns, speaker, party, lines = [], None, None, []

    def flush():
        if lines:
            turns.append(SpeakerTurn(speaker=speaker, party=party, text='\n'.join(lines)))

    for al in paragraphs:
        label = printed_label(al)
        if label:
            flush()
            (speaker, party), lines = label, []
        else:
            line = block_text(al)
            if line:
                lines.append(line)
    flush()
    return turns


# Structural wrappers in the op-xsd-2012 schema: descend into them instead of flattening them
CONTAINERS_2012 = {'handelingen', 'vergadering', 'opening', 'sluiting', 'agendapunt',
                   'handeling_bijlage', 'divisie'}
# Numbering and sitting header fields; date and meeting number come from the SRU record
SKIP_2012 = {'metadata', 'nr', 'vergadering-nummer', 'vergaderdatum', 'vergadertijd'}


def parse_turns_2012_schema(root: ET.Element) -> list[SpeakerTurn]:
    """<officiele-publicatie> documents (op-xsd-2012): agendapunt/opening/bijlage > spreekbeurt."""
    turns = []

    def walk(el):
        for child in el:
            if child.tag in SKIP_2012:
                continue
            if child.tag in CONTAINERS_2012:
                walk(child)
            elif child.tag == 'spreekbeurt':
                spreker = child.find('spreker')
                prefix = child_text(spreker, 'voorvoegsels')
                if prefix is None and spreker is not None and spreker.text and spreker.text.strip():
                    # Some documents write the title as plain text: <spreker>Staatssecretaris <naam>
                    prefix = ' '.join(spreker.text.split())
                surname = child_text(spreker.find('naam') if spreker is not None else None, 'achternaam')
                surname = display_surname(surname) if surname else None
                speaker = ' '.join(p for p in (prefix, surname) if p) or None
                text = drop_printed_label(block_text(child, skip_tags=('spreker',)), prefix, surname)
                if text:
                    turns.append(SpeakerTurn(speaker=speaker, party=child_text(spreker, 'politiek'),
                                             text=text))
            else:
                # item-titel, onderwerp, tekst, kop, al-groep: headings and procedural narrative,
                # or a whole dialogue with printed labels instead of <spreekbeurt> elements
                labelled = split_on_printed_labels(child)
                if labelled is not None:
                    turns.extend(labelled)
                    continue
                text = block_text(child)
                if text:
                    turns.append(SpeakerTurn(speaker=None, party=None, text=text))

    walk(root)
    return turns


def parse_turns_handeling_schema(root: ET.Element) -> list[SpeakerTurn]:
    """Older <handeling> documents: part > item > spreker/voorz/draad."""
    turns = []

    def add(speaker, party, text):
        if text:
            turns.append(SpeakerTurn(speaker=speaker, party=party, text=text))

    for part in root.iter('part'):
        for child in part:
            if child.tag == 'metadata':
                continue
            if child.tag != 'item':
                # intro (opening/presence), stukken (incoming papers), besllst, noten
                add(None, None, block_text(child))
                continue
            # A change of chair is printed as one line 'Voorzitter: Tummers' but stored as two
            # sibling elements <vrzlabel> and <vrznaam>; keep them together as one block.
            chair_label = None
            for el in child:
                if el.tag == 'vrzlabel':
                    if chair_label:
                        add(None, None, chair_label)
                    chair_label = block_text(el)
                    continue
                if el.tag == 'vrznaam':
                    add(None, None, ' '.join(p for p in (chair_label, block_text(el)) if p))
                    chair_label = None
                    continue
                if chair_label:
                    add(None, None, chair_label)
                    chair_label = None
                if el.tag == 'spreker':
                    wie = el.find('wie')
                    speaker = ' '.join(
                        p for p in (child_text(wie, 'aanspr'), child_text(wie, 'naam')) if p
                    ) or None
                    add(speaker, child_text(wie, 'partij'), block_text(el, skip_tags=('wie',)))
                elif el.tag == 'voorz':
                    add(CHAIR, None, block_text(el))
                else:
                    # itemnaam, itemkop, draad, motie, ondtit
                    add(None, None, block_text(el))
            if chair_label:
                add(None, None, chair_label)
    return turns


def parse_modern_xml(file_path: Path, meta: dict) -> ModernDebate:
    """Parse one modern Handelingen document (one agenda item); meta is its SRU index entry."""
    doc_id = file_path.stem
    chamber = CHAMBERS.get(doc_id[:4])
    if chamber is None:
        raise ValueError(f"Unknown chamber prefix in {file_path}")

    root = ET.parse(file_path).getroot()
    if root.tag == 'officiele-publicatie':
        turns = parse_turns_2012_schema(root)
    elif root.tag == 'handeling':
        turns = parse_turns_handeling_schema(root)
    else:
        raise ValueError(f"Unknown root element <{root.tag}> in {file_path}")

    if not meta.get('date'):
        raise ValueError(f"No datumVergadering in SRU record for {doc_id}")

    return ModernDebate(
        id=doc_id,
        date=meta['date'],
        session_year=meta.get('session_year') or '',
        meeting_number=meta.get('meeting_number'),
        chamber=chamber,
        title=meta.get('title') or '',
        turns=turns
    )


def render_text(debate: ModernDebate) -> str:
    """Flat text in the printed Handelingen style: 'Spreker (Partij):' followed by the speech."""
    blocks = []
    for turn in debate.turns:
        if turn.speaker is None:
            blocks.append(turn.text)
        else:
            label = f"{turn.speaker} ({turn.party})" if turn.party else turn.speaker
            blocks.append(f"{label}:\n{turn.text}")
    return '\n\n'.join(blocks)


def convert_historical(input_dir: Path, output_dir: Path):
    """Convert historical OCR text files to HuggingFace dataset."""
    historical_dir = input_dir / "historical"
    if not historical_dir.exists():
        logger.warning(f"Historical directory not found: {historical_dir}")
        return

    records = []
    year_dirs = sorted(historical_dir.iterdir())

    for year_dir in tqdm(year_dirs, desc="Processing historical years"):
        if not year_dir.is_dir():
            continue

        year = year_dir.name
        txt_files = list(year_dir.glob("*.txt"))

        for txt_file in txt_files:
            text = parse_historical_text(txt_file)
            if text and text.strip():
                records.append({
                    "id": f"sgd-{year}-{txt_file.stem}",
                    "year": year,
                    "text": text
                })

    if not records:
        logger.warning("No historical records found")
        return

    logger.info(f"Creating historical dataset with {len(records)} records")

    dataset = Dataset.from_list(records)

    output_path = output_dir / "handelingen-historical"
    output_path.mkdir(parents=True, exist_ok=True)

    dataset.save_to_disk(str(output_path))
    logger.info(f"Saved historical dataset to {output_path}")

    # Also save as Parquet for easy upload
    parquet_path = output_path / "data.parquet"
    dataset.to_parquet(str(parquet_path))
    logger.info(f"Saved Parquet to {parquet_path}")


def convert_modern(input_dir: Path, output_dir: Path):
    """Convert modern XML files to HuggingFace dataset with structured debates."""
    modern_dir = input_dir / "modern"
    index_file = input_dir / "index_modern_sru.json"
    if not index_file.exists():
        raise FileNotFoundError(f"SRU index not found: {index_file} (run download_handelingen.py -s modern)")
    index = json.loads(index_file.read_text())

    # The index is the source of truth: a listed XML that is not on disk means the download
    # is incomplete, which must not silently shrink the dataset.
    without_xml = [d['doc_id'] for d in index if 'xml' not in d['formats']]
    entries = [d for d in index if 'xml' in d['formats']]
    missing = [d['doc_id'] for d in entries
               if not (modern_dir / d['doc_type'] / d['year'] / f"{d['doc_id']}.xml").exists()]
    if missing:
        raise RuntimeError(f"{len(missing)} indexed XML files not downloaded, e.g. {missing[:5]}")
    if without_xml:
        logger.warning(f"{len(without_xml)} documents are only published without XML (PDF only): "
                       f"{without_xml[:10]}")

    records = []
    empty_ids = []

    for entry in tqdm(sorted(entries, key=lambda d: d['doc_id']), desc="Processing modern"):
        xml_file = modern_dir / entry['doc_type'] / entry['year'] / f"{entry['doc_id']}.xml"
        debate = parse_modern_xml(xml_file, entry['meta'])
        if not debate.turns:
            empty_ids.append(debate.id)
            continue
        records.append({
            "id": debate.id,
            "chamber": debate.chamber,
            "session_year": debate.session_year,
            "meeting_number": debate.meeting_number,
            "date": debate.date,
            "title": debate.title,
            "turns": [{"speaker": t.speaker, "party": t.party, "text": t.text}
                      for t in debate.turns],
            "text": render_text(debate)
        })

    if empty_ids:
        logger.warning(f"Skipped {len(empty_ids)} documents without any text: {empty_ids}")

    if not records:
        raise RuntimeError(f"No modern records found in {modern_dir}")

    logger.info(f"Creating modern debates dataset with {len(records)} records")

    dataset = Dataset.from_list(records)

    output_path = output_dir / "handelingen-debates"
    output_path.mkdir(parents=True, exist_ok=True)

    dataset.save_to_disk(str(output_path))
    logger.info(f"Saved modern debates dataset to {output_path}")

    parquet_path = output_path / "data.parquet"
    dataset.to_parquet(str(parquet_path))
    logger.info(f"Saved Parquet to {parquet_path}")


def main():
    parser = argparse.ArgumentParser(description="Convert Handelingen to HuggingFace format")
    parser.add_argument(
        "--input-dir", "-i",
        type=Path,
        default=Path("./data/handelingen"),
        help="Input directory with downloaded Handelingen"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("./data/hf_datasets"),
        help="Output directory for HuggingFace datasets"
    )
    parser.add_argument(
        "--historical-only",
        action="store_true",
        help="Only convert historical data"
    )
    parser.add_argument(
        "--modern-only",
        action="store_true",
        help="Only convert modern data"
    )

    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)

    if not args.modern_only:
        convert_historical(args.input_dir, args.output_dir)

    if not args.historical_only:
        convert_modern(args.input_dir, args.output_dir)

    logger.info("Conversion complete!")


if __name__ == "__main__":
    main()
