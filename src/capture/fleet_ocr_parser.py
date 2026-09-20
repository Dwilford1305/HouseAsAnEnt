"""Stage 1 Data Capture: ingest physical fuel receipts via offline OCR.

This module discovers household-controlled receipt images (TAVI / unstructured)
under ``data/raw/``, runs Tesseract locally, and projects two deterministic
fields — transaction date and total amount — onto a console-verifiable
dictionary per file.

The complete, unedited Tesseract payload is always retained as
``raw_ocr_text`` (Raw Payload Preservation / ELT). Capture stays isolated
from Stage 3 warehouse loads — no database I/O lives here.

Department: Fleet Management (fuel receipt OCR).
"""

from __future__ import annotations

import logging
import pprint
import re
from pathlib import Path
from typing import Dict, Final, List, Match, Optional, Pattern, TypedDict

import pytesseract
from PIL import Image, ImageEnhance

# Resolve paths from this file so the script is deterministic regardless of CWD.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
RAW_DATA_DIR: Final[Path] = PROJECT_ROOT / "data" / "raw"
RECEIPT_GLOB_PATTERN: Final[str] = "mock_fuel_receipt*.jpg"
DEFAULT_RECEIPT_PATH: Final[Path] = RAW_DATA_DIR / "mock_fuel_receipt.jpg"

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

# 2x LANCZOS upsample: Tesseract is trained near 300 DPI; phone JPEGs are
# often ~72-150 DPI, so doubling pixel density recovers stroke width.
UPSCALE_FACTOR: Final[int] = 2

# Mild contrast only — 1.5 separates ink from paper without clipping glyphs
# the way a hard binary threshold does on uneven lighting.
CONTRAST_ENHANCEMENT_FACTOR: Final[float] = 1.5

# PSM 6: assume a uniform block of text (a receipt), not a multi-column page.
TESSERACT_PSM_CONFIG: Final[str] = "--psm 6"

# Year-first calendar dates after '/' and '_' have been normalized to '-'.
# Month/day are 1-2 digits so OCR dropping a leading zero (2016-5-4) still hits.
_ISO_LIKE_DATE: Final[Pattern[str]] = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{1,2})-(?P<day>\d{1,2})"
)

# Currency-style decimals only (33.00, 96.04). No TOTAL label — Tesseract often
# mangles that word. The grand total is the largest two-decimal figure on the slip.
_CURRENCY_AMOUNT: Final[Pattern[str]] = re.compile(r"\d+\.\d{2}")


class FuelReceiptCapture(TypedDict):
    """Stage 1 capture payload for a single fuel receipt.

    Attributes:
        date: ISO 8601 calendar date (``YYYY-MM-DD``), or ``None`` if unparsed.
        total_amount: Tendered total as a float, or ``None`` if unparsed.
        raw_ocr_text: Complete, unedited Tesseract string (ELT preservation).
    """

    date: Optional[str]
    total_amount: Optional[float]
    raw_ocr_text: str


def _to_iso_date(match: Match[str]) -> Optional[str]:
    """Rebuild a regex date match as an ISO 8601 calendar date.

    Args:
        match: A match containing ``year``, ``month``, and ``day`` groups.

    Returns:
        A zero-padded ``YYYY-MM-DD`` string, or ``None`` if month/day are
        outside a valid calendar range (OCR noise such as ``2022-86-71``).
    """
    month: int = int(match.group("month"))
    day: int = int(match.group("day"))
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None

    # CACTUS Accurate: warehouse-friendly dates, independent of printer glyphs.
    return f"{int(match.group('year')):04d}-{month:02d}-{day:02d}"


def extract_transaction_date(raw_ocr_text: str) -> Optional[str]:
    """Extract the first plausible year-first transaction date from OCR text.

    Args:
        raw_ocr_text: Unedited Tesseract payload from the receipt image.

    Returns:
        An ISO 8601 date string, or ``None`` when no pattern matches.
    """
    # OCR often emits 2016/05/04 or 2016_05_04; normalize before the dash regex.
    normalized_text: str = raw_ocr_text.replace("_", "-").replace("/", "-")
    date_match: Match[str]
    for date_match in _ISO_LIKE_DATE.finditer(normalized_text):
        iso_date: Optional[str] = _to_iso_date(date_match)
        if iso_date is not None:
            return iso_date

    LOGGER.warning("No transaction date matched in OCR payload.")
    return None


def extract_total_amount(raw_ocr_text: str) -> Optional[float]:
    """Extract the tender total as the largest two-decimal amount on the page.

    Fuel slips print many currency-like numbers (price/L, tax, litres with a
    false decimal). The amount paid is almost always the maximum ``xx.xx``
    value, which survives even when Tesseract misspells TOTAL.

    Args:
        raw_ocr_text: Unedited Tesseract payload from the receipt image.

    Returns:
        The heuristic total as a float, or ``None`` when no decimals match.
    """
    amount_tokens: List[str] = _CURRENCY_AMOUNT.findall(raw_ocr_text)
    if not amount_tokens:
        LOGGER.warning("No decimal currency amounts matched in OCR payload.")
        return None

    # CACTUS Clean: tokens are already digit.digit digit; max() is the tender.
    parsed_amounts: List[float] = [float(token) for token in amount_tokens]
    return max(parsed_amounts)


def _preprocess_receipt_image(image_path: Path) -> Image.Image:
    """Load a receipt, upsample it, and apply a mild grayscale contrast boost.

    Tesseract's LSTM is calibrated for ~300 DPI scans. Phone photos are
    typically much coarser; a 2x LANCZOS resize synthesizes that density
    without the information loss of a hard binary threshold. Grayscale
    drops chroma noise; contrast 1.5 is enough to separate thermal ink
    from paper.

    Args:
        image_path: Path to a household-controlled receipt JPEG.

    Returns:
        A contrast-enhanced grayscale Pillow image at 2x resolution.
    """
    with Image.open(image_path) as source_image:
        loaded_image: Image.Image = source_image.copy()

    upscaled_image: Image.Image = loaded_image.resize(
        (loaded_image.width * UPSCALE_FACTOR, loaded_image.height * UPSCALE_FACTOR),
        Image.Resampling.LANCZOS,
    )
    grayscale_image: Image.Image = upscaled_image.convert("L")
    contrast_enhancer: ImageEnhance.Contrast = ImageEnhance.Contrast(grayscale_image)
    return contrast_enhancer.enhance(CONTRAST_ENHANCEMENT_FACTOR)


def extract_raw_ocr_text(image_path: Path) -> str:
    """Run offline Tesseract OCR against a preprocessed receipt image.

    Args:
        image_path: Path to a household-controlled receipt JPEG.

    Returns:
        The complete, unedited Tesseract string (may be empty on a blank scan).

    Raises:
        pytesseract.TesseractNotFoundError: If the Tesseract binary is missing.
    """
    preprocessed_image: Image.Image = _preprocess_receipt_image(image_path)
    raw_ocr_text: str = pytesseract.image_to_string(
        preprocessed_image,
        config=TESSERACT_PSM_CONFIG,
    )
    if not raw_ocr_text.strip():
        LOGGER.warning("Tesseract returned an empty payload for the receipt image.")
    return raw_ocr_text


def parse_fuel_receipt(
    image_path: Path = DEFAULT_RECEIPT_PATH,
) -> FuelReceiptCapture:
    """Parse a local fuel-receipt image into date, total, and raw OCR text.

    Args:
        image_path: Path to a household-controlled receipt JPEG. Defaults to
            the synthetic mock at ``data/raw/mock_fuel_receipt.jpg``.

    Returns:
        A dictionary with ``date``, ``total_amount``, and ``raw_ocr_text``.
        Unparsed fields are ``None``; the raw payload is always populated
        when the image can be opened.

    Raises:
        FileNotFoundError: If ``image_path`` does not exist.
    """
    if not image_path.is_file():
        raise FileNotFoundError(
            f"Fuel receipt image not found at {image_path}. "
            "Place a synthetic mock at data/raw/mock_fuel_receipt.jpg "
            "(no live camera ingest or external OCR APIs)."
        )

    raw_ocr_text: str = extract_raw_ocr_text(image_path)

    capture: FuelReceiptCapture = {
        "date": extract_transaction_date(raw_ocr_text),
        "total_amount": extract_total_amount(raw_ocr_text),
        # ELT governance: never discard the source payload at capture time.
        "raw_ocr_text": raw_ocr_text,
    }
    return capture


def discover_fuel_receipts(raw_dir: Path = RAW_DATA_DIR) -> List[Path]:
    """List mock fuel-receipt JPEGs in deterministic filename order.

    Args:
        raw_dir: Stage 1 raw capture directory (defaults to ``data/raw/``).

    Returns:
        Sorted paths matching ``mock_fuel_receipt*.jpg``.
    """
    return sorted(raw_dir.glob(RECEIPT_GLOB_PATTERN))


def main() -> None:
    """OCR every mock fuel receipt and print each ELT dictionary."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(name)s | %(message)s",
    )

    receipt_paths: List[Path] = discover_fuel_receipts()
    if not receipt_paths:
        LOGGER.error(
            "No fuel receipts matching %s in %s. "
            "Place synthetic mocks in data/raw/ (no live camera ingest).",
            RECEIPT_GLOB_PATTERN,
            RAW_DATA_DIR,
        )
        return

    print("\n=== Fleet Fuel Receipt Batch (Stage 1 Capture) ===")
    for receipt_path in receipt_paths:
        print(f"\n{receipt_path.name}")
        try:
            capture: Dict[str, Optional[object]] = dict(
                parse_fuel_receipt(receipt_path)
            )
        except pytesseract.TesseractNotFoundError:
            LOGGER.error(
                "Tesseract binary not found. Install the system package "
                "(e.g. `sudo apt-get install tesseract-ocr`) and retry."
            )
            return
        except Exception as exc:
            # Graceful batch: one bad TAVI asset must not abort the rest.
            LOGGER.error(
                "Failed to ingest %s; continuing batch. Reason: %s",
                receipt_path.name,
                exc,
            )
            continue

        pprint.pprint(capture, width=100, sort_dicts=False)


if __name__ == "__main__":
    main()
