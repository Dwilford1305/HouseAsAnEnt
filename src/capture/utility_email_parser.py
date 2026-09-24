"""Stage 1 Data Capture with a Stage 2 staging handover for utility emails.

This module discovers household-controlled email exports (TAVI / unstructured)
under ``data/raw/``. It prefers a ``text/plain`` body and, when that part is
absent, strips a ``text/html`` part to text. Date, subject, and the max
decimal bill total are projected onto one dictionary per file.

Those dictionaries are appended in batch order and written to
``data/staging/staged_utility_bills.csv``. This handover is middleware
staging only — no warehouse connection lives here.

Department: Facilities Management (utility bill and e-receipt email ingest).
"""

from __future__ import annotations

import glob
import logging
import os
import pprint
import re
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Final, List, Optional, Pattern, TypedDict

import pandas as pd
from bs4 import BeautifulSoup
from dotenv import load_dotenv


# Resolve paths from this file so the script is deterministic regardless of CWD.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
STAGING_DIR: Final[Path] = PROJECT_ROOT / "data" / "staging"
STAGED_BILLS_PATH: Final[Path] = STAGING_DIR / "staged_utility_bills.csv"

# 12-factor config: the inbox glob lives in .env, never as a private path in source.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")

# Public default is mock-only. Without a .env file this cannot see private exports.
EMAIL_PATTERN: str = os.getenv("UTILITY_EMAIL_PATTERN", "data/raw/mock_*.eml")
if not EMAIL_PATTERN.strip():
    EMAIL_PATTERN = "data/raw/mock_*.eml"
CAPTURE_COLUMNS: Final[List[str]] = [
    "date",
    "subject",
    "total_amount",
    "raw_body_text",
]

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)

# Same tender heuristic as fleet OCR: the amount paid is the largest
# two-decimal figure. Commas are optional so "$1,234.56" still casts.
_CURRENCY_AMOUNT: Final[Pattern[str]] = re.compile(
    r"\d{1,3}(?:,\d{3})*\.\d{2}|\d+\.\d{2}"
)


class UtilityEmailCapture(TypedDict):
    """Stage 1 capture payload for a single utility-bill email.

    Attributes:
        date: ISO 8601 calendar date (``YYYY-MM-DD``), or ``None`` if unparsed.
        subject: Decoded Subject header, or ``None`` if the header is absent.
        total_amount: Largest two-decimal amount in the body text, or
            ``None`` when the body has no parsable currency figure.
        raw_body_text: Plain text, or HTML reduced to text when no plain part
            exists (ELT preservation of the readable payload).
    """

    date: Optional[str]
    subject: Optional[str]
    total_amount: Optional[float]
    raw_body_text: str


def _resolve_email_pattern(email_pattern: str = EMAIL_PATTERN) -> str:
    """Anchor a relative glob to the project root.

    Args:
        email_pattern: Glob from ``UTILITY_EMAIL_PATTERN`` or the mock default.

    Returns:
        A glob string ``glob.glob`` can run independent of the working directory.
    """
    pattern_path: Path = Path(email_pattern.strip()).expanduser()
    if not pattern_path.is_absolute():
        # Relative patterns are project paths, not whatever directory launched the script.
        pattern_path = PROJECT_ROOT / pattern_path
    return str(pattern_path)


def discover_utility_emails(email_pattern: str = EMAIL_PATTERN) -> List[Path]:
    """List email exports matching the configured glob, in filename order.

    Args:
        email_pattern: Search pattern. Defaults to ``EMAIL_PATTERN``, which is
            ``data/raw/mock_*.eml`` unless ``UTILITY_EMAIL_PATTERN`` is set.

    Returns:
        Sorted paths. Non-email files that match the glob are included here
        and rejected later so one bad asset cannot hide the rest of the batch.
    """
    resolved_pattern: str = _resolve_email_pattern(email_pattern)
    # glob (not Path.glob) is the requested discovery API; sort keeps reruns stable.
    matched_paths: List[str] = sorted(glob.glob(resolved_pattern))
    return [Path(path) for path in matched_paths]


def _decode_text_payload(part: EmailMessage) -> str:
    """Decode one MIME part to text without dropping undecodable bytes.

    Args:
        part: A non-multipart MIME part, typically ``text/plain``.

    Returns:
        The decoded text. An unknown charset falls back to UTF-8 with
        replacement characters so the batch continues.
    """
    payload: object = part.get_payload(decode=True)
    if payload is None:
        raw_payload: object = part.get_payload(decode=False)
        return raw_payload if isinstance(raw_payload, str) else ""

    if isinstance(payload, str):
        return payload

    encoded_bytes: bytes = bytes(payload)
    charset: str = part.get_content_charset() or "utf-8"
    try:
        # errors=replace: a bad byte must not abort the rest of the inbox export.
        return encoded_bytes.decode(charset, errors="replace")
    except LookupError:
        LOGGER.warning("Unknown charset %s; decoding part as UTF-8.", charset)
        return encoded_bytes.decode("utf-8", errors="replace")


def _collect_parts(message: EmailMessage, content_type: str) -> List[str]:
    """Collect decoded payloads of one MIME type, skipping containers.

    Args:
        message: A parsed RFC 822 message.
        content_type: MIME type to keep, such as ``text/plain``.

    Returns:
        Decoded part bodies in walk order. Empty when that type is absent.
    """
    matched_parts: List[str] = []
    if message.is_multipart():
        part: EmailMessage
        for part in message.walk():
            # The multipart container has no body of its own; skip to the leaves.
            if part.get_content_type() != content_type or part.is_multipart():
                continue
            matched_parts.append(_decode_text_payload(part))
    elif message.get_content_type() == content_type:
        matched_parts.append(_decode_text_payload(message))
    return matched_parts


def strip_html_to_text(html_content: str) -> str:
    """Reduce an HTML bill body to newline-separated visible text.

    Args:
        html_content: Raw ``text/html`` payload from the email.

    Returns:
        Tag-free text. Scripts and styles are omitted by the parser's
        visible-text walk.
    """
    # html.parser is in the standard library, so HTML-only bills need no
    # extra system binary the way Tesseract does.
    return BeautifulSoup(html_content, "html.parser").get_text(
        separator="\n",
        strip=True,
    )


def extract_body_text(message: EmailMessage) -> str:
    """Prefer ``text/plain``; strip ``text/html`` only when plain is absent.

    Multipart bills often carry an HTML twin with a styled total. Using the
    plain part first keeps that twin from changing the amount heuristic.
    HTML-only vendor mail has no plain part, so the tags are stripped before
    the same regex runs.

    Args:
        message: A parsed RFC 822 message.

    Returns:
        Readable body text. Empty when neither ``text/plain`` nor
        ``text/html`` is present.
    """
    plain_parts: List[str] = _collect_parts(message, "text/plain")
    if plain_parts:
        # Preserve each plain part verbatim; the join only separates siblings.
        return "\n".join(plain_parts)

    html_parts: List[str] = _collect_parts(message, "text/html")
    if not html_parts:
        LOGGER.warning(
            "No text/plain or text/html body; content type is %s.",
            message.get_content_type(),
        )
        return ""

    LOGGER.info("No text/plain part; stripping text/html to text.")
    stripped_parts: List[str] = [strip_html_to_text(part) for part in html_parts]
    return "\n".join(stripped_parts)


def extract_email_date(message: EmailMessage) -> Optional[str]:
    """Standardize the Date header to an ISO 8601 calendar date.

    Args:
        message: A parsed RFC 822 message.

    Returns:
        ``YYYY-MM-DD``, or ``None`` when the header is missing or not RFC 2822.
    """
    date_header: Optional[str] = message.get("Date")
    if not date_header:
        LOGGER.warning("Email is missing a Date header.")
        return None

    try:
        parsed_date: datetime = parsedate_to_datetime(date_header)
    except (TypeError, ValueError, IndexError, OverflowError):
        LOGGER.warning("Date header is not RFC 2822: %s", date_header)
        return None

    # CACTUS Accurate: calendar date only, matching fleet receipt capture.
    return parsed_date.date().isoformat()


def extract_subject(message: EmailMessage) -> Optional[str]:
    """Return the decoded Subject header.

    Args:
        message: A parsed RFC 822 message. ``policy.default`` already unfolds
            RFC 2047 encoded-words.

    Returns:
        The stripped subject, or ``None`` when the header is absent or blank.
    """
    subject_header: Optional[str] = message.get("Subject")
    if subject_header is None:
        return None

    stripped_subject: str = str(subject_header).strip()
    return stripped_subject or None


def extract_total_amount(raw_body_text: str) -> Optional[float]:
    """Extract the bill total as the largest two-decimal amount in the body.

    Utility mail prints many currency-like numbers (line items, tax, prior
    balance). The amount due is almost always the maximum ``xx.xx`` value,
    which matches the fleet receipt OCR heuristic.

    Args:
        raw_body_text: Unedited ``text/plain`` body.

    Returns:
        The heuristic total as a float, or ``None`` when no decimals match.
    """
    amount_tokens: List[str] = _CURRENCY_AMOUNT.findall(raw_body_text)
    if not amount_tokens:
        LOGGER.warning("No decimal currency amounts matched in the email body.")
        return None

    # CACTUS Clean: drop thousands separators before the float cast.
    parsed_amounts: List[float] = [
        float(token.replace(",", "")) for token in amount_tokens
    ]
    return max(parsed_amounts)


def _load_email_message(eml_path: Path) -> EmailMessage:
    """Parse an ``.eml`` file into a policy-default message.

    Args:
        eml_path: Path to a household-controlled email export.

    Returns:
        The parsed message.

    Raises:
        ValueError: If the file has no RFC 822 headers. Image bytes saved
            with an ``.eml`` suffix parse as a headerless blob and must not
            be treated as a bill.
    """
    raw_bytes: bytes = eml_path.read_bytes()
    # policy.default decodes RFC 2047 subjects and exposes EmailMessage.walk.
    message: EmailMessage = BytesParser(policy=policy.default).parsebytes(raw_bytes)
    if not message.keys():
        raise ValueError(
            f"{eml_path.name} has no RFC 822 headers and is not an email export."
        )
    return message


def parse_utility_email(eml_path: Path) -> UtilityEmailCapture:
    """Parse one local ``.eml`` export into date, subject, total, and raw body.

    Args:
        eml_path: Path to a household-controlled utility-bill email.

    Returns:
        A dictionary with ``date``, ``subject``, ``total_amount``, and
        ``raw_body_text``. Unparsed scalar fields are ``None``; the raw
        plain-text body is always present (possibly empty).

    Raises:
        FileNotFoundError: If ``eml_path`` does not exist.
        ValueError: If the file is not an RFC 822 message.
    """
    if not eml_path.is_file():
        raise FileNotFoundError(
            f"Utility email not found at {eml_path}. "
            "Place a synthetic .eml export in data/raw/ (no mailbox APIs)."
        )

    message: EmailMessage = _load_email_message(eml_path)
    raw_body_text: str = extract_body_text(message)

    capture: UtilityEmailCapture = {
        "date": extract_email_date(message),
        "subject": extract_subject(message),
        "total_amount": extract_total_amount(raw_body_text),
        # ELT governance: never discard the source payload at capture time.
        "raw_body_text": raw_body_text,
    }
    return capture


def stage_utility_bills(captures: List[UtilityEmailCapture]) -> Path:
    """Write the capture batch to the Stage 2 staging CSV.

    Args:
        captures: ELT dictionaries in ingest order. May be empty when every
            file in the batch failed.

    Returns:
        Path of the written CSV (``data/staging/staged_utility_bills.csv``).
    """
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    # Column list locks header order even when the batch is empty.
    staged_frame: pd.DataFrame = pd.DataFrame(captures, columns=CAPTURE_COLUMNS)
    staged_frame.to_csv(STAGED_BILLS_PATH, index=False)
    return STAGED_BILLS_PATH


def main() -> None:
    """Parse every ``.eml`` export, print each dictionary, and stage the batch."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(name)s | %(message)s",
    )

    email_paths: List[Path] = discover_utility_emails()
    if not email_paths:
        LOGGER.error(
            "No files matching %s. "
            "Place synthetic .eml exports in data/raw/ (no mailbox APIs).",
            EMAIL_PATTERN,
        )
        return

    captures: List[UtilityEmailCapture] = []
    print("\n=== Facilities Utility Email Batch (Stage 1 Capture) ===")
    for email_path in email_paths:
        print(f"\n{email_path.name}")
        try:
            capture: UtilityEmailCapture = parse_utility_email(email_path)
        except (OSError, ValueError) as exc:
            # Graceful batch: one bad TAVI asset must not abort the rest.
            LOGGER.error(
                "Failed to ingest %s; continuing batch. Reason: %s",
                email_path.name,
                exc,
            )
            continue

        captures.append(capture)
        pprint.pprint(dict(capture), width=100, sort_dicts=False)

    staged_path: Path = stage_utility_bills(captures)
    print(f"\nExported {len(captures)} bills to staging ({staged_path}).")


if __name__ == "__main__":
    main()
