# Pipelined Bar-Based Aria Bridge Architecture

## Overview
This document describes the pipelined bar-based generation system for real-time Aria music generation with MIDI clock synchronization from Ableton.

## Core Design Principles

1. **Per-Bar Generation**: Model generates 1 bar at a time (not 2)
2. **Pipelined Playback**: Generated bars are played 2 at a time, after both are ready
3. **Pulse-Grid Alignment**: All timing is based on MIDI clock pulses (24 ppqn from Ableton ARIA_CLOCK)
4. **Anchor-Based Counting**: Block counting starts from the first human note (not Ableton transport)

## MIDI Clock Grid Basics

- **MIDI Clock**: 24 pulses per quarter note (ppqn)
- **Time Signature**: 4/4 (4 beats per bar)
- **Pulses per Bar**: `4 beats/bar × 24 ppqn = 96 pulses/bar`
- **2-Bar Unit**: 192 pulses (used for playback scheduling)

## Architecture Components

### 1. Input Thread (`_input_loop`)
**Purpose**: Read live MIDI input and assign events to per-bar buffers

**Flow**:
```
MIDI Input → Check pulse count → Assign to bar buffer
```

**Key Actions**:
- On first `note_on` (velocity > 0): Set `anchor_pulse` to current pulse
- For each MIDI event (note_on, note_off, sustain):
  - Calculate `bar = (pulse - anchor_pulse) // 96`
  - Add event to `human_bar_buffers[bar]`
  - Tag event with pulse for prompt_midi conversion

**Critical Detail**: Must capture both note_on AND note_off events for Aria to know note durations

### 2. Generation Thread (`_generation_loop`)
**Purpose**: Detect bar boundaries and trigger per-bar generation

**Flow**:
```
Monitor next_bar_boundary_pulse → When reached, call _on_bar_boundary()
```

**Key Actions**:
- Check `if current_pulse >= next_bar_boundary_pulse`
- If true: Call `_on_bar_boundary(finished_bar)`
- Increment `bar_index` and `next_bar_boundary_pulse`

### 3. Bar Boundary Handler (`_on_bar_boundary`)
**Purpose**: Generate 1 bar of music given human input for that bar

**Flow**:
```
Extract bar's events → Build prompt MIDI → Call Aria.generate() → 
Parse generated MIDI → Queue messages → Check if 2-bar pair ready → Schedule playback
```

**Details**:
- Extracts `human_bar_buffers[finished_bar]`
- Builds 1-bar prompt via `buffer_to_tempfile_midi()`
- Calls `aria_engine.generate(horizon_s=1_bar_duration)`
- Stores generated MIDI messages in `generated_bar_queue[finished_bar]`
- **Scheduling Check**: Only after odd-numbered bars (1, 3, 5, ...)
  - When `finished_bar % 2 == 1`, check if both `finished_bar-1` and `finished_bar` are in queue
  - If yes: Call `_schedule_2bar_playback(finished_bar-1, finished_bar)`

### 4. 2-Bar Playback Scheduler (`_schedule_2bar_playback`)
**Purpose**: Convert 2 consecutive bars of generated MIDI to pulse-scheduled messages

**Timing Formula**:
```
start_pulse = anchor_pulse + (bar2 + 1) * 96
```

**Message Scheduling**:
- **Bar 1 messages**: `[start_pulse, start_pulse + 96)`
- **Bar 2 messages**: `[start_pulse + 96, start_pulse + 2*96)`

**Tick-to-Pulse Conversion**:
```
pulse_delta = (tick / ticks_per_beat) * 24.0
target_pulse = start_pulse + pulse_delta
```

**Result**: Messages queued in `scheduled_messages` with `(target_pulse, mido.Message)` tuples

### 5. Output Thread (`_output_loop`)
**Purpose**: Send scheduled messages to ARIA_OUT at the correct pulse

**Flow**:
```
Loop: Check for scheduled messages → If current_pulse >= target_pulse, send → Remove from queue
```

**Method**: `_service_scheduled_messages()`
- Thread-safely locks `scheduled_lock`
- Checks all `(target_pulse, msg)` in `scheduled_messages`
- Sends to `out_port` if pulse boundary reached
- Removes sent messages from queue

## State Variables

### Anchor & Bar Tracking
```python
anchor_pulse: Optional[int]           # Pulse of first human note
bar_index: int                        # Current bar number (0-based, relative to anchor)
next_bar_boundary_pulse: int          # Next bar boundary to check (bar_index * 96 + anchor_pulse)
```

### Per-Bar Buffering
```python
human_bar_buffers: dict[int, list]    # bar_index → [note_on, note_off, sustain events]
generated_bar_queue: dict[int, list]  # bar_index → [mido.Message objects with .time]
last_scheduled_bar: Optional[int]     # Highest bar pair scheduled for playback
```

### Scheduled Output
```python
scheduled_messages: list[(int, mido.Message)]  # (target_pulse, message) tuples
scheduled_lock: RLock                          # Thread-safe access
```

## Message Format

### Human Input Events (in `human_bar_buffers`)
Dynamic objects with attributes:
```python
msg_obj = type('MidiMsg', (), {
    'pulse': int,           # Absolute pulse number
    'msg_type': str,        # 'note_on', 'note_off', 'control_change'
    'note': int,            # (note_on/note_off only)
    'velocity': int,        # (note_on/note_off only)
    'control': int,         # (control_change only, e.g., 64 for sustain)
    'value': int,           # (control_change only, 0-127)
})()
```

### Generated MIDI Messages (in `generated_bar_queue`)
`mido.Message` objects with:
```python
msg.type: str           # 'note_on', 'note_off', 'control_change'
msg.note: int           # Pitch (0-127)
msg.velocity: int       # Velocity (0-127)
msg.control: int        # CC number (e.g., 64)
msg.value: int          # CC value (0-127)
msg.time: int           # Delta time in MIDI ticks (relative to previous message)
```

### Scheduled Messages (in `scheduled_messages`)
Tuples of `(target_pulse: int, mido.Message)` for pulse-based playback

## Timing Diagram

```
Pulse:     1000  1050  1096  1150  1192  1250  1288  1350  1384
           ├─────┼─────┼─────┼─────┼─────┼─────┼─────┼─────┤
Event:     [anchor] ...bar 0 input...  ...bar 1 input...  ...bar 2...
           
Gen:                  ↓gen bar 0       ↓gen bar 1        
Store:              bar 0 done        bar 1 done

Schedule:                                ↓schedule bars 0-1
Playback:                                [bars 0-1 output until 1384]

Phasing:   H(uman)   bar 0-input        bar 1-input                 bar 2-input
           M(odel)   [bar 0 generates]  [bars 0-1 playback]  [bars 0-1 + bar 2-gen]
```

**Key Observations**:
1. Human input is always being collected (bars 0, 1, 2, ...)
2. Generation happens immediately after bar boundary is crossed
3. Playback is delayed 2 bars from generation (pipelined)
4. No overlapping generation for same bar (each bar generated once)
5. Playback windows are 2 bars (192 pulses)

## Data Flow Summary

```
┌─────────────────────────────────────────────────────────────┐
│                      INPUT THREAD                           │
│  MIDI Input → Assign to bar buffers → human_bar_buffers    │
└───────────────────────┬─────────────────────────────────────┘
                        │
                        ↓ (bar buffer when boundary reached)
        ┌───────────────────────────────────────┐
        │   GENERATION THREAD                   │
        │  Check bar_boundary_pulse → _on_bar_  │
        │  boundary() → extract events → Aria   │
        │  generate() → _parse_generated_midi   │
        │  → generated_bar_queue                │
        └───────────────────┬───────────────────┘
                            │
                    (when 2-bar pair ready)
                            ↓
        ┌───────────────────────────────────────┐
        │ _schedule_2bar_playback()             │
        │ Convert ticks → pulses → queue        │
        │ scheduled_messages                    │
        └───────────────────┬───────────────────┘
                            │
                            ↓
        ┌───────────────────────────────────────┐
        │        OUTPUT THREAD                  │
        │ _service_scheduled_messages()         │
        │ Send when current_pulse >=            │
        │ target_pulse                          │
        └───────────────────┬───────────────────┘
                            │
                            ↓
                      MIDI OUT (ARIA_OUT)
```

## Port Configuration

- **Clock Input** (`--clock_in ARIA_CLOCK`): MIDI clock source from Ableton
  - ClockGrid exclusively reads this port
  - TempoTracker is disabled when clock_in is provided (port conflict prevention)
  
- **MIDI Input** (`--in ARIA_IN`): Human input
  - Input thread reads from this port
  
- **MIDI Output** (`--out ARIA_OUT`): Model output
  - Output thread sends scheduled messages to this port

## Error Handling

1. **Missing Bar Events**: If a bar has no human input, generation is skipped for that bar
2. **Generation Failure**: If Aria returns None, that bar is not added to queue
3. **Slow Generation**: 2-bar scheduling waits until both bars are generated
4. **Failsafe**: After 6 seconds with anchor set but no generation, logs warning

## Configuration

CLI arguments (selected):
```bash
python ableton_bridge.py \
  --in ARIA_IN \              # Human input port
  --out ARIA_OUT \            # Model output port
  --clock_in ARIA_CLOCK \     # MIDI clock source
  --beats_per_bar 4 \         # Standard 4/4
  --ticks_per_beat 480 \      # Standard MIDI resolution
  --temperature 0.9 \         # Aria sampling temperature
  --top_p 0.95 \              # Aria top-p nucleus sampling
  --checkpoint aria-medium-base.safetensors
```

## Testing Checklist

- [ ] Anchor sets on first human note
- [ ] Bar boundaries detected at correct pulse intervals (96 pulses apart)
- [ ] Generation triggers per bar (not per 2-bar block)
- [ ] Human input is captured with pulse tags
- [ ] Generated MIDI is parsed and stored per bar
- [ ] 2-bar playback scheduling occurs at expected times
- [ ] Output messages are sent at correct pulses
- [ ] No MIDI Port conflicts (Clock exclusive, tempo_tracker disabled)
- [ ] Note durations are preserved in prompt MIDI

## Legacy Notes

- **Old 2-Measure Blocks**: Previous version used 2-bar blocks (192 pulses) for both generation and playback
- **Deprecated Methods**: `_on_block_boundary()`, `_on_anchor_boundary()` are no-ops (replaced by `_on_bar_boundary()`)
- **Legacy MIDI Buffer**: `self.midi_buffer` still populated for backward compatibility (can be removed)

