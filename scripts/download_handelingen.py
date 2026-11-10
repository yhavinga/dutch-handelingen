#!/usr/bin/env python3
"""
Download all Dutch parliamentary proceedings (Handelingen) from repository.overheid.nl

Two sources:
1. SGD (Staten-Generaal Digitaal) - Historical 1814-1995
   URL: https://repository.overheid.nl/frbr/sgd/

2. Official Publications - Modern 1995-present
   Document list from the SRU 2.0 API: https://repository.overheid.nl/sru
   Files from: https://repository.overheid.nl/frbr/officielepublicaties/

Usage:
    python download_handelingen.py --output-dir ./handelingen --source historical
    python download_handelingen.py --output-dir ./handelingen --source modern
    python download_handelingen.py --output-dir ./handelingen --source all
"""

import argparse
import asyncio
import aiohttp
import aiofiles
import json
import re
import ssl
import time
from pathlib import Path
from urllib.parse import urljoin, quote
from dataclasses import dataclass
from datetime import date
from typing import Optional
from xml.etree import ElementTree as ET
import logging
import certifi
from tqdm import tqdm

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Repository base URLs
SGD_BASE = "https://repository.overheid.nl/frbr/sgd/"
OFFICIAL_BASE = "https://repository.overheid.nl/frbr/officielepublicaties/"
SRU_BASE = "https://repository.overheid.nl/sru"
SRU_PAGE_SIZE = 1000  # SRU maximum; deep paging beyond 10,000 results times out, so query per session year
MODERN_FIRST_SESSION_YEAR = 1994

# Modern Handelingen paths
MODERN_PATHS = ["h-ek", "h-tk", "h-vv"]


@dataclass
class Document:
    """Represents a document to download."""
    doc_id: str
    year: str
    source: str  # 'sgd' or 'official'
    doc_type: str  # 'h-ek', 'h-tk', 'h-vv' for modern, empty for sgd
    formats: list[str]  # available formats
    urls: Optional[dict] = None  # format -> direct file URL (SRU index only)
    meta: Optional[dict] = None  # date, title, session_year, meeting_number (SRU index only)


class RepositoryCrawler:
    """Crawls repository.overheid.nl to find all Handelingen documents."""

    def __init__(self, session: aiohttp.ClientSession):
        self.session = session

    async def fetch_page(self, url: str) -> str:
        """Fetch a page with retry logic."""
        for attempt in range(3):
            try:
                async with self.session.get(url, timeout=30) as resp:
                    if resp.status == 200:
                        return await resp.text()
                    logger.warning(f"HTTP {resp.status} for {url}")
            except Exception as e:
                logger.warning(f"Attempt {attempt+1} failed for {url}: {e}")
                await asyncio.sleep(2 ** attempt)
        return ""

    async def get_subfolders(self, url: str) -> list[str]:
        """Extract subfolder links from a repository page."""
        html = await self.fetch_page(url)
        if not html:
            return []

        # Extract folder links - they appear as links in the Subarea section
        # Pattern: href="/frbr/.../{folder-name}"
        pattern = r'href="([^"]+)"[^>]*>\s*<span[^>]*>([^<]+)</span>'
        matches = re.findall(pattern, html)

        # Also try simpler pattern for direct links
        pattern2 = r'<a[^>]*href="(/frbr/[^"]+)"[^>]*>(?:<[^>]*>)*([^<]+)'
        matches2 = re.findall(pattern2, html)

        folders = []
        for href, name in matches + matches2:
            # Skip navigation links, only get content folders
            if '/frbr/' in href and name.strip() and not name.startswith('«'):
                folders.append(href)

        return list(set(folders))

    async def get_pagination_urls(self, base_url: str) -> list[str]:
        """Get all paginated URLs for a listing."""
        html = await self.fetch_page(base_url)
        if not html:
            return [base_url]

        # Find total count from "1 - 11 van de X resultaten"
        match = re.search(r'van de (\d+) resultaten', html)
        if not match:
            return [base_url]

        total = int(match.group(1))
        urls = [base_url]

        # Repository shows 11 items per page
        for start in range(11, total, 11):
            urls.append(f"{base_url}?start={start}")

        return urls

    async def crawl_sgd_years(self) -> list[str]:
        """Get all year folders from SGD repository."""
        logger.info("Crawling SGD year folders...")
        all_years = []

        page_urls = await self.get_pagination_urls(SGD_BASE)
        for page_url in page_urls:
            html = await self.fetch_page(page_url)
            # Extract year folder names
            years = re.findall(r'href="/frbr/sgd/([^/"]+)"', html)
            all_years.extend(years)

        return list(set(all_years))

    async def crawl_sgd_year_documents(self, year: str) -> list[Document]:
        """Get all document IDs for a specific SGD year."""
        base_url = f"{SGD_BASE}{year}/"
        documents = []

        page_urls = await self.get_pagination_urls(base_url)
        for page_url in page_urls:
            html = await self.fetch_page(page_url)
            # Extract document IDs (numeric)
            doc_ids = re.findall(rf'href="/frbr/sgd/{year}/(\d+)"', html)
            for doc_id in doc_ids:
                documents.append(Document(
                    doc_id=doc_id,
                    year=year,
                    source='sgd',
                    doc_type='',
                    formats=['pdf', 'ocr']  # Historical docs have pdf and ocr
                ))

        return documents

    async def crawl_modern_sru_session_year(self, session_year: str) -> list[Document]:
        """Get all modern Handelingen documents of one vergaderjaar (e.g. '2011-2012') via SRU.

        The HTML directory listings dropped ~8% of documents per year (and most of some years),
        so the official SRU API is used as the document list instead.
        """
        query = (f'c.product-area==officielepublicaties AND w.publicatienaam==Handelingen '
                 f'AND w.vergaderjaar=="{session_year}"')
        documents = []
        expected = None
        start = 1
        while expected is None or start <= expected:
            url = (f"{SRU_BASE}?httpAccept=application/xml&maximumRecords={SRU_PAGE_SIZE}"
                   f"&startRecord={start}&query={quote(query)}")
            xml_text = await self.fetch_page(url)
            if not xml_text:
                raise RuntimeError(f"SRU request failed for {session_year}: {url}")
            root = ET.fromstring(xml_text)
            if expected is None:
                count = next((e.text for e in root.iter() if e.tag.endswith('}numberOfRecords')), None)
                if count is None:
                    raise RuntimeError(f"No numberOfRecords in SRU response for {session_year}: {url}")
                expected = int(count)
            for record in (e for e in root.iter() if e.tag.endswith('}recordData')):
                documents.append(self._sru_record_to_document(record))
            start += SRU_PAGE_SIZE

        if len(documents) != expected:
            raise RuntimeError(
                f"SRU returned {len(documents)} records for {session_year}, expected {expected}"
            )
        # Appendices (blg-*) and a few legacy ids are also published as Handelingen; only the
        # chamber proceedings themselves are in scope.
        return [d for d in documents if d.doc_type in MODERN_PATHS]

    @staticmethod
    def _sru_record_to_document(record: ET.Element) -> Document:
        fields, urls = {}, {}
        for el in record.iter():
            name = el.tag.rsplit('}', 1)[-1]
            if name == 'itemUrl':
                urls[el.get('manifestation')] = el.text
            elif el.text and el.text.strip():
                fields.setdefault(name, el.text.strip())

        doc_id = fields['identifier']
        # content-area looks like 'officielepublicaties/h-tk/20112012'
        area_parts = fields.get('content-area', '').split('/')
        doc_type, year = (area_parts[1], area_parts[2]) if len(area_parts) == 3 else ('', '')
        meeting = fields.get('publicatienummer')
        return Document(
            doc_id=doc_id,
            year=year,
            source='official',
            doc_type=doc_type,
            formats=sorted(urls),
            urls=urls,
            meta={
                'date': fields.get('datumVergadering'),
                'title': fields.get('title'),
                'session_year': fields.get('vergaderjaar'),
                'meeting_number': int(meeting) if meeting and meeting.isdigit() else None,
            }
        )


class RateLimitError(Exception):
    """Raised when rate limit is detected, to stop fast."""
    pass


class DocumentDownloader:
    """Downloads documents from the repository."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        output_dir: Path,
        formats: list[str],
        max_concurrent: int = 10,
        fail_fast: bool = False
    ):
        self.session = session
        self.output_dir = output_dir
        self.formats = formats
        self.semaphore = asyncio.Semaphore(max_concurrent)
        self.downloaded = 0
        self.failed = 0
        self.skipped = 0
        self.fail_fast = fail_fast
        self.stop_flag = False

    def get_download_url(self, doc: Document, fmt: str) -> str:
        """Construct download URL for a document format."""
        if doc.source == 'sgd':
            return f"{SGD_BASE}{doc.year}/{doc.doc_id}/1/{fmt}/"
        else:
            return f"{OFFICIAL_BASE}{doc.doc_type}/{doc.year}/{doc.doc_id}/1/{fmt}/"

    def get_output_path(self, doc: Document, fmt: str) -> Path:
        """Construct output file path."""
        if doc.source == 'sgd':
            subdir = self.output_dir / "historical" / doc.year
        else:
            subdir = self.output_dir / "modern" / doc.doc_type / doc.year

        subdir.mkdir(parents=True, exist_ok=True)

        ext = fmt if fmt != 'ocr' else 'txt'
        return subdir / f"{doc.doc_id}.{ext}"

    async def get_actual_file_url(self, index_url: str) -> Optional[str]:
        """Get the actual file URL from the format index page."""
        # Index pages are rate limited like the files themselves; treating a 429 as
        # "no file" silently dropped ~20% of documents per run.
        max_retries = 5
        for attempt in range(max_retries):
            try:
                async with self.session.get(index_url, timeout=30) as resp:
                    if resp.status == 429 or resp.status >= 500:
                        retry_after = resp.headers.get('Retry-After', '')
                        wait_time = int(retry_after) if retry_after.isdigit() else (2 ** attempt) * 5
                        logger.warning(
                            f"HTTP {resp.status} on index page, waiting {wait_time}s "
                            f"(attempt {attempt+1}/{max_retries}): {index_url}"
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    if resp.status != 200:
                        logger.warning(f"HTTP {resp.status} on index page {index_url}")
                        return None
                    html = await resp.text()

                    # Find the actual file link
                    # Pattern varies: .pdf, .xml, .txt for ocr
                    patterns = [
                        r'href="([^"]+\.pdf)"',
                        r'href="([^"]+\.xml)"',
                        r'href="([^"]+\.txt)"',
                        r'href="(/frbr/[^"]+)"'
                    ]

                    for pattern in patterns:
                        match = re.search(pattern, html)
                        if match:
                            file_path = match.group(1)
                            if file_path.startswith('/'):
                                return f"https://repository.overheid.nl{file_path}"
                            return file_path

                    return None
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                wait_time = (2 ** attempt) * 2
                logger.warning(
                    f"Connection error on index page ({type(e).__name__}), waiting {wait_time}s "
                    f"(attempt {attempt+1}/{max_retries}): {index_url}"
                )
                await asyncio.sleep(wait_time)

        logger.warning(f"Giving up on index page after {max_retries} attempts: {index_url}")
        return None

    async def get_all_ocr_urls(self, index_url: str) -> list[str]:
        """Get all OCR XML file URLs from the index page (historical docs have multiple pages)."""
        try:
            async with self.session.get(index_url, timeout=30) as resp:
                if resp.status != 200:
                    return []
                html = await resp.text()

                # Find all XML file links (SGD_*_tekst_*.xml pattern)
                matches = re.findall(r'href="(/frbr/[^"]+\.xml)"', html)
                urls = [f"https://repository.overheid.nl{m}" for m in matches]

                # Sort by filename to maintain page order
                urls.sort(key=lambda u: u.split('/')[-1])
                return urls
        except Exception as e:
            logger.debug(f"Error getting OCR URLs from {index_url}: {e}")
            return []

    def extract_text_from_ocr_xml(self, xml_content: bytes) -> str:
        """Extract plain text from OCR XML (SGDTEKST format)."""
        try:
            text = xml_content.decode('utf-8', errors='replace')
            # Extract content from <p> tags
            paragraphs = re.findall(r'<p>([^<]*)</p>', text)
            return '\n'.join(p.strip() for p in paragraphs if p.strip())
        except Exception as e:
            logger.debug(f"Error parsing OCR XML: {e}")
            return ""

    async def _download_ocr_pages(self, doc: Document, index_url: str, output_path: Path) -> bool:
        """Download all OCR XML pages and concatenate into single text file."""
        ocr_urls = await self.get_all_ocr_urls(index_url)
        if not ocr_urls:
            logger.debug(f"No OCR URLs found for {doc.doc_id}")
            return False

        all_text = []
        for url in ocr_urls:
            for attempt in range(3):
                try:
                    async with self.session.get(url, timeout=60) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            page_text = self.extract_text_from_ocr_xml(content)
                            if page_text:
                                all_text.append(page_text)
                            break
                        elif resp.status == 429:
                            wait_time = (2 ** attempt) * 5
                            logger.warning(f"Rate limited on OCR page, waiting {wait_time}s")
                            await asyncio.sleep(wait_time)
                        else:
                            logger.debug(f"HTTP {resp.status} for OCR page {url}")
                            break
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
                    else:
                        logger.debug(f"Failed to download OCR page {url}: {e}")

        if all_text:
            combined = '\n\n'.join(all_text)
            async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
                await f.write(combined)
            logger.debug(f"Downloaded OCR text for {doc.doc_id} ({len(ocr_urls)} pages)")
            return True

        return False

    async def download_document(self, doc: Document) -> bool:
        """Download a single document in all requested formats."""
        # Check if we should stop
        if self.stop_flag:
            return False

        # Check what needs downloading BEFORE acquiring semaphore
        to_download = []
        for fmt in self.formats:
            if fmt not in doc.formats:
                continue
            output_path = self.get_output_path(doc, fmt)
            if output_path.exists() and output_path.stat().st_size > 0:
                continue  # Already exists
            to_download.append((fmt, output_path))

        # Fast path: everything already exists
        if not to_download:
            self.skipped += 1
            return True

        # Need to download - now acquire semaphore
        async with self.semaphore:
            if self.stop_flag:
                return False

            downloaded_any = False

            for fmt, output_path in to_download:
                index_url = self.get_download_url(doc, fmt)

                # Special handling for OCR format (historical docs have multiple XML pages)
                if fmt == 'ocr':
                    try:
                        success = await self._download_ocr_pages(doc, index_url, output_path)
                        if success:
                            downloaded_any = True
                        continue
                    except RateLimitError:
                        raise
                    except Exception as e:
                        if self.fail_fast:
                            logger.error(f"FAIL FAST - OCR download failed for {doc.doc_id}: {e}")
                            self.stop_flag = True
                            raise RateLimitError(f"OCR failed for {doc.doc_id}: {e}")
                        logger.debug(f"OCR download failed for {doc.doc_id}: {e}")
                        continue

                try:
                    # SRU records carry the direct file URL; only crawled docs need the index page
                    file_url = (doc.urls or {}).get(fmt) or await self.get_actual_file_url(index_url)
                except Exception as e:
                    if self.fail_fast:
                        logger.error(f"FAIL FAST - Error getting file URL for {doc.doc_id}")
                        logger.error(f"  Index URL: {index_url}")
                        logger.error(f"  Exception: {type(e).__name__}: {e}")
                        self.stop_flag = True
                        raise RateLimitError(f"Failed on {doc.doc_id}: {e}")
                    continue

                if not file_url:
                    if self.fail_fast:
                        # Fetch again to log what we actually got
                        try:
                            async with self.session.get(index_url, timeout=30) as resp:
                                html = await resp.text()
                                logger.error(f"FAIL FAST - No file URL found for {doc.doc_id}")
                                logger.error(f"  Index URL: {index_url}")
                                logger.error(f"  HTTP Status: {resp.status}")
                                logger.error(f"  HTML received ({len(html)} chars):")
                                logger.error(f"  {html[:1500]}")
                        except Exception as e:
                            logger.error(f"FAIL FAST - No file URL and re-fetch failed: {e}")
                        self.stop_flag = True
                        raise RateLimitError(f"No file URL for {doc.doc_id}")
                    logger.warning(f"No file URL found for {doc.doc_id} format {fmt}")
                    continue

                try:
                    # Retry loop with exponential backoff for rate limits and transient errors
                    max_retries = 5
                    last_error = None
                    for attempt in range(max_retries):
                        try:
                            async with self.session.get(file_url, timeout=120) as resp:
                                if resp.status == 200:
                                    content = await resp.read()
                                    async with aiofiles.open(output_path, 'wb') as f:
                                        await f.write(content)
                                    downloaded_any = True
                                    logger.debug(f"Downloaded {output_path.name}")
                                    break
                                elif resp.status == 429:
                                    # Rate limited - back off and retry
                                    retry_after = resp.headers.get('Retry-After', None)
                                    if retry_after:
                                        wait_time = int(retry_after)
                                    else:
                                        wait_time = (2 ** attempt) * 5  # 5, 10, 20, 40, 80 sec

                                    if attempt < max_retries - 1:
                                        logger.warning(
                                            f"Rate limited (429), waiting {wait_time}s "
                                            f"(attempt {attempt+1}/{max_retries})"
                                        )
                                        await asyncio.sleep(wait_time)
                                    else:
                                        last_error = f"429 after {max_retries} retries"
                                elif resp.status >= 500:
                                    # Server error - retry
                                    wait_time = (2 ** attempt) * 2
                                    if attempt < max_retries - 1:
                                        logger.warning(
                                            f"Server error ({resp.status}), waiting {wait_time}s"
                                        )
                                        await asyncio.sleep(wait_time)
                                    else:
                                        last_error = f"HTTP {resp.status}"
                                else:
                                    # Client error (4xx except 429) - don't retry
                                    last_error = f"HTTP {resp.status}"
                                    break
                        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                            # Network/connection error - retry with backoff
                            wait_time = (2 ** attempt) * 2
                            if attempt < max_retries - 1:
                                logger.warning(
                                    f"Connection error ({type(e).__name__}), "
                                    f"waiting {wait_time}s (attempt {attempt+1}/{max_retries})"
                                )
                                await asyncio.sleep(wait_time)
                            else:
                                last_error = f"{type(e).__name__}: {e}"

                    if last_error and not downloaded_any:
                        if self.fail_fast:
                            logger.error(f"FAIL FAST - {last_error} for {doc.doc_id}")
                            logger.error(f"  URL: {file_url}")
                            self.stop_flag = True
                            raise RateLimitError(f"{last_error} for {doc.doc_id}")
                        logger.warning(f"Failed {doc.doc_id}: {last_error}")
                except RateLimitError:
                    raise
                except Exception as e:
                    # Unexpected error - not retried above
                    if self.fail_fast:
                        logger.error(f"FAIL FAST - Unexpected error for {doc.doc_id}")
                        logger.error(f"  URL: {file_url}")
                        logger.error(f"  Exception: {type(e).__name__}: {e}")
                        self.stop_flag = True
                        raise RateLimitError(f"Unexpected error for {doc.doc_id}: {e}")
                    logger.debug(f"Error downloading {doc.doc_id}: {e}")

            if downloaded_any:
                self.downloaded += 1
            else:
                self.failed += 1

            return downloaded_any


def save_index(documents: list[Document], index_file: Path):
    """Save document index to file."""
    with open(index_file, 'w') as f:
        json.dump([vars(d) for d in documents], f, indent=2)


def load_progress(progress_file: Path) -> dict:
    """Load crawl progress from file."""
    if progress_file.exists():
        with open(progress_file) as f:
            return json.load(f)
    return {"completed_years": [], "documents": []}


def save_progress(progress: dict, progress_file: Path):
    """Save crawl progress to file."""
    with open(progress_file, 'w') as f:
        json.dump(progress, f, indent=2)


async def crawl_and_save_index(
    session: aiohttp.ClientSession,
    output_dir: Path,
    source: str
) -> list[Document]:
    """Crawl repository and save document index with resume support."""
    crawler = RepositoryCrawler(session)

    output_dir.mkdir(parents=True, exist_ok=True)
    # Separate name so the older HTML-crawled modern index is never loaded as if complete
    suffix = "_sru" if source in ['modern', 'all'] else ""
    index_file = output_dir / f"index_{source}{suffix}.json"
    progress_file = output_dir / f"crawl_progress_{source}{suffix}.json"

    # Load completed index if available
    if index_file.exists():
        logger.info(f"Loading existing index from {index_file}")
        with open(index_file) as f:
            data = json.load(f)
            return [Document(**d) for d in data]

    # Load partial progress if available
    progress = load_progress(progress_file)
    completed_years = set(progress.get("completed_years", []))
    all_documents = [Document(**d) for d in progress.get("documents", [])]

    if all_documents:
        logger.info(f"Resuming crawl: {len(completed_years)} years done, {len(all_documents)} docs found")

    if source in ['historical', 'all']:
        logger.info("Crawling SGD (historical) repository...")
        years = await crawler.crawl_sgd_years()
        years_to_crawl = [y for y in sorted(years) if f"sgd/{y}" not in completed_years]
        logger.info(f"Found {len(years)} SGD years, {len(years_to_crawl)} remaining")

        for i, year in enumerate(years_to_crawl):
            logger.info(f"Crawling SGD year {year} ({i+1}/{len(years_to_crawl)})")
            docs = await crawler.crawl_sgd_year_documents(year)
            all_documents.extend(docs)
            completed_years.add(f"sgd/{year}")
            logger.info(f"  Found {len(docs)} documents (total: {len(all_documents)})")

            # Save progress every 5 years
            if (i + 1) % 5 == 0:
                progress = {
                    "completed_years": list(completed_years),
                    "documents": [vars(d) for d in all_documents]
                }
                save_progress(progress, progress_file)
                logger.info(f"  Progress saved ({len(all_documents)} docs)")

    if source in ['modern', 'all']:
        logger.info("Querying SRU API for modern Handelingen...")
        # Up to and including the session year that started this calendar year
        for first_year in range(MODERN_FIRST_SESSION_YEAR, date.today().year + 1):
            session_year = f"{first_year}-{first_year + 1}"
            if f"sru/{session_year}" in completed_years:
                continue
            docs = await crawler.crawl_modern_sru_session_year(session_year)
            all_documents.extend(docs)
            completed_years.add(f"sru/{session_year}")
            logger.info(f"  {session_year}: {len(docs)} documents (total: {len(all_documents)})")

    # Save final index
    save_index(all_documents, index_file)
    logger.info(f"Saved index with {len(all_documents)} documents to {index_file}")

    # Clean up progress file
    if progress_file.exists():
        progress_file.unlink()
        logger.info("Removed progress file (crawl complete)")

    return all_documents


async def download_all(
    documents: list[Document],
    output_dir: Path,
    formats: list[str],
    max_concurrent: int = 10,
    fail_fast: bool = False,
    delay: float = 0
):
    """Download all documents."""
    ssl_context = create_ssl_context()
    connector = aiohttp.TCPConnector(limit=max_concurrent, limit_per_host=5, ssl=ssl_context)
    timeout = aiohttp.ClientTimeout(total=300)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        downloader = DocumentDownloader(
            session, output_dir, formats, max_concurrent, fail_fast=fail_fast
        )

        logger.info(f"Starting download of {len(documents)} documents...")

        # Use tqdm for progress bar
        pbar = tqdm(total=len(documents), desc="Downloading", unit="doc",
                    dynamic_ncols=True)

        async def download_with_progress(doc: Document):
            result = await downloader.download_document(doc)
            if delay > 0 and not downloader.stop_flag:
                await asyncio.sleep(delay)
            pbar.update(1)
            pbar.set_postfix({
                'ok': downloader.downloaded,
                'skip': downloader.skipped,
                'fail': downloader.failed
            })
            return result

        # Stream results as they complete for real-time progress
        tasks = [download_with_progress(doc) for doc in documents]
        try:
            for coro in asyncio.as_completed(tasks):
                await coro
                if downloader.stop_flag:
                    break
        except RateLimitError as e:
            pbar.close()
            logger.error(f"Stopped due to error: {e}")
            logger.info("Switch VPN and restart to continue.")
            return

        pbar.close()

        logger.info(
            f"Download complete. "
            f"Downloaded: {downloader.downloaded}, "
            f"Skipped: {downloader.skipped}, "
            f"Failed: {downloader.failed}"
        )


def create_ssl_context():
    """Create SSL context that works with repository.overheid.nl."""
    # The government site has certificate chain issues, so we need to be permissive
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    return ssl_context


async def main():
    parser = argparse.ArgumentParser(
        description="Download Dutch parliamentary proceedings (Handelingen)"
    )
    parser.add_argument(
        "--output-dir", "-o",
        type=Path,
        default=Path("./handelingen"),
        help="Output directory for downloads"
    )
    parser.add_argument(
        "--source", "-s",
        choices=["historical", "modern", "all"],
        default="historical",
        help="Which source to download from"
    )
    parser.add_argument(
        "--formats", "-f",
        nargs="+",
        default=["pdf"],
        help="Formats to download (pdf, xml, ocr)"
    )
    parser.add_argument(
        "--crawl-only",
        action="store_true",
        help="Only crawl and create index, don't download"
    )
    parser.add_argument(
        "--max-concurrent", "-c",
        type=int,
        default=10,
        help="Maximum concurrent downloads"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging"
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop on first failure and log detailed error (for debugging rate limits)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0,
        help="Delay in seconds between downloads (to avoid rate limits)"
    )

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    ssl_context = create_ssl_context()
    connector = aiohttp.TCPConnector(limit=20, ssl=ssl_context)
    async with aiohttp.ClientSession(connector=connector) as session:
        # Crawl and get document index
        documents = await crawl_and_save_index(session, args.output_dir, args.source)

        if args.crawl_only:
            logger.info("Crawl-only mode, skipping downloads")
            return

        # Download documents
        await download_all(
            documents,
            args.output_dir,
            args.formats,
            args.max_concurrent,
            fail_fast=args.fail_fast,
            delay=args.delay
        )


if __name__ == "__main__":
    asyncio.run(main())
