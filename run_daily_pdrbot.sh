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
    # Try to create log file, fallback to local directory if data symlink is broken
    if ! echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOG_FILE" 2>/dev/null; then
        # Fallback to local log file if data directory is not accessible
        LOCAL_LOG="pdrbot_cron.log"
        echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" >> "$LOCAL_LOG"
        # Update log file path for subsequent calls
        LOG_FILE="$LOCAL_LOG"
    fi
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
if [ -w "$(dirname "$LOG_FILE")" ] 2>/dev/null; then
    ./.venv/bin/python pdrbot.py auto >> "$LOG_FILE" 2>&1
else
    # Fallback to local log if data directory not writable
    LOCAL_LOG="pdrbot_cron.log"
    ./.venv/bin/python pdrbot.py auto >> "$LOCAL_LOG" 2>&1
    LOG_FILE="$LOCAL_LOG"
fi
EXIT_CODE=$?

if [ $EXIT_CODE -eq 0 ]; then
    log_message "PDRBot automation completed successfully"
else
    log_message "PDRBot automation failed with exit code $EXIT_CODE"
fi

log_message "PDRBot daily automation finished"
exit $EXIT_CODE 