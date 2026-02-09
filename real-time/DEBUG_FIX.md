# Bar-Based Aria Bridge: Debug & Fix Summary

## Problem Diagnosed

**Root Cause**: Both `TempoTracker` and `ClockGrid` were attempting to open the same MIDI input port (`ARIA_CLOCK`) simultaneously. In mido, this causes message delivery to be unpredictable—typically only the first reader gets messages, leaving the second one silent.

**Evidence from logs**:
- ✅ `TempoTracker` received MIDI clock (showed BPM updates ~100)
- ❌ `ClockGrid` saw zero pulses (all notes logged with `pulse=0`)
- ❌ No `ClockGrid pulse update` messages (ClockGrid's per-second logging never fired)
- ❌ No block boundaries detected

## Solution Implemented

### File: `ableton_bridge.py`
**Change**: Disabled `TempoTracker` when using `--clock_in` mode (grid-based interaction).

**Logic**:
```python
if args.clock_in:
    logger.info(f"Using ClockGrid on '{args.clock_in}'; disabling TempoTracker (port conflict)")
else:
    # Start TempoTracker only for legacy time-based mode
```

**Rationale**: 
- `ClockGrid` is now the exclusive MIDI clock reader
- `ClockGrid` provides thread-safe pulse counting
- Estimated BPM can be computed from pulse deltas in `ClockGrid` if needed later
- No port contention

## Enhanced Logging (Already Added)

The bridge now logs:

| What | Message Pattern |
|------|-----------------|
| Clock pulses (1/sec) | `ClockGrid pulse update: count=<N>, running=True` |
| Boundaries | `ClockGrid: block boundary pulse=<N>` |
| Human input | `[HUMAN] note_on pitch=<note> vel=<vel> pulse=<pulse>` |
| Boundary handler | `[BOUNDARY] pulse=<N>, phase=<phase>, measures=2` |
| Generation trigger | `[trigger] generating at pulse=<N>, measures=2, horizon_s=<sec>` |
| Phase switches | `[phase] HUMAN -> MODEL, scheduled until pulse=<N>` |
| Failsafe (6-sec) | `[FAILSAFE] No generation in 6s despite human input...` |

## How to Test

### Prerequisites
1. Ableton with loopMIDI ports set up:
   - `ARIA_IN` — input from Ableton to bridge
   - `ARIA_OUT` — output from bridge to Ableton
   - `ARIA_CLOCK` — Ableton clock source

2. Ableton Sync enabled (Preferences → MIDI Ports → ARIA_CLOCK set to Sync=ON)

3. Bridge checkpoint available: `../models/aria-medium-base.safetensors`

### Run Command
From the `real-time` folder:

```bash
python ableton_bridge.py \
  --in ARIA_IN \
  --out ARIA_OUT \
  --clock_in ARIA_CLOCK \
  --measures 2 \
  --beats_per_bar 4 \
  --ticks_per_beat 480 \
  --temperature 1.1 \
  --device cpu \
  --checkpoint "C:\Code\GitHub\Aria Habz\aria\models\aria-medium-base.safetensors"
```

### Test Sequence
1. **Start the bridge** (run command above)
2. **In Ableton**: Click Play (starts MIDI clock)
3. **Watch logs** for:
   - `ClockGrid pulse update: count=...` — should appear every 1 second, count increasing
   - `ClockGrid: block boundary pulse=384` — after ~4 seconds (2 measures at ~100 BPM)
4. **Play on keyboard** during the first 2 measures (e.g., a simple melody)
5. **At the 2-measure boundary**, watch for:
   - `[BOUNDARY] pulse=384, phase=human`
   - `[HUMAN] note_on ...pulse=...` in the captured messages
   - `[trigger] generating at pulse=384, measures=2`
   - `[phase] HUMAN -> MODEL, scheduled until pulse=576`
6. **Listen/view output**: Generated notes should play on ARIA_OUT during measures 3–4

### Expected Timeline (at ~100 BPM)
- **0–2 sec**: Silence, waiting for human input
- **1–2 sec**: You play something
- **4–5 sec**: Boundary fires, generation starts
- **8–16 sec**: Model playback
- **Repeat**

## Debugging Checklist

If everything stops after the fix:

| Symptom | Check |
|---------|-------|
| No `ClockGrid pulse update` | Is Ableton playing? Is ARIA_CLOCK connected correctly? |
| Pulses don't increase past ~10 | Boundary logic may still be wrong; verify `pulses_per_block = 2 * 4 * 24 = 192` |
| `[BOUNDARY]` fires but no `[trigger]` | Generation failed; check error logs and Aria engine |
| `[FAILSAFE]` kicks in at 6 sec | Normal for first test; it will force generation and you'll see phase switch |

## Files Modified

1. **clock_grid.py**
   - Added `last_pulse_log_time` tracking
   - Added per-second pulse updates to logs

2. **ableton_bridge_engine.py**
   - Enhanced logging in `_input_loop` and `_on_block_boundary`
   - Added `last_generation_time` and `failsafe_forced` flags
   - Failsafe generation attempt at 6 seconds
   - Updated `_generation_loop` to implement failsafe

3. **ableton_bridge.py**
   - Disabled `TempoTracker` when `--clock_in` is provided
   - Added note to logs about port conflict avoidance

## Next Actions

1. **Run the test** with the command above
2. **Paste the logs** here
3. If pulses now increase correctly:
   - Remove or comment out the failsafe (marked with `[FAILSAFE]`)
   - If generation triggers correctly at boundaries, we're done!
4. If there are still issues, the logs will show exactly where the flow breaks

## Notes

- The failsafe is temporary and should be removed once the boundary-based generation is confirmed working
- CPU mode is slower but safe for testing; use `--device cuda` once validated
- Model generation quality is separate from this trigger fix
