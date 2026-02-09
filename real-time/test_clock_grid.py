#!/usr/bin/env python3
"""Quick test of ClockGrid logging."""

import logging
import time

logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s %(asctime)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)

try:
    from clock_grid import ClockGrid
except Exception:
    logger.error("Failed to import ClockGrid")
    exit(1)

logger.info("Starting ClockGrid test on ARIA_CLOCK...")

try:
    grid = ClockGrid(clock_port_name="ARIA_CLOCK", measures=2, beats_per_bar=4)
    grid.start()
    logger.info("ClockGrid started. Waiting 15 seconds for clock pulses...")
    
    for i in range(15):
        time.sleep(1)
        pulse = grid.get_pulse_count()
        running = grid.get_is_running()
        logger.info(f"Status check #{i+1}: pulse={pulse}, running={running}")
    
    grid.stop()
    logger.info("ClockGrid stopped.")

except Exception as e:
    logger.exception(f"ClockGrid test error: {e}")
