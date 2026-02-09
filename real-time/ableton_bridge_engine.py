"""Core orchestration for real-time Ableton-Aria bridge."""

import logging
import os
import queue
import threading
import time
import tempfile
from typing import Optional
logger = logging.getLogger(__name__)


class AbletonBridge:
    """
    Orchestrates real-time MIDI I/O and Aria generation.

    Flow:
    1. Input thread reads MIDI from loopMIDI port and adds to rolling buffer
    2. Generation thread runs every N ms:
       - Snapshot rolling buffer
       - Convert to MIDI file
       - Run Aria inference
       - Queue output events
    3. Output thread sends queued MIDI events to loopMIDI port

    No human->human feedback by default: only ingests human input, not generated notes.
    """

    def __init__(
        self,
        in_port_name: str,
        out_port_name: str,
        midi_buffer,
        aria_engine,
        tempo_tracker=None,
        # Grid / clock parameters
        clock_in: Optional[str] = None,
        measures: int = 2,
        beats_per_bar: int = 4,
        gen_measures: Optional[int] = None,
        cooldown_seconds: float = 0.2,
        temperature: float = 0.9,
        top_p: float = 0.95,
        quantize: bool = False,
        ticks_per_beat: int = 480,
    ):
        """
        Args:
            in_port_name: Input MIDI port (e.g., "ARIA_IN")
            out_port_name: Output MIDI port (e.g., "ARIA_OUT")
            midi_buffer: RollingMidiBuffer instance
            aria_engine: AriaEngine instance
            tempo_tracker: TempoTracker instance (optional)
            listen_seconds: Duration to listen before generating (e.g., 4.0)
            gen_seconds: Duration of continuation to generate (e.g., 1.0)
            cooldown_seconds: Cooldown after generation before listening again (e.g., 0.2)
            temperature: Sampling temperature
            top_p: Top-p sampling
            quantize: Whether to quantize output to 1/16 grid
            ticks_per_beat: MIDI ticks per quarter note
        """
        self.in_port_name = in_port_name
        self.out_port_name = out_port_name
        self.midi_buffer = midi_buffer
        self.aria_engine = aria_engine
        self.tempo_tracker = tempo_tracker
        # Grid/clock
        self.clock_in = clock_in
        self.measures = measures
        self.beats_per_bar = beats_per_bar
        self.gen_measures = gen_measures if gen_measures is not None else measures

        self.cooldown_seconds = cooldown_seconds
        self.temperature = temperature
        self.top_p = top_p
        self.quantize = quantize
        self.ticks_per_beat = ticks_per_beat

        # MIDI I/O
        self.in_port = None
        self.out_port = None

        # Queue of (msg_type, msg_data)
        self.event_queue = queue.Queue()

        # Control
        self.running = False
        self.threads = []

        # PHASE state machine
        self.PHASE_HUMAN = 'human'
        self.PHASE_MODEL = 'model'
        self.phase = self.PHASE_HUMAN

        # ClockGrid will be set if clock_in provided
        self.clock_grid = None

        self.listen_start_time = None
        self.cooldown_end_time = None

        # Stats
        self.generation_count = 0
        self.skip_count = 0
        self.generation_times = []

        # Scheduler for model output: list of (target_pulse, mido.Message)
        self.scheduled_messages = []
        self.scheduled_lock = threading.RLock()
        self.model_end_pulse = None
        self.last_boundary_pulse = None

        # Anchor-based boundary tracking (counts from first human note, not transport start)
        self.anchor_pulse = None
        self.bar_index = 0
        self.next_bar_boundary_pulse = None

        # Per-bar buffering: bar_index -> list of (pulse, event_type, msg_data)
        self.human_bar_buffers = {}  # dict[int, list[TimestampedMidiMsg]]
        self.generated_bar_queue = {}  # dict[int, list[mido.Message]]
        self.last_scheduled_bar = None  # Highest bar index we've scheduled for playback

        # Failsafe: force generation after 6 seconds of no generation
        self.last_generation_time = time.time()
        self.failsafe_forced = False

    def run(self):
        """Start the bridge: input, generation, output threads."""
        try:
            self._setup_midi_ports()
            # Start clock grid if requested
            if self.clock_in:
                try:
                    from .clock_grid import ClockGrid
                except Exception:
                    from clock_grid import ClockGrid

                self.clock_grid = ClockGrid(clock_port_name=self.clock_in, measures=self.measures, beats_per_bar=self.beats_per_bar)
                # Do NOT register boundary callback; we use anchor-based boundary detection in _generation_loop
                try:
                    self.clock_grid.start()
                    logger.info(f"ClockGrid started on '{self.clock_in}' (measures={self.measures})")
                except Exception as e:
                    logger.warning(f"Failed to start ClockGrid: {e}")
            self.running = True

            # Start threads
            t_input = threading.Thread(target=self._input_loop, daemon=True)
            t_gen = threading.Thread(target=self._generation_loop, daemon=True)
            t_output = threading.Thread(target=self._output_loop, daemon=True)

            for t in [t_input, t_gen, t_output]:
                t.start()
                self.threads.append(t)

            logger.info("Bridge started. Press Ctrl+C to stop.")

            # Keep main thread alive
            while self.running:
                time.sleep(1)

        except KeyboardInterrupt:
            logger.info("Interrupt received, shutting down...")
            self.shutdown()
        except Exception as e:
            logger.exception(f"Bridge error: {e}")
            self.shutdown()

    def shutdown(self):
        """Stop all threads and close ports."""
        self.running = False

        for t in self.threads:
            t.join(timeout=2)

        if self.tempo_tracker:
            self.tempo_tracker.stop()

        if self.in_port:
            self.in_port.close()
            logger.info("Input port closed")

        if self.out_port:
            self.out_port.close()
            logger.info("Output port closed")

        logger.info(
            f"Bridge shutdown. Stats: {self.generation_count} generations, "
            f"{self.skip_count} skips"
        )

    def _setup_midi_ports(self):
        """Open MIDI input and output ports."""
        try:
            import mido
        except ImportError:
            raise ImportError("mido is required. Install with: pip install mido")

        # Input port
        try:
            # Try exact name first, then try with port number suffix
            in_port_name = self.in_port_name
            try:
                self.in_port = mido.open_input(in_port_name)
            except (OSError, ValueError):
                # Port not found, try matching with available ports
                available = mido.get_input_names()
                matched = [p for p in available if p.startswith(in_port_name)]
                if matched:
                    in_port_name = matched[0]
                    self.in_port = mido.open_input(in_port_name)
                else:
                    raise

            logger.info(f"Input port opened: {in_port_name}")
        except Exception as e:
            logger.error(f"Failed to open input port '{self.in_port_name}': {e}")
            logger.info("Listing available input ports: " + ", ".join(mido.get_input_names()))
            raise

        # Output port
        try:
            # Try exact name first, then try with port number suffix
            out_port_name = self.out_port_name
            try:
                self.out_port = mido.open_output(out_port_name)
            except (OSError, ValueError):
                # Port not found, try matching with available ports
                available = mido.get_output_names()
                matched = [p for p in available if p.startswith(out_port_name)]
                if matched:
                    out_port_name = matched[0]
                    self.out_port = mido.open_output(out_port_name)
                else:
                    raise

            logger.info(f"Output port opened: {out_port_name}")
        except Exception as e:
            logger.error(f"Failed to open output port '{self.out_port_name}': {e}")
            logger.info("Listing available output ports: " + ", ".join(mido.get_output_names()))
            raise

    def _input_loop(self):
        """Read live MIDI input and add to rolling buffer."""
        logger.info("Input thread started")
        try:
            while self.running:
                # Poll for messages (non-blocking)
                for msg in self.in_port.iter_pending():
                    # Tag messages with current pulse if clock available
                    pulse = None
                    if self.clock_grid:
                        try:
                            pulse = self.clock_grid.get_pulse_count()
                        except Exception:
                            pulse = None

                    if msg.type == 'note_on':
                        # Set anchor on first human note if clock is running
                        if self.anchor_pulse is None and self.clock_grid and self.clock_grid.get_is_running():
                            self.anchor_pulse = pulse
                            pulses_per_bar = self.clock_grid.get_pulses_per_bar()
                            self.next_bar_boundary_pulse = self.anchor_pulse + pulses_per_bar
                            self.bar_index = 0
                            logger.info(f"[anchor] set at pulse={self.anchor_pulse}, pulses_per_bar={pulses_per_bar}")

                        # Assign to bar buffer
                        if self.anchor_pulse is not None and pulse is not None:
                            pulses_per_bar = self.clock_grid.get_pulses_per_bar()
                            bar = (pulse - self.anchor_pulse) // pulses_per_bar
                            if bar not in self.human_bar_buffers:
                                self.human_bar_buffers[bar] = []
                        else:
                            bar = None

                        msg_obj = type('MidiMsg', (), {'pulse': pulse, 'msg_type': 'note_on', 'note': msg.note, 'velocity': msg.velocity})()
                        self.midi_buffer.add_message('note_on', note=msg.note, velocity=msg.velocity, pulse=pulse)
                        if bar is not None:
                            self.human_bar_buffers[bar].append(msg_obj)
                        logger.info(f"[HUMAN] bar={bar} note_on pitch={msg.note} vel={msg.velocity} pulse={pulse}")

                    elif msg.type == 'note_off':
                        self.midi_buffer.add_message(
                            'note_off',
                            note=msg.note,
                            velocity=msg.velocity,
                            pulse=pulse,
                        )
                        logger.debug(f"[HUMAN] note_off pitch={msg.note} pulse={pulse}")

                    elif msg.type == 'control_change' and msg.control == 64:
                        self.midi_buffer.add_message(
                            'control_change',
                            control=64,
                            value=msg.value,
                            pulse=pulse,
                        )
                        logger.debug(f"[HUMAN] sustain {msg.value} pulse={pulse}")

                time.sleep(0.001)  # Small sleep to avoid busy loop

        except Exception as e:
            logger.exception(f"Input loop error: {e}")

    def _generation_loop(self):
        """Monitor bar boundaries and generate per-bar, scheduling playback after 2 bars."""
        logger.info("Generation thread started (per-bar pipeline)")
        try:
            last_failsafe_check = time.time()
            while self.running:
                # Bar-based boundary detection
                if self.clock_grid and self.next_bar_boundary_pulse is not None:
                    current_pulse = self.clock_grid.get_pulse_count()
                    if current_pulse >= self.next_bar_boundary_pulse:
                        finished_bar = self.bar_index
                        self._on_bar_boundary(finished_bar)
                        # Update for next bar
                        pulses_per_bar = self.clock_grid.get_pulses_per_bar()
                        self.bar_index += 1
                        self.next_bar_boundary_pulse += pulses_per_bar

                # Failsafe: force generation after 6 seconds if anchor is set but no generation occurred
                now = time.time()
                if (now - last_failsafe_check > 6.0) and self.anchor_pulse is not None:
                    all_msgs = self.midi_buffer.get_messages()
                    has_notes = any(m.msg_type == 'note_on' and m.velocity > 0 for m in all_msgs)
                    if has_notes and not self.failsafe_forced:
                        logger.warning(f"[FAILSAFE] No generation in 6s despite human input. Forcing generation.")
                        self.failsafe_forced = True
                    last_failsafe_check = now

                time.sleep(0.01)  # Check 100 times per second
        except Exception as e:
            logger.exception(f"Generation loop error: {e}")

    def _has_human_activity(self) -> bool:
        """Check if there's any human activity (note_on or CC changes) in the buffer."""
        messages = self.midi_buffer.get_messages()
        for msg in messages:
            # Human activity: note_on with vel>0 or sustain pedal
            if (msg.msg_type == 'note_on' and msg.velocity and msg.velocity > 0) or \
               (msg.msg_type == 'control_change' and msg.control == 64):
                return True
        return False

    def _trigger_generation(self):
        """Snapshot buffer and queue generation."""
        try:
            # legacy: time-based trigger not used when ClockGrid is active
            logger.debug("_trigger_generation called (legacy/time-based). Ignored when using ClockGrid.")

        except Exception as e:
            logger.exception(f"Generation trigger error (will skip): {e}")
            self.skip_count += 1

    def _output_loop(self):
        """Send queued MIDI files with precise timing using mido.play()."""
        logger.info("Output thread started")
        try:
            import mido

            while self.running:
                try:
                    try:
                        msg_type, msg_data = self.event_queue.get(timeout=0.05)
                    except queue.Empty:
                        msg_type = None

                    if msg_type == 'midi_file':
                        midi_path = msg_data
                        # When a midi_file event arrives from legacy path, play immediately
                        self._play_midi_file_with_timing(midi_path)

                    # Also service scheduled model messages (pulse-scheduled)
                    self._service_scheduled_messages()

                except queue.Empty:
                    pass
                except Exception as e:
                    logger.exception(f"Output error: {e}")

        except Exception as e:
            logger.exception(f"Output loop error: {e}")

    def _service_scheduled_messages(self):
        """Send any scheduled model messages whose target_pulse <= current pulse."""
        if not self.clock_grid:
            return

        current_pulse = self.clock_grid.get_pulse_count()

        to_send = []
        with self.scheduled_lock:
            remaining = []
            for target_pulse, msg in self.scheduled_messages:
                if current_pulse >= target_pulse:
                    to_send.append((target_pulse, msg))
                else:
                    remaining.append((target_pulse, msg))
            self.scheduled_messages = remaining

        # Send messages due
        for tp, msg in to_send:
            try:
                self.out_port.send(msg)
                logger.debug(f"OUT scheduled: {msg.type} target_pulse={tp} now={current_pulse}")
            except Exception:
                logger.exception("Failed to send scheduled message")

        # If model end pulse reached, switch back to HUMAN
        if self.model_end_pulse is not None and current_pulse >= self.model_end_pulse:
            if self.phase == self.PHASE_MODEL:
                logger.info(f"MODEL -> HUMAN after playback (pulse={current_pulse})")
            self.phase = self.PHASE_HUMAN
            self.model_end_pulse = None

    # _on_anchor_boundary deprecated: use _on_bar_boundary instead (pipelined mode)

    def _on_bar_boundary(self, finished_bar: int):
        """Handle end-of-bar boundary. Generate 1 bar for this bar index."""
        try:
            logger.info(f"[bar_boundary] finished_bar={finished_bar}, anchor={self.anchor_pulse}")

            # Extract human events for this bar
            if finished_bar not in self.human_bar_buffers:
                logger.info(f"[bar_boundary] No human events for bar {finished_bar}")
                return

            human_events = self.human_bar_buffers[finished_bar]
            if not human_events:
                logger.info(f"[bar_boundary] Empty buffer for bar {finished_bar}")
                return

            logger.info(f"[bar_boundary] Bar {finished_bar}: {len(human_events)} human events")

            # Build prompt MIDI for this single bar
            try:
                from .prompt_midi import buffer_to_tempfile_midi
            except Exception:
                from prompt_midi import buffer_to_tempfile_midi

            prompt_midi_path = buffer_to_tempfile_midi(
                human_events,
                window_seconds=0,
                current_bpm=(self.tempo_tracker.get_bpm() if self.tempo_tracker else None),
                ticks_per_beat=self.ticks_per_beat,
            )

            # Estimate horizon for 1 bar
            if self.tempo_tracker:
                bpm = self.tempo_tracker.get_bpm()
                sec_per_beat = 60.0 / max(1.0, bpm)
            else:
                sec_per_beat = 0.5

            horizon_s = 1 * self.beats_per_bar * sec_per_beat

            logger.info(f"[generate] bar={finished_bar}, horizon_s={horizon_s:.2f}s")

            start_time = time.time()
            generated_midi_path = self.aria_engine.generate(
                prompt_midi_path=prompt_midi_path,
                prompt_duration_s=0,
                horizon_s=horizon_s,
                temperature=self.temperature,
                top_p=self.top_p,
            )
            gen_time = time.time() - start_time
            self.generation_times.append(gen_time)

            if generated_midi_path is None:
                logger.warning(f"[bar_boundary] Generation returned None for bar {finished_bar}")
                try:
                    os.unlink(prompt_midi_path)
                except Exception:
                    pass
                return

            # Parse generated MIDI and store
            self._parse_generated_midi_for_bar(finished_bar, generated_midi_path)
            self.last_generation_time = time.time()
            self.generation_count += 1

            # Check if we can schedule playback (when finished_bar is odd, i.e., 1, 3, 5,...)
            if finished_bar % 2 == 1 and (finished_bar - 1) in self.generated_bar_queue and finished_bar in self.generated_bar_queue:
                # Both previous and current bar are generated
                previous_bar = finished_bar - 1
                self._schedule_2bar_playback(previous_bar, finished_bar)
                self.last_scheduled_bar = finished_bar

            # Cleanup prompt MIDI
            try:
                os.unlink(prompt_midi_path)
            except Exception:
                pass

        except Exception as e:
            logger.exception(f"Error on bar boundary: {e}")

    def _on_block_boundary(self, boundary_pulse: int):
        """Legacy handler - not used in pipelined mode. Kept for compatibility."""
        logger.debug(f"[_on_block_boundary] called but pipelined mode uses _on_bar_boundary")
        pass

    def _schedule_generated_midi(self, midi_path: str, boundary_pulse: int):
        """Convert generated MIDI file into pulse-scheduled messages and enqueue them.
        boundary_pulse is the pulse index at which the model should start playing (i.e., immediate next pulse).
        """
        try:
            import mido

            mid = mido.MidiFile(midi_path)
            tpq = mid.ticks_per_beat if mid.ticks_per_beat else self.ticks_per_beat

            abs_tick = 0
            messages = []
            for track in mid.tracks:
                abs_tick = 0
                for msg in track:
                    abs_tick += msg.time
                    if not hasattr(msg, 'type'):
                        continue
                    if msg.type in ('note_on', 'note_off', 'control_change'):
                        # Convert tick -> pulse: pulse_delta = (tick / ticks_per_beat) * 24
                        pulse_delta = int((abs_tick / float(tpq)) * 24.0)
                        target_pulse = boundary_pulse + pulse_delta
                        messages.append((target_pulse, msg.copy()))

            # Merge into scheduled_messages list (thread-safe)
            with self.scheduled_lock:
                self.scheduled_messages.extend(messages)

            # Set model end pulse
            pulses_per_block = self.clock_grid.get_pulses_per_block()
            self.model_end_pulse = boundary_pulse + pulses_per_block

            # Cleanup generated midi file
            try:
                os.unlink(midi_path)
            except Exception:
                pass

            logger.info(f"[schedule] Scheduled {len(messages)} generated events starting at pulse={boundary_pulse}")

        except Exception as e:
            logger.exception(f"Failed to schedule generated MIDI: {e}")

    def _parse_generated_midi_for_bar(self, bar_index: int, midi_path: str):
        """Parse generated MIDI and store for bar playback."""
        try:
            import mido
            mid = mido.MidiFile(midi_path)
            messages = []
            for track in mid.tracks:
                for msg in track:
                    if hasattr(msg, 'type') and msg.type in ('note_on', 'note_off', 'control_change'):
                        messages.append(msg.copy())
            self.generated_bar_queue[bar_index] = messages
            logger.info(f"[parse] Bar {bar_index}: {len(messages)} messages stored")
        except Exception as e:
            logger.exception(f"Failed to parse generated MIDI for bar {bar_index}: {e}")

    def _schedule_2bar_playback(self, bar1: int, bar2: int):
        """Schedule playback of 2 consecutive bars aligned to pulse grid."""
        try:
            if bar1 not in self.generated_bar_queue or bar2 not in self.generated_bar_queue:
                logger.error(f"[schedule_2bar] Bar {bar1} or {bar2} not in queue")
                return

            # Compute start pulse for playback (at the boundary of bar2)
            pulses_per_bar = self.clock_grid.get_pulses_per_bar()
            start_pulse = self.anchor_pulse + (bar2 + 1) * pulses_per_bar

            # Convert bar MIDI messages to pulse-absolute timings
            messages = []
            bar1_msgs = self.generated_bar_queue[bar1]
            bar2_msgs = self.generated_bar_queue[bar2]

            # Bar 1 messages: in range [start_pulse, start_pulse + pulses_per_bar)
            tpq = self.ticks_per_beat
            abs_tick = 0
            for msg in bar1_msgs:
                abs_tick += msg.time
                pulse_delta = int((abs_tick / float(tpq)) * 24.0)
                target_pulse = start_pulse + pulse_delta
                messages.append((target_pulse, msg.copy()))

            # Bar 2 messages: in range [start_pulse + pulses_per_bar, start_pulse + 2*pulses_per_bar)
            abs_tick = 0
            for msg in bar2_msgs:
                abs_tick += msg.time
                pulse_delta = int((abs_tick / float(tpq)) * 24.0)
                target_pulse = start_pulse + pulses_per_bar + pulse_delta
                messages.append((target_pulse, msg.copy()))

            # Queue for output
            with self.scheduled_lock:
                self.scheduled_messages.extend(messages)

            logger.info(f"[schedule_2bar] Bars {bar1}-{bar2}: {len(messages)} events scheduled in pulse [{start_pulse}, {start_pulse + 2*pulses_per_bar})")

        except Exception as e:
            logger.exception(f"Failed to schedule 2-bar playback: {e}")
    
    def _play_midi_file_with_timing(self, midi_path: str):
        """Load and play a MIDI file with proper timing to output port."""
        try:
            import mido
            
            mid = mido.MidiFile(midi_path)
            total_time = mid.length
            msg_count = 0
            
            # Use mid.play() to iterate through messages with absolute timing
            for msg in mid.play():
                if not self.running:
                    break
                    
                # Filter out meta messages, only send channel messages
                if msg.type in ('note_on', 'note_off', 'control_change'):
                    # Sleep for the message's elapsed time
                    msg_time = msg.time
                    
                    # Optionally quantize to 1/16 grid
                    if self.quantize and self.tempo_tracker:
                        bpm = self.tempo_tracker.get_bpm()
                        # Sixteenth note duration in seconds
                        sixteenth_dur = (60.0 / bpm) / 4.0
                        # Quantize to nearest sixteenth
                        quantized_time = round(msg_time / sixteenth_dur) * sixteenth_dur
                        msg_time = max(0, quantized_time)
                    
                    if msg_time > 0:
                        time.sleep(msg_time)
                    
                    self.out_port.send(msg)
                    msg_count += 1
                    logger.debug(f"OUT: {msg.type} (t={msg.time:.3f}s)")
            
            logger.info(f"Sent {msg_count} MIDI messages in {total_time:.2f}s")
            
        except Exception as e:
            logger.error(f"Failed to play MIDI file {midi_path}: {e}")
        finally:
            # Cleanup temp file after playback
            try:
                os.unlink(midi_path)
            except:
                pass
