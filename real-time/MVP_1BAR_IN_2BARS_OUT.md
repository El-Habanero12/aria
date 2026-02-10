# MVP: Simple 1-Bar-In → 2-Bars-Out Cycle

## Overview

The bridge now implements a simple, non-overlapping real-time music generation cycle:

1. **User plays 1 human bar** (during PHASE_HUMAN)
2. **At bar boundary**: If idle, AI triggers 2-bar generation asynchronously
3. **While AI generates**: User can start playing the next bar (no blocking)
4. **When AI 2-bar response ready**: Immediately schedule and play it (switch to PHASE_AI_PLAY)
5. **After 2-bar playback finishes**: Return to PHASE_HUMAN, clear buffers, repeat cycle

## State Machine

```
┌─────────┐                    ┌──────────┐
│HUMAN    │  AI response ready │ AI_PLAY  │
│(collect)├───────────────────>│(playback)│
│ input   │                    │          │
└────┬────┘                    └────┬─────┘
     ^                              │
     │   Playback ends              │
     └──────────────────────────────┘
```

**PHASE_HUMAN**:
- Record human MIDI input into `human_bar_buffers[bar_index]`
- At end of bar: if no AI response scheduled, enqueue 2-bar generation job
- Stay in HUMAN while generation happens in background
- When AI response ready: immediately switch to AI_PLAY and schedule

**PHASE_AI_PLAY**:
- Play the scheduled 2-bar AI response
- Do NOT trigger new generation (block bar boundaries)
- At playback end (pulse >= model_end_pulse): clear buffers and switch back to HUMAN

## Key Components

### GenerationJob (Simplified)
```python
class GenerationJob:
    bar_index: int              # Which human bar triggered this
    prompt_events: list         # Human MIDI for prompt
    aria_engine: AriaEngine
    temperature: float
    top_p: float
    gen_bars: int = 2          # Always 2 for MVP
    result_midi_path: Optional[str]  # Set when Aria finishes
```

### GenerationWorker
- Runs in background thread
- Pulls jobs from `gen_job_queue`
- Calls `aria_engine.generate(horizon_s = gen_bars * 1.0)`
- Stores result in `job.result_midi_path` when done

### Main Bridge Methods

**_on_bar_boundary(finished_bar)**:
```python
if phase == PHASE_HUMAN:
    # Enqueue 2-bar generation
    job = GenerationJob(..gen_bars=2..)
    gen_job_queue.put(job)
    pending_ai_job = job
elif phase == PHASE_AI_PLAY:
    # Block new generation
    pass
```

**_check_and_schedule_ai_response()**:
```python
if pending_ai_job.result_midi_path is not None:
    # AI finished! Schedule playback
    _schedule_two_bar_response(midi_path, boundary_pulse, pulses_per_bar)
    phase = PHASE_AI_PLAY
    pending_ai_job = None
```

**_schedule_two_bar_response(midi_path, boundary, pulses_per_bar)**:
- Parse MIDI, enforce 2-bar limit (2*pulses_per_bar = 192 pulses for 4/4)
- Discard events beyond boundary
- Force note-offs for unclosed notes at 2-bar end
- Send CC123 (All Notes Off) at end
- Clear old queue, queue new messages

**_generation_loop()**:
```python
while running:
    # Check if AI response ready (only in HUMAN phase)
    if phase == PHASE_HUMAN and pending_ai_job:
        _check_and_schedule_ai_response()
    
    # Monitor bar boundaries (only in HUMAN phase)
    if phase == PHASE_HUMAN and current_pulse >= next_bar_boundary:
        _on_bar_boundary(finished_bar)
        bar_index += 1
        next_bar_boundary += pulses_per_bar
```

**_service_scheduled_messages()**:
- Send due messages (target_pulse <= current_pulse)
- Pop sent messages from queue (one-shot)
- At model_end_pulse: clear queue, switch HUMAN, clear human_bar_buffers

## Logging

Key log messages:

```
# User bar boundary (collect human input)
[bar_boundary] finished_bar=0, phase=human
[bar_boundary] Bar 0: 8 prompt events, triggering 2-bar generation
[enqueue] 2-bar generation job for bar 0 queued

# AI generation in progress (background)
[gen_worker] Starting generation for bar 0 (2 bars)
[gen_worker] Bar 0 (2-bar generation) done in 0.45s

# AI response ready, schedule playback
[ai_ready] 2-bar response ready for job at bar 0, scheduling playback
[schedule_2bar] 2-bar response: 32 events in pulse [96..288), min=96 max=280
[phase] HUMAN -> AI_PLAY at pulse=96

# Output thread playing
OUT scheduled: note_on target_pulse=96 now=96
OUT scheduled: note_on target_pulse=102 now=102
...

# Playback finishes
[phase] AI_PLAY -> HUMAN at pulse=288, playback finished, queue_size=2
[service] Cleared human_bar_buffers for next cycle
```

## Guarantees

✅ **No overlapping**: 1 human bar + 2 AI bars per cycle, no simultaneous playback  
✅ **No accumulation**: Buffers cleared after each cycle, queue emptied after each message  
✅ **Responsive**: Generation happens in background, doesn't block input  
✅ **Clock-aligned**: All timing via MIDI clock pulses  
✅ **No hanging notes**: Forced note-offs at 2-bar end, CC123 sent  

## Testing Checklist

- [ ] Play 1 bar, wait for AI response (logs should show enqueue, gen_worker, ai_ready)
- [ ] AI response plays back without overlapping
- [ ] Logs show correct pulse ranges [B..B+192) for 2 bars
- [ ] After AI finishes, phase switches back to HUMAN
- [ ] No "old scheduled events" or "stale bars" warnings
- [ ] Queue size reaches 0 at playback end
- [ ] Play 2+ cycles in sequence (no accumulation)

## Files Modified

- **ableton_bridge_engine.py**:
  - GenerationJob: Added `gen_bars` parameter
  - GenerationWorker: Simplified to generate from job directly
  - Phase: PHASE_MODEL → PHASE_AI_PLAY
  - _on_bar_boundary(): MVP 1-bar-in logic
  - _check_and_schedule_ai_response(): NEW - checks job status, schedules when ready
  - _schedule_two_bar_response(): NEW - 2-bar enforcer
  - _generation_loop(): Rewritten for MVP cycle
  - _service_scheduled_messages(): Clears buffers on phase switch

## MVP Trade-offs

✅ **Simplicity**: Single 2-bar response per cycle, no per-bar pipelining  
✅ **Predictability**: Clear, linear state transitions  
✅ **Reliability**: Less edge cases around half-ready bars  
❌ **Latency**: Must wait for full 2-bar Aria response before playback (vs. streaming 1 bar)  
❌ **Interactivity**: User plays 1 bar, AI responds with 2 bars (vs. per-bar interaction)

For future enhancement:
- Add pipelining back (generate bar 1 while playing, schedule bar 2 when ready)
- Add fallback patterns for timeout (sustain, loop, silence)
- Add config to adjust cycle length (1-in-3-out, 2-in-4-out, etc.)

