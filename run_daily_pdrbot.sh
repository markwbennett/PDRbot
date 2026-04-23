#!/bin/bash

# PDRBot Daily Automation Script
# This script runs PDRBot daily automation at 9:10 AM

set -o pipefail

# Set working directory to the script location
cd "$(dirname "$0")"

# Set up environment — include ~/.local/bin for claude CLI
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# Load non-secret config from .env (secrets live in Doppler).
if [ -f ".env" ]; then
    set -a
    source .env
    set +a
fi

# Export Doppler-managed credentials for this run.
# Doppler's --format env emits KEY="value" lines; sourcing them through
# bash (with set -a) correctly strips the quotes, which `export "$line"`
# does not.
if command -v doppler >/dev/null 2>&1; then
    DOPPLER_ENV="$(doppler secrets download --project shell-secrets --config dev --no-file --format env 2>/dev/null)"
    if [ -n "$DOPPLER_ENV" ]; then
        set -a
        source <(printf '%s\n' "$DOPPLER_ENV")
        set +a
    else
        echo "WARNING: doppler secrets load returned empty; .env values only"
    fi
fi

# The claude CLI reads these env vars in preference to ~/.claude/.credentials.json.
# The OAuth token file is refreshed by the keep-alive cron and is the authoritative
# source on this host; unset any Doppler-stored overrides so the CLI uses it.
unset CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_API_KEY CLAUDE_API_KEY

# Log file for cron job output
LOG_FILE="data/pdrbot_cron.log"

# Function to log with timestamp
log_message() {
    local message="$(date '+%Y-%m-%d %H:%M:%S') - $1"
    # Output to terminal
    echo "$message"
    # Try to create log file, fallback to local directory if data symlink is broken
    if ! echo "$message" >> "$LOG_FILE" 2>/dev/null; then
        # Fallback to local log file if data directory is not accessible
        LOCAL_LOG="pdrbot_cron.log"
        echo "$message" >> "$LOCAL_LOG"
        # Update log file path for subsequent calls
        LOG_FILE="$LOCAL_LOG"
    fi
}

# Send a failure alert email to mb@ivi3.com
send_failure_alert() {
    local subject="$1"
    local body="$2"
    local to="mb@ivi3.com"

    if [ -z "$EMAIL_FROM" ] || [ -z "$EMAIL_PASSWORD" ]; then
        log_message "WARNING: Cannot send failure alert — email credentials not configured"
        return 1
    fi

    ALERT_SUBJECT="$subject" ALERT_BODY="$body" ALERT_TO="$to" \
    ./.venv/bin/python -c "
import os, smtplib
from email.mime.text import MIMEText
msg = MIMEText(os.environ['ALERT_BODY'])
msg['Subject'] = os.environ['ALERT_SUBJECT']
msg['From'] = os.environ['EMAIL_FROM']
msg['To'] = os.environ['ALERT_TO']
host = os.environ.get('EMAIL_SMTP_HOST', 'smtp.gmail.com')
port = int(os.environ.get('EMAIL_SMTP_PORT', '587'))
user = os.environ.get('EMAIL_AUTH_USER', os.environ['EMAIL_FROM'])
pw = os.environ['EMAIL_PASSWORD']
server = smtplib.SMTP(host, port)
server.starttls()
server.login(user, pw)
server.sendmail(msg['From'], [msg['To']], msg.as_string())
server.quit()
" 2>/dev/null

    if [ $? -eq 0 ]; then
        log_message "Failure alert sent to $to"
    else
        log_message "WARNING: Failed to send alert email"
    fi
}

log_message "Starting PDRBot daily automation"

# Snapshot the SQLite DB before any changes. Failure is non-fatal.
if [ -x "scripts/backup_db.sh" ]; then
    if ./scripts/backup_db.sh data/pdrbot.db 2>&1 | while IFS= read -r line; do log_message "$line"; done; then
        :
    else
        log_message "WARNING: DB backup failed (continuing)"
    fi
fi

# Pre-flight: verify Claude CLI auth before running the full automation.
# The OAuth token expires if no interactive Claude Code session has run
# recently. A quick test call detects this early.
AUTH_FAILED=false
CLAUDE_BIN="$(which claude 2>/dev/null)"
if [ -z "$CLAUDE_BIN" ]; then
    log_message "WARNING: claude CLI not found in PATH"
    AUTH_FAILED=true
else
    # Unset CLAUDECODE var so nested-session check doesn't block us
    unset CLAUDECODE
    AUTH_TEST=$(ANTHROPIC_API_KEY= CLAUDE_API_KEY= "$CLAUDE_BIN" --print "ping" 2>&1)
    AUTH_RC=$?
    if [ $AUTH_RC -ne 0 ] || [ -z "$AUTH_TEST" ]; then
        log_message "ERROR: Claude CLI auth failed (rc=$AUTH_RC). OAuth token may be expired."
        log_message "ERROR: Run an interactive 'claude' session to refresh credentials."
        AUTH_FAILED=true
    else
        log_message "Claude CLI auth verified"
    fi
fi

if [ "$AUTH_FAILED" = true ]; then
    send_failure_alert \
        "PDRBot: Claude CLI auth failed" \
        "PDRBot cannot analyze opinions because the Claude CLI OAuth token has expired.

Run an interactive 'claude' session on the server to refresh credentials.

Time: $(date '+%Y-%m-%d %H:%M:%S')
Host: $(hostname)"
    # Continue anyway — scraping still works, analyses will queue for later.
fi

# Activate virtual environment and run automation
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
    log_message "Virtual environment activated"
else
    log_message "ERROR: Virtual environment not found"
    exit 1
fi

# Run PDRBot automation
# Use pipefail (set above) so $PIPESTATUS[0] / $? captures pdrbot's exit code
# through the tee pipe.
log_message "Running PDRBot automation..."
if [ -w "$(dirname "$LOG_FILE")" ] 2>/dev/null; then
    ./.venv/bin/python pdrbot.py auto 2>&1 | tee -a "$LOG_FILE"
else
    # Fallback to local log if data directory not writable
    LOCAL_LOG="pdrbot_cron.log"
    ./.venv/bin/python pdrbot.py auto 2>&1 | tee -a "$LOCAL_LOG"
    LOG_FILE="$LOCAL_LOG"
fi
EXIT_CODE=${PIPESTATUS[0]}

if [ $EXIT_CODE -eq 0 ]; then
    log_message "PDRBot automation completed successfully"
else
    log_message "PDRBot automation failed with exit code $EXIT_CODE"
    send_failure_alert \
        "PDRBot: daily automation failed (exit $EXIT_CODE)" \
        "PDRBot daily automation exited with code $EXIT_CODE.

Check the log: $(hostname):$(readlink -f "$LOG_FILE")

Last 20 log lines:
$(tail -20 "$LOG_FILE" 2>/dev/null || echo '(could not read log)')

Time: $(date '+%Y-%m-%d %H:%M:%S')
Host: $(hostname)"
fi

# Run Anders Project audit on today's opinions.
# Non-fatal -- failure here does not affect pdrbot exit code.
log_message "Running Anders Project audit..."
if ./.venv/bin/python andersproject.py 2>&1 | tee -a "$LOG_FILE"; then
    log_message "Anders Project audit completed"
else
    log_message "WARNING: Anders Project audit failed (exit $?)"
    send_failure_alert \
        "AndersProject: audit failed" \
        "andersproject.py exited non-zero after pdrbot run. Check: $(hostname):$(readlink -f \"$LOG_FILE\") Time: $(date '+%Y-%m-%d %H:%M:%S')"
fi

log_message "Running tx-criminal-oralarg-calendar build..."
if /home/ubuntu/github/tx-criminal-oralarg-calendar/run.sh 2>&1 | tee -a "$LOG_FILE"; then
    log_message "Calendar build completed"
else
    RC=$?
    log_message "WARNING: Calendar build failed (exit $RC)"
    send_failure_alert \
        "tx-criminal-oralarg-calendar: build failed" \
        "Calendar build exited $RC. See $(hostname):$(readlink -f \"$LOG_FILE\") Time: $(date '+%Y-%m-%d %H:%M:%S')"
fi

log_message "PDRBot daily automation finished"
exit $EXIT_CODE
