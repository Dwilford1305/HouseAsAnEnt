"""Stage 1 Data Capture: ingest household-controlled bank statement CSVs.

This module reads a static, offline bank statement extract (OLTP) and
produces a console-verifiable DataFrame of *cleared* transactions. Capture
stays isolated from Stage 3 warehouse loads — no database I/O lives here.

Vendor Schema Variance is handled by a header-driven mapping layer: each
bank export is detected from its raw CSV headers, then projected onto a
canonical ledger shape before any CACTUS cleaning.

Department: Procurement & Supply Chain (financial ledger extraction).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Dict, Final, List, Optional

import pandas as pd
from dotenv import load_dotenv

# Resolve paths from this file so the script is deterministic regardless of CWD.
PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[2]
MOCK_CSV_PATH: Final[Path] = (
    PROJECT_ROOT / "data" / "raw" / "mock_bank_statement.csv"
)

# 12-factor config: secrets and local paths live in .env, never in source.
load_dotenv(dotenv_path=PROJECT_ROOT / ".env")


def _resolve_default_csv_path() -> Path:
    """Resolve the capture CSV from ``BANK_CSV_PATH``, else the mock extract.

    Returns:
        An absolute path to the bank statement CSV. Relative env values are
        joined to the project root so the working directory cannot break ingest.
    """
    env_csv_path: Optional[str] = os.getenv("BANK_CSV_PATH")
    if env_csv_path is None or not env_csv_path.strip():
        # Safe fallback: clone-and-run works without a local .env file.
        return MOCK_CSV_PATH

    configured_path: Path = Path(env_csv_path.strip()).expanduser()
    if not configured_path.is_absolute():
        configured_path = PROJECT_ROOT / configured_path
    return configured_path.resolve()


DEFAULT_CSV_PATH: Final[Path] = _resolve_default_csv_path()

# Header marker that uniquely identifies the signed-amount vendor export.
PREFERRED_PACKAGE_MARKER: Final[str] = "Sub-description"

# Raw vendor header -> canonical Stage 1 field. Only the columns we ingest.
PREFERRED_PACKAGE_HEADER_MAP: Final[Dict[str, str]] = {
    "Date": "post_date",
    "Sub-description": "description",
    "Amount": "amount",
}

REQUIRED_SPLIT_LEDGER_COLUMNS: Final[List[str]] = [
    "account_number",
    "post_date",
    "check",
    "description",
    "debit",
    "credit",
    "status",
]

CANONICAL_COLUMNS: Final[List[str]] = [
    "account_number",
    "post_date",
    "check",
    "description",
    "amount",
    "status",
]

LOGGER: Final[logging.Logger] = logging.getLogger(__name__)


def _to_snake_case(column_name: str) -> str:
    """Normalize a source header to lowercase snake_case.

    Args:
        column_name: Original CSV header (e.g. ``"Post Date"``).

    Returns:
        A snake_case identifier (e.g. ``"post_date"``).
    """
    # CACTUS Consistent: warehouse-friendly identifiers, no spaces or mixed case.
    return column_name.strip().lower().replace(" ", "_")


def _redact_account_number(value: str) -> str:
    """Mask an account number, preserving only the last four digits.

    Args:
        value: Raw account identifier from the statement extract.

    Returns:
        A redacted token such as ``XXXX-1234``.
    """
    # CACTUS Secure: PII must be redacted at capture, before any downstream use.
    digits: str = "".join(character for character in str(value) if character.isdigit())
    last_four: str = digits[-4:] if len(digits) >= 4 else digits
    return f"XXXX-{last_four}" if last_four else "XXXX-REDACTED"


def _redacted_account_from_filename(csv_path: Path) -> str:
    """Derive a redacted account number from a vendor export filename.

    Args:
        csv_path: Path such as ``Preferred_Package_3521_091926.csv``.

    Returns:
        A masked account token (e.g. ``XXXX-3521``).
    """
    # This vendor omits account number in-file; the last-four is an underscore token.
    filename_tokens: List[str] = csv_path.stem.split("_")
    for token in filename_tokens:
        if token.isdigit() and len(token) == 4:
            return f"XXXX-{token}"

    LOGGER.warning(
        "Could not derive account last-four from filename %s; using placeholder.",
        csv_path.name,
    )
    return "XXXX-REDACTED"


def _to_absolute_float(series: pd.Series) -> pd.Series:
    """Strip currency formatting and signs, then cast a series to float.

    Bank extracts often store debits as ``-142.50`` (or ``($142.50)``). We
    convert to an unsigned magnitude first so the signed ``amount`` column
    can apply a single, explicit debit/credit convention.

    Args:
        series: Debit or credit column, typically read as strings.

    Returns:
        A float series of absolute dollar amounts. Empty/unparsable cells
        become ``0.0`` so row-level arithmetic does not raise.
    """
    # CACTUS Clean/Accurate: strip $, commas, parentheses, and minus signs
    # before casting so pandas never infers object dtype on mixed blanks.
    cleaned: pd.Series = (
        series.astype(str)
        .str.replace(r"[\$,()]", "", regex=True)
        .str.replace("-", "", regex=False)
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    )
    numeric: pd.Series = pd.to_numeric(cleaned, errors="coerce")

    unparsable_count: int = int(numeric.isna().sum() - cleaned.isna().sum())
    if unparsable_count > 0:
        # Graceful failure: keep the batch alive; surface bad cells in logs.
        LOGGER.warning(
            "Coerced %s unparsable currency value(s) to 0.0.",
            unparsable_count,
        )

    return numeric.fillna(0.0).astype(float)


def _to_signed_float(series: pd.Series) -> pd.Series:
    """Strip ``$`` and commas from an already-signed amount, then cast to float.

    Args:
        series: Unified amount column that already encodes outflow as negative.

    Returns:
        A float series with vendor signs preserved. Empty/unparsable cells
        become ``0.0``.
    """
    # CACTUS Clean/Accurate: remove grouping/currency glyphs only — do not
    # flip or drop the vendor's signed cash-flow convention.
    cleaned: pd.Series = (
        series.astype(str)
        .str.replace(r"[\$,]", "", regex=True)
        .str.strip()
        .replace({"": pd.NA, "nan": pd.NA, "None": pd.NA})
    )
    numeric: pd.Series = pd.to_numeric(cleaned, errors="coerce")

    unparsable_count: int = int(numeric.isna().sum() - cleaned.isna().sum())
    if unparsable_count > 0:
        LOGGER.warning(
            "Coerced %s unparsable signed amount(s) to 0.0.",
            unparsable_count,
        )

    return numeric.fillna(0.0).astype(float)


def _apply_header_map(
    raw_frame: pd.DataFrame, header_map: Dict[str, str]
) -> pd.DataFrame:
    """Project vendor columns onto canonical names via an explicit mapping.

    Args:
        raw_frame: Freshly loaded CSV with original vendor headers.
        header_map: Mapping of raw header -> canonical field name.

    Returns:
        A DataFrame containing only the mapped canonical columns.

    Raises:
        ValueError: If a mapped source header is missing from the file.
    """
    missing_headers: List[str] = [
        source_header
        for source_header in header_map
        if source_header not in raw_frame.columns
    ]
    if missing_headers:
        raise ValueError(
            f"Vendor schema is missing expected headers: {missing_headers}"
        )

    renamed_frame: pd.DataFrame = raw_frame.rename(columns=header_map)
    canonical_names: List[str] = list(header_map.values())
    return renamed_frame.loc[:, canonical_names].copy()


def _finalize_canonical_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Apply shared CACTUS finishing rules to a schema-normalized ledger.

    Args:
        frame: Schema-mapped frame that already has a signed ``amount``.

    Returns:
        A canonical capture DataFrame with stable column order.
    """
    finalized: pd.DataFrame = frame.copy()
    finalized["account_number"] = finalized["account_number"].map(
        _redact_account_number
    )
    finalized["description"] = finalized["description"].astype(str).str.strip()
    # CACTUS Accurate: ISO-8601 calendar dates; invalid values become NaT.
    finalized["post_date"] = pd.to_datetime(
        finalized["post_date"], errors="coerce", format="ISO8601"
    )
    if "check" not in finalized.columns:
        finalized["check"] = ""
    return finalized.loc[:, CANONICAL_COLUMNS].reset_index(drop=True)


def _normalize_preferred_package_schema(
    raw_frame: pd.DataFrame, csv_path: Path
) -> pd.DataFrame:
    """Normalize the signed-amount vendor export (Sub-description schema).

    This bank already emits a unified ``Amount`` (debits negative, credits
    positive) and omits account number / clearance status in the file.

    Args:
        raw_frame: Raw CSV with original vendor headers.
        csv_path: Source path used to derive the redacted account last-four.

    Returns:
        A canonical cleared-transaction DataFrame.
    """
    mapped_frame: pd.DataFrame = _apply_header_map(
        raw_frame, PREFERRED_PACKAGE_HEADER_MAP
    )
    # Vendor does not ship account_number or status — inject CACTUS-safe defaults.
    mapped_frame["account_number"] = _redacted_account_from_filename(csv_path)
    mapped_frame["status"] = "Cleared"
    mapped_frame["check"] = ""
    # Skip debit-credit math: the vendor amount is already signed.
    mapped_frame["amount"] = _to_signed_float(mapped_frame["amount"])
    return _finalize_canonical_frame(mapped_frame)


def _normalize_split_ledger_schema(raw_frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize the original mock debit/credit ledger schema (fallback).

    Args:
        raw_frame: Raw CSV with original mock headers.

    Returns:
        A canonical DataFrame of cleared transactions.

    Raises:
        ValueError: If required source columns are missing after rename.
    """
    ledger_frame: pd.DataFrame = raw_frame.copy()
    ledger_frame.columns = [_to_snake_case(column) for column in ledger_frame.columns]

    missing_columns: List[str] = [
        column
        for column in REQUIRED_SPLIT_LEDGER_COLUMNS
        if column not in ledger_frame.columns
    ]
    if missing_columns:
        raise ValueError(
            f"Statement is missing required columns after rename: {missing_columns}"
        )

    # Pending rows are authorizations, not settled cash movement — drop them
    # so Stage 2 never treats holds as household spend.
    pending_mask: pd.Series = (
        ledger_frame["status"].str.strip().str.casefold() == "pending"
    )
    pending_count: int = int(pending_mask.sum())
    if pending_count:
        LOGGER.info("Filtered %s pending (uncleared) row(s).", pending_count)

    cleared_frame: pd.DataFrame = ledger_frame.loc[~pending_mask].copy()

    debit_abs: pd.Series = _to_absolute_float(cleared_frame["debit"])
    credit_abs: pd.Series = _to_absolute_float(cleared_frame["credit"])
    # Accounting convention: outflow (debit) is negative, inflow (credit) is
    # positive. Combining both sides into one numeric measure enables net
    # cash-flow KPIs without dual-column arithmetic downstream.
    cleared_frame["amount"] = credit_abs - debit_abs

    return _finalize_canonical_frame(cleared_frame)


def parse_bank_statement(csv_path: Path = DEFAULT_CSV_PATH) -> pd.DataFrame:
    """Parse a static bank statement CSV into cleared, signed transactions.

    Detects vendor schema from raw headers. Files containing
    ``Sub-description`` use the signed-amount mapping; all other files fall
    back to the original debit/credit mock logic.

    Args:
        csv_path: Path to a household-controlled bank statement CSV.
            Defaults to ``BANK_CSV_PATH`` from ``.env``, or the synthetic
            mock file under ``data/raw/`` when that variable is unset.

    Returns:
        A DataFrame of cleared transactions with snake_case columns and a
        single signed ``amount`` column (debits negative, credits positive).
        Account numbers are redacted.

    Raises:
        FileNotFoundError: If ``csv_path`` does not exist.
        ValueError: If required source columns are missing after rename.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(
            f"Bank statement CSV not found at {csv_path}. "
            "Place a mock extract in data/raw/ (no live bank APIs)."
        )

    # dtype=str keeps empty amount cells as "" so we control numeric cast.
    raw_frame: pd.DataFrame = pd.read_csv(csv_path, dtype=str, keep_default_na=False)
    raw_frame.columns = [str(column).strip() for column in raw_frame.columns]
    raw_headers: List[str] = list(raw_frame.columns)

    # Schema Mapping: inspect raw headers before any CACTUS transforms.
    if PREFERRED_PACKAGE_MARKER in raw_headers:
        LOGGER.info(
            "Detected signed-amount vendor schema (%s present).",
            PREFERRED_PACKAGE_MARKER,
        )
        return _normalize_preferred_package_schema(raw_frame, csv_path)

    LOGGER.info("Detected split debit/credit ledger schema (default fallback).")
    return _normalize_split_ledger_schema(raw_frame)


def main() -> None:
    """Load the configured statement, print the cleaned extract, and exit."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s | %(name)s | %(message)s",
    )

    cleaned_transactions: pd.DataFrame = parse_bank_statement()
    print("\n=== Cleared Bank Transactions (Stage 1 Capture) ===")
    print(cleaned_transactions.to_string(index=False))
    print(f"\nRow count: {len(cleaned_transactions)}")
    print("Sign convention: amount < 0 is a debit (outflow); amount > 0 is a credit (inflow).")


if __name__ == "__main__":
    main()
