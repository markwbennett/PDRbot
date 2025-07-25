#!/bin/bash

# PDRBot Daily Automation Script
# This script runs PDRBot daily automation at 12:01 AM

# Set working directory to the script location
cd "$(dirname "$0")"

# Set up environment
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

# Log file for cron job output
LOG_FILE="data/pdrbot_cron.log"

# Function to log with timestamp
log_message() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE"
}

log_message "Starting PDRBot daily automation"

# Activate virtual environment and run automation
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    log_message "Virtual environment activated"
else
    log_message "ERROR: Virtual environment not found"
    exit 1
fi

# Run PDRBot automation
./.venv/bin/python pdrbot.py auto >> "$LOG_FILE" 2>&1
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log_message "PDRBot automation completed successfully"
else
    log_message "PDRBot automation failed with exit code $EXIT_CODE"
fi

log_message "PDRBot daily automation finished"
exit $EXIT_CODE 