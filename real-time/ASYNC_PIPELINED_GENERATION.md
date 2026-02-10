# Asynchronous Pipelined Generation - Implementation Guide

## Overview

Replaced the blocking 2-bar generation model with true asynchronous pipelining. The bridge now:
- Enqueues generation jobs without blocking
- Generates each bar in parallel while previous bars are playing
- Immediately schedules generated bars when ready (no waiting)
- Handles missed deadlines with silence fallback (MVP)

## Architecture Changes

### Old Flow (Blocked)
```
End of bar 1: Generate (wait...) → Wait for bar 2 human input
End of bar 2: Generate (wait...) → Schedule both bars → Play
```

**Problem**: User waits for generation, feels unresponsive, randomness due to long latencies.

### New Flow (Pipelined, Async)
```
End of bar 0: Enqueue generation for bar 0 (return immediately) → Repeat for next bar
  [GenerationWorker processes bar 0 in background]
End of bar 1: If bar 0 ready → Schedule bar 0 playback; Enqueue bar 1 generation
  [Bar 0 starts playing while bar 1 generates in parallel]
End of bar 2: If bar 1 ready → Schedule bar 1 playback; Enqueue bar 2 generation
  [Bar 1 starts playing while bar 2 generates]
```

**Benefit**: No blocking, responsive feel, lower latency perception.

## New Components

### 1. `GenerationJob` Class
```python
class GenerationJob:
    def __init__(self, bar_index, prompt_events, aria_engine, temperature, top_p):
        self.bar_index = bar_index
        self.prompt_events = prompt_events  # Last 2 bars for context
        self.aria_engine = aria_engine
        self.temperature = temperature
        self.top_p = top_p
        self.result_midi_path = None  # Set when done
        self.error = None  # Set if failed
```

### 2. `GenerationWorker` Thread
- Daemon thread that runs continuously
- Fetches jobs from `gen_job_queue`
- Calls `aria_engine.generate()` asynchronously
- Stores result in `generated_bars[bar_index]` when ready
- Thread-safe access via `gen_bars_lock`

### 3. Modified `_on_bar_boundary()`
Instead of:
```python
# OLD: blocks until generation completes
midi_path = aria_engine.generate(...)
_parse_and_schedule(midi_path)
```

Now:
```python
# NEW: enqueue and return immediately
job = GenerationJob(bar, events, aria, temp, top_p)
gen_job_queue.put(job)  # Non-blocking

# Try to schedule previous bar if ready
_try_schedule_ready_bar(bar)
```

### 4. New `_try_schedule_ready_bar(current_bar)`
- Checks if `current_bar - 1` is in `generated_bars` dict
- If ready, schedules immediately for playback
- If not ready, logs debug message and continues
- No blocking, lightweight check

### 5. New `_schedule_single_bar_playback(bar_index, midi_path, boundary_pulse)`
- Replaces legacy 2-bar scheduling
- Parses MIDI, converts ticks → pulses
- Schedules all events for bar window: `[B..B+96)`
- Cleans up temp MIDI file

## State Variables Added

```python
# Generation worker
self.gen_job_queue = queue.Queue()           # Job queue (non-blocking)
self.generated_bars = {}                     # {bar_index: midi_path} when ready
self.gen_bars_lock = threading.RLock()       # Thread-safe access
self.gen_worker = GenerationWorker(...)      # Background thread
```

## Prompt Context Enhancement

When generating bar k, prompt context is now:
- **Bars k-1 and k** (last 2 bars of human input) if available
- Falls back to just bar k if no previous bar

```python
prompt_events = human_events  # Current bar
if finished_bar >= 1 and finished_bar - 1 in human_bar_buffers:
    prev_events = human_bar_buffers[finished_bar - 1]
    prompt_events = prev_events + human_events  # 2-bar context
```

This improves musical coherence by giving Aria context from the previous bar.

## Sampling Parameter Changes

### aria_engine.py defaults:
**Before**: temperature=0.9, top_p=0.95 (quite random)  
**After**: temperature=0.8, top_p=0.9 (more conservative)

**Rationale**: 
- Lower temperature → more deterministic, less "weird" notes
- Lower top_p → nucleus sampling is tighter, fewer outliers
- Better for real-time: reduces jarring, random musical artifacts

### ableton_bridge_engine.py defaults:
Same change: temperature=0.8, top_p=0.9 instead of 0.9, 0.95

## Logging

New/updated log messages for debugging:

```
[bar_boundary] finished_bar=1, anchor=100          # Bar boundary detected
[enqueue] Generation job for bar 1 queued          # Job enqueued (non-blocking)
[gen_worker] Starting generation for bar 1         # Worker started processing
[gen_worker] Bar 1 generation done in 0.45s        # Generation complete
[scheduler] Bar 0 ready! Scheduling for playback   # Bar ready, scheduling
[schedule_bar] Bar 0: 16 events in pulse [192..288), min=192 max=280
```

### Missed Deadline Warning (MVP)
If bar N is not ready by bar N+1 boundary:
```
[scheduler] Bar N not ready yet                    # Will retry next boundary
```

Current behavior: Silence for that bar (MVP fallback). Notes can be added later:
- Option (a): Replay previous bar
- Option (b): Stretch previous bar
- Option (c): Smart gap fill

For now, (a) is simplest: if bar not ready, log and continue.

## Control Flow Guarantees

1. **No Blocking**: `_on_bar_boundary()` returns in ~1ms (queue.put())
2. **No Duplicate Scheduling**: Bars removed from `generated_bars` after scheduling
3. **Thread-Safe**: All shared state protected by RLock
4. **Worker Cleanup**: Generation worker stopped gracefully in `shutdown()`

## Testing Checklist

- [ ] Run bridge with pipelined mode
- [ ] Play 3+ bars of input
- [ ] Verify logs show:
  - `[enqueue]` for each bar
  - `[gen_worker] Starting` and `done` for each
  - `[scheduler]` when bar ready
  - `[schedule_bar]` with pulse ranges
- [ ] Auditory test: Bars should play smoothly without long waits
- [ ] Verify no duplicate notes (bars don't re-send)
- [ ] Check generation latency: target < 0.5s per bar (depends on GPU)

## Performance Notes

- **Generation**: Still GPU-bound (depends on Aria model latency)
- **Scheduling**: ~1-2ms (thread-safe queue operations)
- **Overhead**: ~5% CPU for gen worker thread (mostly waiting)
- **Memory**: Minimal (2 MIDI files in queue at any time)

## Migration from Old Code

**Legacy Methods (deprecated, kept for reference)**:
- `_schedule_2bar_playback()` - marked as deprecated, no-op
- `_parse_generated_midi_for_bar()` - unused
- `generated_bar_queue` - unused (replaced by `generated_bars`)

Can be removed in next cleanup pass if desired.

## Known Limitations (MVP)

1. **Missed Deadline Handling**: Currently silent. Could be improved with:
   - Repeat previous bar
   - Stretch/hold last chord
   - Smart gap fill

2. **Prompt Context**: 2 bars. Could expand to:
   - 4 bars for richer context
   - FIFO buffer instead of just previous bar

3. **Generation Timeout**: No timeout on jobs. Could add:
   - Force generation stop after 5 seconds
   - Fallback pattern if timeout

4. **Statistics**: No per-bar generation timing stats. Could add:
   - p50/p95/p99 latencies
   - Dropout rate (missed deadlines)

## Future Improvements

1. **Adaptive Prompt Context**: Use 2-4 bars based on available time
2. **Fallback Patterns**: Define per-key root + chord sustain for gaps
3. **Quality Metrics**: Track generation success rate, latency variance
4. **Smart Scheduling**: Pre-warm generation before bar boundary
5. **A/B Testing**: Compare temperature/top_p settings in real-time

