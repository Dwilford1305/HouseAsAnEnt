# Household as an Enterprise: A Full-Stack Data Pipeline 

## Project Objective
The primary goal of this project is to architect and implement a production-grade data ecosystem that treats a standard household as a corporate entity . By segmenting household operations into "departments," this project demonstrates mastery over the full data lifecycle—from capturing unstructured physical data to delivering predictive business intelligence . The project utilizes the CACTUS framework (Clean, Accurate, Consistent, Timely, Unique, and Secure) to transform raw domestic events into actionable enterprise-level insights .

## Enterprise Departments

*   **Department 1: Fleet Management (Vehicle Analytics)** 
    *   Manages the lifecycle and operational efficiency of household transportation assets, focusing on vehicle health, fuel costs, and preventative maintenance .
    *   Utilizes Python-based OCR to ingest unstructured data (TAVI) from gas station receipts and automotive shop invoices .
*   **Department 2: Procurement & Supply Chain (Household Spending)** 
    *   Oversees the acquisition of goods and services via high-granularity analysis of food systems, convenience services, and general supply procurement .
    *   Automates the extraction (OLTP) of financial data from bank statements, credit card portals, and email parsers .
*   **Department 3: IT Infrastructure (Home Lab Telemetry)** 
    *   Monitors the digital health, network throughput, server uptime, and containerized application health of the household .
    *   Programmatically extracts raw system logs from networking hardware and Docker container status APIs .
*   **Department 4: Facilities Management (Physical Asset Management)** 
    *   Tracks and analyzes property health, utility consumption, and routine structural maintenance .
    *   Captures data through unstructured document ingestion (tax assessments, paper bills), system of record extraction (utility provider scraping), and manual maintenance logs .

## Technical Stack

| Component | Technology | Role |
| :--- | :--- | :--- |
| **Orchestration & Logic** | Python | Primary engine for OCR, Scraping, and ETL logic.  |
| **Database (OLAP)** | SQL / Supabase | Cloud-hosted relational warehouse for long-term storage.  |
| **Containerization** | Docker | Hosting environment for data collectors and log providers.  |
| **Visualization** | Power BI / Excel | Business Intelligence layer for stakeholder reporting.  |
| **Version Control** | GitHub | Management of CI/CD pipelines and codebase.  |

## Data Flow Architecture

1.  **OLTP (Source Systems):** Raw data originates from physical receipts (OCR), financial institutions (APIs/Scrapers), and system logs (Telemetry) .
2.  **Middleware (ETL/ELT):** Python scripts act as the middleware, performing validation, cleansing, and normalization (CACTUS) before transitioning data through the pipeline .
3.  **OLAP (Warehouse):** Transformed data is loaded into Supabase/Postgres, structured for analytical processing and historical trend analysis .
4.  **Presentation Layer:** Structured data is queried by Power BI to generate an "Enterprise Executive Dashboard" for household decision-makers .