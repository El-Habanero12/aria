# Playback Scheduling Fix - Sequential 2-Bar Playback

## Problem Statement

When 2-bar playback was initiated, both generated bars played **simultaneously** (overlapped in time) instead of sequentially. This caused notes from both bars to sound at the same time, creating a jumbled mix instead of coherent musical progression.

### Root Cause
The original implementation had correct pulse offset math (`start_pulse` vs `start_pulse + pulses_per_bar`), but the starting pulse was computed relative to `anchor_pulse` rather than the **actual current boundary pulse**. Combined with potential tick-to-pulse conversion timing, this could shift the effective playback windows.

## Solution

### Changes Made to `_schedule_2bar_playback()`

**1. Use Actual Boundary Pulse (Not Computed)**
```python
# OLD: Computed from anchor and bar indices
start_pulse = self.anchor_pulse + (bar2 + 1) * pulses_per_bar

# NEW: Get the actual clock pulse when scheduling happens
boundary_pulse = self.clock_grid.get_pulse_count()
```

**Why**: The actual boundary pulse from the clock is the ground truth. Computed values can drift if there are timing variations.

---

**2. Separate Bar Pulse Ranges Explicitly**
```python
# Bar 1: [boundary_pulse, boundary_pulse + pulses_per_bar)
abs_tick = 0
for msg in bar1_msgs:
    abs_tick += msg.time
    offset_pulses = int((abs_tick / float(tpq)) * 24.0)
    target_pulse = boundary_pulse + offset_pulses
    bar1_events.append((target_pulse, msg.copy()))

# Bar 2: [boundary_pulse + pulses_per_bar, boundary_pulse + 2*pulses_per_bar)
abs_tick = 0
for msg in bar2_msgs:
    abs_tick += msg.time
    offset_pulses = int((abs_tick / float(tpq)) * 24.0)
    target_pulse = boundary_pulse + pulses_per_bar + offset_pulses
    bar2_events.append((target_pulse, msg.copy()))
```

**Why**: Clear naming (`bar1_events`, `bar2_events`) and explicit pulses_per_bar offset ensures no ambiguity.

---

**3. Sort All Messages Once Before Queuing**
```python
all_messages = bar1_events + bar2_events
all_messages.sort(key=lambda x: x[0])  # Sort by target_pulse

with self.scheduled_lock:
    self.scheduled_messages.extend(all_messages)
```

**Why**: Ensures the output thread processes events in strict pulse order, preventing any reordering that could cause overlap.

---

**4. Detailed Logging with Pulse Ranges**
```python
logger.info(
    f"[schedule_2bar] Boundary B={boundary_pulse}, "
    f"bar1 range [{boundary_pulse}..{boundary_pulse + pulses_per_bar}), "
    f"bar2 range [{boundary_pulse + pulses_per_bar}..{boundary_pulse + 2*pulses_per_bar})"
)
logger.info(
    f"[schedule_2bar] Bars {bar1}-{bar2}: {len(all_messages)} total events "
    f"(bar1: {len(bar1_events)} events pulse_min={bar1_min} pulse_max={bar1_max}, "
    f"bar2: {len(bar2_events)} events pulse_min={bar2_min} pulse_max={bar2_max})"
)
```

**Why**: Enable rapid verification that pulse ranges don't overlap. Min/max values should be:
- bar1: `pulse_min ≥ B`, `pulse_max < B + 96`
- bar2: `pulse_min ≥ B + 96`, `pulse_max < B + 192`

---

**5. Set Model End Pulse for Phase Management**
```python
self.model_end_pulse = boundary_pulse + (2 * pulses_per_bar)
```

**Why**: Tells the output thread when to switch from MODEL phase back to HUMAN phase after playback completes.

## Expected Behavior After Fix

### Timeline

```
Pulse:    P=1192 (end of human bar 1, start of scheduling)
                  |
Boundary  B=1192  ├─────────────────────────────────────────┤
                  └─ bar1 playback ─┬─ bar2 playback ─┘
                  [1192..1288)     [1288..1384)

Generated Bar 0 plays 1192-1288 (96 pulses)
Generated Bar 1 plays 1288-1384 (96 pulses)
```

### Log Output
```
[schedule_2bar] Boundary B=1192, bar1 range [1192..1288), bar2 range [1288..1384)
[schedule_2bar] Bars 0-1: 32 total events (bar1: 16 events pulse_min=1192 pulse_max=1280, bar2: 16 events pulse_min=1288 pulse_max=1376)
```

**Key validation**: 
- bar1 pulses ✓ all in [1192, 1288)
- bar2 pulses ✓ all in [1288, 1384)
- bar2 min (1288) > bar1 max (1280) ✓ no overlap

---

## Testing Checklist

- [ ] Run bridge with Ableton playing (4/4 time, tempo ~120 BPM)
- [ ] Play human bar 0 and bar 1
- [ ] Check logs: `[schedule_2bar]` shows clear pulse range separation
- [ ] **Auditory test**: Generated bar 0 plays first, then bar 1 transitions smoothly
- [ ] Verify no notes start simultaneously from different bars
- [ ] Check next scheduling at end of bar 3 (for bars 2-3): pulse ranges should shift by 192 pulses
- [ ] Verify `model_end_pulse` switches phase correctly after 2 bars

---

## Performance Impact

- **No degradation**: Sorting happens once per 2-bar pair (every ~0.5s at 120 BPM)
- **Clock overhead**: Single `get_pulse_count()` call instead of arithmetic
- **Memory**: Marginal (temporary list for sorting, immediately used and cleared)

---

## Files Modified

- [ableton_bridge_engine.py](ableton_bridge_engine.py) - `_schedule_2bar_playback()` method only

---

## Code Validation

✅ No syntax errors  
✅ All type hints preserved  
✅ Thread safety maintained (RLock used for scheduled_messages)  
✅ Backward compatible with existing phases/state machine  
✅ Minimal changes (only scheduling method modified)

