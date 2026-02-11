#!/usr/bin/env python3
"""
Real-time Ableton bridge for Aria.

Reads live MIDI from loopMIDI port "ARIA_IN", maintains a rolling prompt window,
and generates continuations via Aria every ~200ms, sending to "ARIA_OUT".

Usage:
    python ableton_bridge.py --in ARIA_IN --out ARIA_OUT [--options]

Options:
    --in PORT_NAME          Input MIDI port (default: ARIA_IN)
    --out PORT_NAME         Output MIDI port (default: ARIA_OUT)
    --checkpoint PATH       Path to Aria checkpoint (default: aria-medium-gen)
    --prompt_seconds        Rolling window (default: 4)
    --tick_seconds          Generation interval (default: 0.2 = 200ms)
    --horizon_seconds       Generation horizon (default: 0.6)
    --temperature           Sampling temperature (default: 0.9)
    --top_p                 Top-p sampling (default: 0.95)
    --device                cuda or cpu (default: cuda)
"""

import argparse
import logging
import math
import os
import sys
import threading
import time
from pathlib import Path

# Logging
logging.basicConfig(
    level=logging.INFO,
    format='[%(levelname)s %(asctime)s] %(name)s: %(message)s',
    datefmt='%H:%M:%S',
)
logger = logging.getLogger(__name__)


def find_checkpoint(checkpoint_hint: str = "aria-medium-gen") -> str:
    """
    Locate the checkpoint file. Try default locations first.
    """
    if os.path.isfile(checkpoint_hint):
        return checkpoint_hint

    # Try in models/ folder
    default_paths = [
        Path("models") / f"{checkpoint_hint}.safetensors",
        Path(__file__).parent.parent / "models" / f"{checkpoint_hint}.safetensors",
    ]

    for p in default_paths:
        if p.exists():
            logger.info(f"Found checkpoint: {p}")
            return str(p)

    raise FileNotFoundError(
        f"Could not find checkpoint '{checkpoint_hint}'. "
        f"Searched: {default_paths}. Provide --checkpoint with full path."
    )


def get_midi_ports():
    """
    List available MIDI ports (input and output).
    """
    try:
        import mido
        logger.info("Available MIDI input ports:")
        for port in mido.get_input_names():
            logger.info(f"  - {port}")
        logger.info("Available MIDI output ports:")
        for port in mido.get_output_names():
            logger.info(f"  - {port}")
    except Exception as e:
        logger.warning(f"Could not list MIDI ports: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="Real-time Aria + Ableton bridge",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--in",
        dest="in_port",
        default="ARIA_IN",
        help="Input MIDI port name (default: ARIA_IN)",
    )
    parser.add_argument(
        "--out",
        dest="out_port",
        default="ARIA_OUT",
        help="Output MIDI port name (default: ARIA_OUT)",
    )
    parser.add_argument(
        "--checkpoint",
        default="aria-medium-gen",
        help="Path to Aria checkpoint (default: aria-medium-gen)",
    )
    parser.add_argument(
        "--listen_seconds",
        type=float,
        default=4.0,
        help="Duration to listen for human input before generating (default: 4.0)",
    )
    parser.add_argument(
        "--gen_seconds",
        type=float,
        default=1.0,
        help="Duration of continuation to generate (default: 1.0)",
    )
    parser.add_argument(
        "--cooldown_seconds",
        type=float,
        default=0.2,
        help="Cooldown after generation before listening again (default: 0.2)",
    )
    parser.add_argument(
        "--clock_in",
        dest="clock_in",
        default="ARIA_CLOCK",
        help="MIDI clock input port name (default: ARIA_CLOCK)",
    )
    parser.add_argument(
        "--measures",
        type=int,
        default=2,
        help="Number of measures per human/model block (default: 2)",
    )
    parser.add_argument(
        "--beats_per_bar",
        type=int,
        default=4,
        help="Beats per bar (time signature numerator, default: 4)",
    )
    parser.add_argument(
        "--gen_measures",
        type=int,
        default=None,
        help="Measures to generate (default: same as --measures)",
    )
    parser.add_argument(
        "--human_measures",
        type=int,
        default=1,
        help="Number of human measures to collect before generating (default: 1)",
    )
    parser.add_argument(
        "--quantize",
        action="store_true",
        help="Quantize generated output to 1/16 note grid (default: off)",
    )
    parser.add_argument(
        "--ticks_per_beat",
        type=int,
        default=480,
        help="MIDI ticks per quarter note (default: 480)",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.9,
        help="Sampling temperature (default: 0.9)",
    )
    parser.add_argument(
        "--top_p",
        type=float,
        default=0.95,
        help="Top-p sampling (default: 0.95)",
    )
    parser.add_argument(
        "--device",
        default="cuda",
        choices=["cuda", "cpu"],
        help="Device for model inference (default: cuda)",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available MIDI ports and exit",
    )

    args = parser.parse_args()

    if args.list_ports:
        get_midi_ports()
        return 0

    # Verify CUDA if needed
    if args.device == "cuda":
        import torch
        if not torch.cuda.is_available():
            logger.error("CUDA requested but not available. Use --device cpu")
            return 1
        logger.info(f"CUDA device: {torch.cuda.get_device_name(0)}")

    # Find checkpoint
    try:
        checkpoint_path = find_checkpoint(args.checkpoint)
    except FileNotFoundError as e:
        logger.error(str(e))
        return 1

    # Import and start bridge
    try:
        # Handle both module and script execution
        try:
            from .midi_buffer import RollingMidiBuffer
            from .aria_engine import AriaEngine
            from .ableton_bridge_engine import AbletonBridge
            from .tempo_tracker import TempoTracker
        except ImportError:
            from midi_buffer import RollingMidiBuffer
            from aria_engine import AriaEngine
            from ableton_bridge_engine import AbletonBridge
            from tempo_tracker import TempoTracker

        logger.info(f"Connecting to ports: IN={args.in_port}, OUT={args.out_port}")
        logger.info(f"Checkpoint: {checkpoint_path}")
        logger.info(
            f"Listen {args.listen_seconds}s → Generate {args.gen_seconds}s → "
            f"Cooldown {args.cooldown_seconds}s"
        )
        if args.clock_in:
            logger.info(f"MIDI Clock input: {args.clock_in}")

        # Create components
        buffer = RollingMidiBuffer(window_seconds=args.listen_seconds)
        engine = AriaEngine(
            checkpoint_path=checkpoint_path,
            device=args.device,
            config_name="medium",
        )
        
        # Start tempo tracker only if NOT using clock_in (they conflict on same MIDI port)
        tempo_tracker = None
        if args.clock_in:
            logger.info(f"Using ClockGrid on '{args.clock_in}'; disabling TempoTracker (port conflict)")
        else:
            # Legacy: use tempo tracker without grid
            if args.clock_in:
                try:
                    tempo_tracker = TempoTracker(clock_port_name=args.clock_in)
                    tempo_tracker.start()
                    logger.info(f"Tempo tracker started on '{args.clock_in}'")
                except Exception as e:
                    logger.warning(f"Failed to start tempo tracker: {e}. Continuing without tempo sync.")
        
        bridge = AbletonBridge(
            in_port_name=args.in_port,
            out_port_name=args.out_port,
            midi_buffer=buffer,
            aria_engine=engine,
            tempo_tracker=tempo_tracker,
            clock_in=args.clock_in,
            measures=args.measures,
            beats_per_bar=args.beats_per_bar,
            gen_measures=args.gen_measures,
            human_measures=args.human_measures,
            cooldown_seconds=args.cooldown_seconds,
            temperature=args.temperature,
            top_p=args.top_p,
            quantize=args.quantize,
            ticks_per_beat=args.ticks_per_beat,
        )

        bridge.run()
        return 0

    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
