# Texas Court of Appeals Opinion Scraper

An automated system for downloading, analyzing, and reporting on criminal law opinions from all 14 Texas Courts of Appeals using Claude AI for legal analysis.

## Features

- **Automated Opinion Downloading**: Scrapes all 14 Texas Courts of Appeals for criminal opinions
- **AI-Powered Legal Analysis**: Uses Claude 4 Sonnet to identify interesting legal issues
- **Professional PDF Reports**: Generates comprehensive reports with direct links to court opinions
- **Error Detection & Retry**: Robust downloading with retry logic and error handling
- **Database Storage**: SQLite database for tracking opinions, analyses, and metadata
- **Flexible Execution**: Multiple modes for scraping, analysis, and reporting

## Key Capabilities

### Legal Issue Detection
The system identifies:
- Novel or controversial legal questions
- Cases relying on pre-2000 Court of Criminal Appeals precedent
- Logical errors in court reasoning (category errors, internal contradictions, circular reasoning)
- Emerging legal doctrines and unsettled law

### Report Generation
- **Direct PDF Links**: Clickable links to original court opinion PDFs
- **Case Details**: Court, date, case number, and issue count
- **Full Analysis**: Complete Claude analysis with legal reasoning
- **Professional Formatting**: Clean, organized PDF reports

## Installation

1. **Clone the repository**:
   ```bash
   git clone <repository-url>
   cd Scrape_COA_Opinions
   ```

2. **Create virtual environment**:
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # On Windows: .venv\Scripts\activate
   ```

3. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **Configure environment**:
   - Copy `.env` and add your Claude API key:
   ```bash
   CLAUDE_API_KEY=your_claude_api_key_here
   ```

## Usage

### Basic Commands

```bash
# Download and analyze today's opinions (default)
python pdrbot.py

# Download opinions only
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

# Update existing records with direct PDF URLs
python pdrbot.py backfill-urls
```

### Configuration

Edit `.env` file to customize:

```bash
# Claude API settings
CLAUDE_API_KEY=your_key_here
CLAUDE_MODEL=claude-3-7-sonnet-20250219
CLAUDE_MAX_TOKENS=64000

# Network settings
REQUEST_TIMEOUT=30
MAX_RETRIES=3
DOWNLOAD_DELAY=1
COURT_DELAY=2

# Analysis settings
ANALYSIS_ENABLED=true
AUTO_GENERATE_REPORTS=true

# Courts to scrape (1-14)
COURTS=1,2,3,4,5,6,7,8,9,10,11,12,13,14
```

## Project Structure

```
├── pdrbot.py              # Main application
├── scraper.py             # Legacy scraper (deprecated)
├── pdrbot-prompt          # Legal analysis prompt for Claude
├── .env                   # Environment configuration
├── requirements.txt       # Python dependencies
├── data/                  # Downloaded opinions and reports
│   ├── YYYYMMDD/         # Daily opinion folders
│   ├── pdrbot.db         # SQLite database
│   └── *.pdf             # Generated reports
└── README.md             # This file
```

## Database Schema

### `opinions` table
- Case metadata, file paths, and direct PDF URLs
- Tracks court, date, case number, opinion type
- Links to original court opinion PDFs

### `analysis` table
- Claude AI analysis results
- Interesting issue flags and counts
- Analysis timestamp and model used

### `daily_runs` table
- Execution tracking and statistics
- Success/failure monitoring

## Analysis Prompt

The system uses a sophisticated prompt (`pdrbot-prompt`) that instructs Claude to identify:

1. **Novel Legal Issues**: Controversial or unsettled legal questions
2. **Pre-2000 Precedent**: Cases relying on outdated Court of Criminal Appeals authority
3. **Logical Errors**: Flawed reasoning, category errors, contradictions
4. **Emerging Doctrines**: New or evolving legal principles

## Report Features

Generated PDF reports include:
- **Summary Statistics**: Total cases, issues found, generation date
- **Case-by-Case Analysis**: Detailed breakdown with full Claude analysis
- **Direct Links**: Clickable URLs to original court opinion PDFs
- **Professional Formatting**: Clean layout with proper pagination

## Error Handling

- **Retry Logic**: 3 attempts with exponential backoff for downloads
- **PDF Validation**: Checks file integrity and PDF magic bytes
- **Network Resilience**: Handles timeouts, connection errors
- **Database Integrity**: Transaction safety and error recovery

## Scheduling

For daily automation, add to crontab:
```bash
# Run at 12:01 AM daily (Tuesday-Saturday for Monday-Friday opinions)
1 0 * * 2-6 cd /path/to/Scrape_COA_Opinions && ./.venv/bin/python pdrbot.py
```

## Output

### Daily Download Statistics
```
2025-07-25 06:42:10,872 - INFO - Daily scrape completed!
2025-07-25 06:42:10,872 - INFO - Courts checked: 14
2025-07-25 06:42:10,873 - INFO - Total cases found: 62
2025-07-25 06:42:10,873 - INFO - Total files downloaded: 62
```

### Analysis Progress
```
2025-07-25 07:28:49,436 - INFO - Completed analysis for 01-23-00771-CR
2025-07-25 07:28:49,752 - INFO - Saved analysis for 01-23-00771-CR
2025-07-25 07:28:51,753 - INFO - Analysis batch complete: 1 processed, 0 failed
```

### Report Generation
```
2025-07-25 07:44:00,778 - INFO - Analysis report generated: data/analysis_report_20250725_074400.pdf
Report generated: data/analysis_report_20250725_074400.pdf
```

## Requirements

- Python 3.8+
- Claude API key (Anthropic)
- Internet connection for court website access
- ~1GB storage for daily opinions (varies by volume)

## Legal Notice

This tool is for legal research and educational purposes. Always verify information against official court records. The AI analysis is supplementary and should not replace professional legal judgment.

## Contributing

1. Fork the repository
2. Create a feature branch
3. Make changes with appropriate tests
4. Submit a pull request

## License

[Add your license information here]

## Support

For issues or questions:
1. Check the logs in the output
2. Verify your `.env` configuration
3. Ensure Claude API key is valid
4. Review network connectivity

## Changelog

### v2.0 (Current)
- Added Claude AI analysis integration
- Implemented PDF report generation with direct links
- Enhanced error handling and retry logic
- Added comprehensive database schema
- Streaming support for long Claude requests

### v1.0 (Legacy)
- Basic opinion downloading
- Simple file organization
- Manual analysis required 