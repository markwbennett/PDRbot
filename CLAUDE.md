# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Essential Commands

### Running PDRBot
```bash
# Main application - downloads and analyzes today's opinions (default behavior)
python pdrbot.py

# Download opinions only (no analysis)
python pdrbot.py scrape

# Analyze existing unanalyzed opinions
python pdrbot.py analyze

# Analyze specific number of opinions
python pdrbot.py analyze 10

# Download + analyze everything
python pdrbot.py both

# Generate PDF report from analyses
python pdrbot.py report

# Generate report for specific date
python pdrbot.py report 2025-07-24

# Generate today's daily report
python pdrbot.py daily-report

# Run daily automation (used by cron)
python pdrbot.py auto
```

### Environment Setup
```bash
# Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Daily Automation
```bash
# Run the daily automation script (includes logging)
./run_daily_pdrbot.sh
```

## High-Level Architecture

### Core Components

**PDRBot (`pdrbot.py`)**: The main application that orchestrates the entire pipeline:
- **Court Scraping**: Downloads criminal law opinions from all 14 Texas Courts of Appeals
- **AI Analysis**: Uses Claude 4 Sonnet to analyze opinions for interesting legal issues
- **Report Generation**: Creates professional PDF reports with direct links to court opinions
- **Database Management**: SQLite database (`data/pdrbot.db`) for tracking opinions, analyses, and metadata
- **Email System**: Sends daily reports and manages subscription system

**Legacy Scraper (`scraper.py`)**: Deprecated scraper component, replaced by integrated functionality in pdrbot.py

**Analysis Prompt (`pdrbot-prompt`)**: Sophisticated legal analysis prompt for Claude AI with strict language requirements and specific criteria for identifying:
- Novel or controversial legal questions
- Cases relying on pre-2000 Court of Criminal Appeals precedent
- Logical errors in court reasoning
- Constitutional issues and statutory interpretation disputes

### Database Schema
- `opinions` table: Case metadata, file paths, direct PDF URLs, court information
- `analysis` table: Claude AI analysis results, interesting issue flags and counts
- `daily_runs` table: Execution tracking and statistics

### Data Organization
```
data/
├── YYYYMMDD/           # Daily opinion folders with PDF files
├── pdrbot.db          # SQLite database
├── pdrbot_report_*.pdf # Generated analysis reports
├── members.json       # Email subscription list
└── pdrbot_cron.log   # Automation logs
```

### Environment Configuration
The application uses `.env` file for configuration:
- Claude API settings (key, model, token limits)
- Network settings (timeouts, retries, delays)
- Email configuration (SMTP, recipients, subscription management)
- Court selection and analysis toggles

### Analysis Pipeline
1. **Scrape**: Download opinions from Texas Courts of Appeals websites
2. **Analyze**: Process PDFs through Claude AI using the specialized legal prompt
3. **Report**: Generate formatted PDF reports with case summaries and direct court links
4. **Email**: Distribute reports to subscribers with subscription management

### Automation
- Runs Tuesday-Saturday at 9:10 AM via cron (`run_daily_pdrbot.sh`)
- Collects opinions from previous business day
- Comprehensive error handling and retry logic
- Professional PDF generation with ReportLab

## Important Files
- `pdrbot-prompt`: Legal analysis prompt with strict language requirements - never modify forbidden/required language lists
- `requirements.txt`: Python dependencies including anthropic, reportlab, beautifulsoup4
- `run_daily_pdrbot.sh`: Daily automation script with logging and error handling
- `.env`: Configuration file (not in repo) - contains Claude API key and email credentials