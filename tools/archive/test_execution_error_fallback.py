#!/usr/bin/env python3
"""
Test that execution error detection triggers SDK fallback
"""
import sys
sys.path.insert(0, '/home/mb/github/mwb_common')

from mwb_claude import call_claude_with_retry, ClaudeError
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def test_execution_error_fallback():
    """Test that 'Execution error' responses trigger SDK fallback"""

    # Simple test prompt
    test_prompt = "What is 2+2? Just give me the number."

    try:
        logger.info("Testing call_claude_with_retry with simple prompt...")
        response = call_claude_with_retry(
            prompt=test_prompt,
            timeout=60,
            max_retries=1
        )

        logger.info(f"Response received: {response[:100]}...")

        # Check if we got a valid response
        if response.lower() == "execution error":
            logger.error("Still getting execution error - SDK fallback may have failed")
            return False
        else:
            logger.info("SUCCESS: Got valid response (not 'Execution error')")
            return True

    except ClaudeError as e:
        logger.error(f"Claude call failed completely: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        return False

if __name__ == "__main__":
    success = test_execution_error_fallback()
    sys.exit(0 if success else 1)
