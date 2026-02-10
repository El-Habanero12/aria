# Quick Reference: `--measures N`

## Basic Usage

```bash
# Default (2 AI measures per 1 human measure)
python ableton_bridge.py --clock_in ARIA_CLOCK --in ARIA_IN --out ARIA_OUT \
  --checkpoint "path/to/aria-medium-base.safetensors"

# Generate 4 AI measures
python ableton_bridge.py --measures 4 --clock_in ARIA_CLOCK --in ARIA_IN --out ARIA_OUT \
  --checkpoint "path/to/aria-medium-base.safetensors"

# Generate 1 AI measure (tightest interaction)
python ableton_bridge.py --measures 1 --clock_in ARIA_CLOCK --in ARIA_IN --out ARIA_OUT \
  --checkpoint "path/to/aria-medium-base.safetensors"
```

## Common Configurations

| Use Case | Command | Notes |
|----------|---------|-------|
| **Real-time interaction** | `--measures 1` | User plays 1 bar, AI responds 1 bar. Tightest loop. |
| **Default MVP** | `--measures 2` | User plays 1 bar, AI responds 2 bars. Good balance. |
| **Music generation** | `--measures 4` | User plays 1 bar, AI responds 4 bars. More output. |
| **Long-form** | `--measures 8` | User plays 1 bar, AI responds 8 bars. Takes 4-5 seconds. |

## Flow Diagram

```
--measures N means:

┌────────────────────────────┐
│ User plays 1 measure       │  (Fixed)
└────────────────────────────┘
              ↓
┌────────────────────────────┐
│ AI generates N measures    │  (Configurable)
│ (runs in background)       │
│ Takes ~0.5s per measure    │
└────────────────────────────┘
              ↓
┌────────────────────────────┐
│ AI plays N measures        │  (Synchronized to clock)
│ (no new input during)      │
└────────────────────────────┘
              ↓
┌────────────────────────────┐
│ Loop back to user input    │
└────────────────────────────┘
```

## How It Works Internally

### Pulse Calculation (4/4 time, 24 ppqn)
- 1 measure = 96 pulses
- `--measures 1` → 96 pulses of playback
- `--measures 2` → 192 pulses of playback (MVP default)
- `--measures 4` → 384 pulses of playback

### Event Dropping
- Generated MIDI events outside the [boundary, boundary + N*96) window are discarded
- Ensures no "tail" of notes plays after the N measures
- Forced note-offs at boundary + N*96

### Buffer Clearing
- At the end of playback, `human_bar_buffers` is cleared
- Allows the next 1-measure cycle to start fresh
- No accumulation or carry-over between cycles

## Expected Behavior by Configuration

### `--measures 1` (Tightest Real-Time)
```
[User]  │M1│....
[AI]    │   │M1│....
[User]  │   │   │M2│....
[AI]    │   │   │   │M2│....
```
- Minimal latency (0.5-1 sec per cycle)
- Maximum interactivity
- Each party always gets 1 measure of input

### `--measures 2` (Default MVP)
```
[User]  │M1│....
[AI]    │   │M1|M2│....
[User]  │   │    │ │M2│....
[AI]    │   │    │ │   │M3|M4│....
```
- 1-2 seconds per cycle
- Good balance for musical interaction

### `--measures 4` (Longer Output)
```
[User]  │M1│....
[AI]    │   │M1|M2|M3|M4│....
[User]  │   │   |  |  |  │M2│....
[AI]    │   │   |  |  |  │   │M3|M4|M5|M6│....
```
- 2-3 seconds per cycle
- More output, but longer wait times

## Logging Output

When you run with `--measures 4`:

```
[bar_boundary] Bar 0: 8 prompt events, triggering 4-measure generation
[enqueue] 4-measure generation job for bar 0 queued
[gen_worker] Starting generation for bar 0 (4 bars)
[gen_worker] Bar 0 (4-bar generation) done in 1.85s
[ai_ready] 4-measure response ready for job at bar 0, scheduling playback
[schedule_2bar] 4-measure response: 64 events in pulse [96..480), min=96 max=472
[phase] HUMAN -> AI_PLAY at pulse=96
OUT scheduled: note_on target_pulse=102 now=102
OUT scheduled: note_on target_pulse=115 now=115
...
[phase] AI_PLAY -> HUMAN at pulse=480, playback finished, queue_size=0
```

## Prompt Context (Always 2 Bars)

Regardless of `--measures`, the **input prompt to Aria is always 2 bars** (or less):
- Bar N-1 (previous) + Bar N (current)

This is independent of output measures for consistency.

## Temperature & Sampling

The `--measures` option does **not** affect sampling parameters:
- `--temperature` (default 0.8): How random the output is
- `--top_p` (default 0.9): Diversity of predictions

These remain independent. Use them to control output character:
```bash
# More creative/random longer sequences
python ableton_bridge.py --measures 4 --temperature 0.9 --top_p 0.95

# More stable/predictable longer sequences
python ableton_bridge.py --measures 4 --temperature 0.7 --top_p 0.85
```

## Performance Notes

### GPU (NVIDIA RTX 4090)
- 1-2 measures: <1 second
- 3-4 measures: 1-2 seconds
- 5-8 measures: 2-5 seconds

### CPU
- Expect **7-10x slower** than GPU
- Even 2 measures may take 7-10 seconds
- Recommended: Use GPU if possible

### Ableton Performance
- With `--measures 2-4`: Minimal CPU overhead (single worker thread)
- With `--measures 8+`: Generation may delay input processing
- Recommended: Keep below 6 measures for responsive real-time feel

## Troubleshooting

### "No output after playing a measure"
→ Generation may be slow. Check logs for `[gen_worker]` time. Consider reducing `--measures` or using GPU.

### "Output plays too fast/slow"
→ Check MIDI clock source (--clock_in). Verify tempo in Ableton matches bridge expectations.

### "Events clipped or cut short"
→ Confirm `--measures N` matches your session expectations. Events outside [B, B+N*pulses_per_bar) are dropped by design.

### "Hanging notes"
→ Rare. If it happens, forced note-offs + CC123 should clean up. Restart bridge if needed.

## Further Configuration

### Time Signatures
```bash
# 3/4 time (waltz)
python ableton_bridge.py --beats_per_bar 3 --measures 4 ...

# 6/8 time (compound duple)
python ableton_bridge.py --beats_per_bar 6 --measures 4 ...
```

### Sampling Control
```bash
# Conservative (more predictable)
python ableton_bridge.py --temperature 0.7 --top_p 0.85 ...

# Creative (more diverse)
python ableton_bridge.py --temperature 0.95 --top_p 0.95 ...
```

### Full Example
```bash
python ableton_bridge.py \
  --in ARIA_IN \
  --out ARIA_OUT \
  --clock_in ARIA_CLOCK \
  --checkpoint "C:\path\to\aria-medium-base.safetensors" \
  --measures 3 \
  --beats_per_bar 4 \
  --temperature 0.8 \
  --top_p 0.9 \
  --device cuda
```

