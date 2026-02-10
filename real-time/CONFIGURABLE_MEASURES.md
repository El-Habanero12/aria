# Configurable AI Measures: `--measures N`

## Overview

The Ableton real-time Aria bridge now supports configurable AI response length. Instead of always generating 2 measures of output, you can now specify any number of AI measures per cycle.

## Usage

```bash
# Generate 4 AI measures for each 1 human measure
python ableton_bridge.py \
  --in ARIA_IN \
  --out ARIA_OUT \
  --clock_in ARIA_CLOCK \
  --checkpoint "/path/to/aria-medium-base.safetensors" \
  --measures 4

# Generate 3 AI measures (default is 2)
python ableton_bridge.py --measures 3 ...

# Default (2 AI measures per 1 human measure)
python ableton_bridge.py ...
```

## CLI Options

```
--measures N
  Default: 2
  Type: int
  Description: Number of AI measures to generate for each 1 human measure played.
  
  Examples:
    --measures 1   → User plays 1 bar, AI plays 1 bar, repeat
    --measures 2   → User plays 1 bar, AI plays 2 bars, repeat (default MVP)
    --measures 3   → User plays 1 bar, AI plays 3 bars, repeat
    --measures 4   → User plays 1 bar, AI plays 4 bars, repeat
```

## Implementation

### Core Changes

#### 1. **ableton_bridge.py** (CLI handling)
- Already had `--measures` and `--gen_measures` CLI arguments defined
- Passes `gen_measures=args.gen_measures` to `AbletonBridge`

#### 2. **ableton_bridge_engine.py** (Main engine)

**GenerationJob class**:
```python
class GenerationJob:
    def __init__(self, ..., gen_bars: int = 2):
        self.gen_bars = gen_bars  # Number of measures to generate
        self.result_midi_path = None
```

**AbletonBridge initialization**:
```python
def __init__(self, ..., gen_measures: Optional[int] = None, ...):
    self.gen_measures = gen_measures if gen_measures is not None else 2
```

**Generation triggering** (`_on_bar_boundary`):
```python
job = GenerationJob(
    bar_index=finished_bar,
    prompt_events=prompt_events,
    aria_engine=self.aria_engine,
    temperature=self.temperature,
    top_p=self.top_p,
    gen_bars=self.gen_measures,  # Use configurable measure count
)
self.gen_job_queue.put(job)
logger.info(f"[enqueue] {self.gen_measures}-measure generation job for bar {finished_bar} queued")
```

**Scheduling** (`_schedule_two_bar_response`):
```python
# Enforce strict N-measure limit
max_offset_pulses = self.gen_measures * pulses_per_bar

# Keep only events within [0, N*pulses_per_bar)
if offset_pulses >= max_offset_pulses:
    continue  # Discard event

# Force note-offs at the end of N measures
end_pulse = boundary_pulse + max_offset_pulses
for pitch in active_notes:
    messages.append((end_pulse, note_off_message))
```

### Timing Math

For any time signature (beats_per_bar is configurable):

```
pulses_per_bar = beats_per_bar * 24  (MIDI clock ppqn)

For --measures N:
  ai_total_pulses = N * pulses_per_bar
  
  Example (4/4 time, default 24 ppqn):
    beats_per_bar = 4
    pulses_per_bar = 4 * 24 = 96
    
    --measures 1 → 96 pulses (1 bar)
    --measures 2 → 192 pulses (2 bars)
    --measures 4 → 384 pulses (4 bars)
```

### State Machine (Unchanged)

```
┌─────────────────────────────────────────────────────────────┐
│ PHASE_HUMAN (Collecting human input for 1 bar)              │
│                                                              │
│  User plays → events recorded in human_bar_buffers[N]       │
│  At bar boundary:                                           │
│    - Enqueue GenerationJob(gen_bars=self.gen_measures)      │
│    - Job goes to background worker thread                   │
│                                                              │
│  While job generates:                                       │
│    - _check_and_schedule_ai_response() polls job result     │
│    - When ready: schedule and switch to PHASE_AI_PLAY       │
└──────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────┐
│ PHASE_AI_PLAY (Playing AI response for N measures)           │
│                                                              │
│  Playback pulses [B, B + N*pulses_per_bar)                  │
│  All events outside this range are discarded                │
│  Forced note-offs + CC123 at B + N*pulses_per_bar           │
│                                                              │
│  At playback end:                                           │
│    - Clear scheduled_messages                               │
│    - Clear human_bar_buffers                                │
│    - Return to PHASE_HUMAN                                  │
└──────────────────────────────────────────────────────────────┘
```

## Logging

When `--measures 4` is used:

```
[generation_loop] Generation thread started (MVP 1-bar-in -> 4-measures-out)

[bar_boundary] Bar 0: 8 prompt events, triggering 4-measure generation

[enqueue] 4-measure generation job for bar 0 queued

[gen_worker] Starting generation for bar 0 (4 bars)
[gen_worker] Bar 0 (4-bar generation) done in 1.23s

[ai_ready] 4-measure response ready for job at bar 0, scheduling playback

[schedule_2bar] 4-measure response: 64 events in pulse [96..480), min=96 max=472

[phase] HUMAN -> AI_PLAY at pulse=96

# ... output plays events from pulse 96 to 479 ...

[service] Cleared human_bar_buffers for next cycle
[phase] AI_PLAY -> HUMAN at pulse=480, playback finished, queue_size=0
```

## Prompt Context

The prompt context (input to Aria) is **always 2 bars** regardless of output measures:
- If available: [prev_bar_events + current_bar_events]
- If not: [current_bar_events]

This is independent of `--measures`. The prompt provides 2 bars of context for consistency, while the output length is configurable.

**Why?** A fixed prompt window simplifies the input to Aria and makes outputs more consistent. The output length controls response duration, not prompt richness.

## Generation Duration

Aria generation time scales roughly with output measures:
- `--measures 1`: ~0.4-0.6s
- `--measures 2`: ~0.8-1.2s (default)
- `--measures 4`: ~1.5-2.5s
- `--measures 8`: ~3.0-5.0s

These are estimates on GPU (NVIDIA RTX 4090 or similar). On CPU, expect 5-10x slower.

## Constraints

1. **Human input**: Always exactly 1 measure per cycle (fixed)
2. **AI output**: Configurable from 1 to 8+ measures (via `--measures`)
3. **Timing**: Must respect MIDI clock pulse grid
   - Events outside [boundary_pulse, boundary_pulse + N*pulses_per_bar) are dropped
   - Forced note-offs ensure no hanging notes
4. **Buffer clearing**: After each N-measure playback, buffers are cleared and cycle restarts

## Testing Checklist

- [ ] Run with `--measures 1`: User 1 bar → AI 1 bar
  - Log should show `[enqueue] 1-measure generation job`
  - Playback range should be `[B..B+96)` for 4/4 time
  
- [ ] Run with `--measures 2`: User 1 bar → AI 2 bars (default MVP)
  - Logs should match original MVP behavior
  - Playback range should be `[B..B+192)`
  
- [ ] Run with `--measures 4`: User 1 bar → AI 4 bars
  - Log should show `[enqueue] 4-measure generation job`
  - Generation takes ~2-3 seconds
  - Playback range should be `[B..B+384)`
  
- [ ] Multiple cycles: Play bar, wait for AI, play bar again
  - Buffers should clear properly after each cycle
  - No accumulation of old events
  
- [ ] Edge case: Very large measure count (e.g., `--measures 16`)
  - Should still work, but generation may take 5+ seconds
  - Output may feel slow due to latency

## Files Modified

- `ableton_bridge.py`: CLI argument passing (already implemented)
- `ableton_bridge_engine.py`:
  - `GenerationJob`: Now uses `gen_bars` from parameter
  - `AbletonBridge.__init__`: Stores `self.gen_measures`
  - `_on_bar_boundary()`: Uses `self.gen_measures` for job creation
  - `_schedule_two_bar_response()`: Uses `self.gen_measures * pulses_per_bar` for limit enforcement
  - Logging: All "2-bar" references now show actual measure count

## Future Enhancements

1. **Variable prompt context**: `--prompt_measures N` to control input window
2. **Asymmetric cycles**: `--human_measures M --ai_measures N` for flexible patterns
3. **Pipelined generation**: Generate next response while playing current one
4. **Timeout handling**: Fallback pattern if generation too slow for selected measures
5. **Per-bar streaming**: Start playing AI output while generation still in progress

