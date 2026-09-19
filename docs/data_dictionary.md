# Household Enterprise Data Dictionary

## 1. Governance & Quality (CACTUS Framework)
All data entering the staging environment must pass the CACTUS validation rules before being loaded into the OLAP warehouse.

*   **Clean & Accurate:** Dates must follow ISO 8601 formatting (`YYYY-MM-DD`). Currency fields must be explicitly cast as floats or decimals, stripping out `$` symbols and commas via Regex.
*   **Consistent:** Vendor and merchant names must be normalized to a standard nominal taxonomy (e.g., transforming `AMZN Mktp`, `Amazon.com`, and `AMZN` all into a single `Amazon` entity).
*   **Timely:** Data pipelines should be configured to process incoming TAVI (unstructured data) or OLTP extracts within 48 hours of receipt.
*   **Unique:** Every transaction or log entry must be assigned a unique primary key (UUID) to prevent duplication during ELT staging.
*   **Secure:** Personally Identifiable Information (PII) and sensitive banking numbers must be masked or redacted at the point of capture before entering the data warehouse.

## 2. Statistical Architecture & Data Types

To perform accurate analytics, we must define the Data Type and the targeted Question. 

### Accepted Data Types
*   **Nominal:** Categorical data without a strict order (e.g., Expense Categories like `Groceries`, `Dining Out`, `Utilities`).
*   **Ordinal:** Categorical data with a set order (e.g., Maintenance Priority: `Low`, `Medium`, `High`).
*   **Numeric:** Quantifiable numbers used for calculations (e.g., `Total_Cost`, `Fuel_Volume`, `Kilowatt_Hours`).

### Centrality Measures
*   **Mean:** The arithmetic average. Because it leaves nothing out, it is highly susceptible to edge cases. Used sparingly, primarily for high-frequency, low-variance purchases.
*   **Median:** The exact middle value. Used as the primary centrality measure for utility bills and fuel costs to prevent seasonal outliers (like a massive winter heating bill or a long road trip) from skewing the monthly baseline budgets.

## 3. Core Entities (Draft Schema)

### 3.1. Financial Ledger (Procurement & Supply Chain)
| Field Name | Type | Description |
| :--- | :--- | :--- |
| `transaction_id` | UUID | Unique identifier generated at ingestion. |
| `date` | Date | The date the transaction cleared. |
| `vendor_normalized`| Nominal | The cleaned merchant name (CACTUS consistent). |
| `category` | Nominal | Spend category (e.g., Utilities, Groceries). |
| `amount` | Numeric | Total transaction cost. |

### 3.2. Asset Logs (Fleet & Facilities)
| Field Name | Type | Description |
| :--- | :--- | :--- |
| `log_id` | UUID | Unique identifier for the maintenance event. |
| `asset_type` | Nominal | e.g., `Vehicle`, `HVAC`, `Plumbing`. |
| `service_date` | Date | Date the maintenance was performed. |
| `cost` | Numeric | Cost of parts/labor. |
| `next_service_due` | Date | Forecasted date for next required maintenance. |