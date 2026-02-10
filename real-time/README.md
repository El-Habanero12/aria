# Real-Time Aria + Ableton Bridge

A minimal, low-latency real-time MIDI bridge that reads live keyboard input from Ableton and generates continuations using the Aria music generation model.

## What It Does

1. **Listens** for human MIDI input (notes, sustain pedal)
2. **Records** the last N seconds of playing (default: 4s) once listening starts
3. **When listening window closes**, generates a continuation via Aria
4. **Streams** generated MIDI back to Ableton with proper rhythm
5. **Cooldown** for a brief moment before listening for the next performance
6. **Repeats** - ready for the next human phrase

Example flow:
- Play notes for 4 seconds (listening window)
- Generation starts automatically
- Generated music streams to your instrument track
- Brief pause (cooldown)
- Ready to play again

## Setup

### Prerequisites

- **Windows 11** with CUDA-compatible GPU (tested on RTX 4070)
- **Python 3.10+** with PyTorch CUDA support
- **Ableton Live** (any version)
- **loopMIDI**: Download from https://www.tobias-erichsen.de/software/loopmidi.html

### 1. Install loopMIDI

1. Download and install loopMIDI
2. Run loopMIDI
3. Create two new MIDI ports by clicking the "+" button:
   - `ARIA_IN` (for input from Ableton keyboard)
   - `ARIA_OUT` (for output to Ableton instrument)

### 2. Install Dependencies

```bash
cd real-time
pip install -r requirements.txt
```

Or if Aria is already set up:
```bash
pip install mido python-rtmidi torch safetensors
```

### 3. Configure Ableton

1. Open Ableton Live
2. Create two MIDI tracks:
   - **Track A** (Input): Route your MIDI keyboard → this track
   - **Track B** (Output): Load an instrument, set input to `ARIA_OUT` port
3. In Track A, add a MIDI effect or software that routes output to `ARIA_IN` port
   - Simplest: use a Max for Live MIDI device, or
   - Route via Ableton's MIDI routing: Track A output → External Instrument → `ARIA_IN`
4. **Important**: Make sure Track B is *not* set to monitor/input from Track A (to avoid feedback)
5. **For tempo sync** (optional but recommended):
   - In Ableton MIDI preferences, enable **Sync** output
   - Route MIDI Sync output to a new loopMIDI port called `ARIA_CLOCK` (or custom name with `--clock_in`)
   - The bridge will automatically detect clock messages and sync to your Ableton tempo

### 4. Run the Bridge

```bash
python ableton_bridge.py --in ARIA_IN --out ARIA_OUT --checkpoint path/to/aria-medium-gen.safetensors
```

Or with defaults (assumes checkpoint in `../models/aria-medium-gen.safetensors`):
```bash
python ableton_bridge.py
```

#### CLI Options

```
--in PORT_NAME              Input MIDI port (default: ARIA_IN)
--out PORT_NAME             Output MIDI port (default: ARIA_OUT)
--checkpoint PATH           Checkpoint path (default: aria-medium-gen)
--listen_seconds N          Time to listen before generating (default: 4.0)
--gen_seconds N             Continuation duration to generate (default: 1.0)
--cooldown_seconds N        Cooldown after generation (default: 0.2)
--temperature N             Sampling temperature (default: 0.9)
--top_p N                   Top-p sampling (default: 0.95)
--device cuda|cpu           Inference device (default: cuda)
--clock_in PORT_NAME        MIDI clock input port (default: ARIA_CLOCK)
--quantize                  Quantize output to 1/16 note grid (default: off)
--ticks_per_beat N          MIDI resolution (default: 480)
--list-ports                List available MIDI ports and exit
```

### 5. Play!

1. Start the bridge script
2. Watch for logs: `[INFO] Bridge started. Press Ctrl+C to stop.`
3. Play notes on your MIDI keyboard in Ableton (Track A) - **listen window starts**
4. After ~4 seconds of playing or silence, generation starts automatically
5. Watch the generated MIDI appear in real-time on Track B!
6. Brief cooldown, then ready to play your next phrase

The system listens to your playing and generates continuations based on what you perform.

## Tempo Synchronization (Optional)

The bridge can automatically sync to your Ableton session tempo via MIDI Clock:

### Setup

1. In Ableton **Preferences → MIDI Ports**, enable **Sync** as an output
2. Route Ableton's MIDI Sync output to a loopMIDI port (e.g., `ARIA_CLOCK`)
3. Run the bridge:
   ```powershell
   python ableton_bridge.py --clock_in ARIA_CLOCK
   ```

### What it does

- Reads MIDI Clock (24 ppqn) from Ableton
- Computes BPM from clock pulse intervals (rolling average)
- Embeds detected tempo in the prompt MIDI
- Optional: quantize generated output to 1/16 note grid with `--quantize` flag

### Output

You'll see logs like:
```
[INFO] MIDI Clock: START
[INFO] BPM: 120.0
[INFO] Sent 47 MIDI messages in 2.34s
```

## Architecture

- **`midi_buffer.py`**: Thread-safe rolling buffer of timestamped MIDI messages (last N seconds)
- **`prompt_midi.py`**: Converts rolling buffer to MIDI files/dicts suitable for Aria prompt
- **`aria_engine.py`**: Wraps Aria model loading and generation inference
- **`ableton_bridge_engine.py`**: Orchestrates three concurrent threads:
  - Input thread: reads MIDI from `ARIA_IN` port
  - Generation thread: runs Aria every ~200ms
  - Output thread: sends generated events to `ARIA_OUT` port

## Performance Notes

- **Latency**: Listen window (~4s) + Aria inference time (~100-500ms depending on GPU)
- **GPU Memory**: Model loads once at startup (~2-4GB for medium model)
- **Real-time Safety**: Generation errors are logged and skipped; MIDI loop always stays responsive
- **Human-only Prompt**: Only human input triggers listening; generated notes do NOT feed back
- **Listening Gap**: Brief cooldown (0.2s) between generations to avoid immediate retrigger

## Troubleshooting

### "Could not find port ARIA_IN"
- Ensure loopMIDI is running and both ports are created
- Run with `--list-ports` to see available ports
- Use correct port names: `python ableton_bridge.py --in "My Port" --out "My Port"`

### "CUDA requested but not available"
- Verify PyTorch detects CUDA: `python -c "import torch; print(torch.cuda.is_available())"`
- If not, install PyTorch with CUDA support or use `--device cpu` (slower)

### "Could not find checkpoint"
- Provide full path to checkpoint: `--checkpoint C:\path\to\aria-medium-gen.safetensors`
- Check that the file exists and is readable

### No generated MIDI appearing in Ableton
- Check Ableton Track B has `ARIA_OUT` selected as input and monitoring is ON
- Check logs for generation errors
- Try playing longer/more notes (need at least ~0.5s of input for meaningful generation)

### Generation is slow or stalling
- Reduce `--horizon_seconds` (default 0.6, try 0.3)
- Reduce `--tick_seconds` if you want fewer but faster generations
- Try `--device cpu` if GPU memory is exhausted (trade latency for stability)
- Monitor GPU: `nvidia-smi` should show the process using VRAM

## Design Decisions (MVP)

1. **Listen-then-generate**: User plays 4 seconds → system generates continuation
   - More musical than constant real-time interruption
   - Respects musical phrasing
   - Clear start/end points for generation
2. **Human-only trigger**: Only human input starts listening
   - No feedback loops
   - Stable, predictable behavior
3. **Time-windowed prompt**: Last N seconds of performance form the prompt
   - Works regardless of note density
   - Consistent context for generation
4. **Immediate playback**: Generated MIDI sent with rhythm preserved
   - Uses mido's timing to maintain musical timing
5. **Optional tempo sync**: MIDI Clock input from Ableton
   - Automatically detects session tempo
   - Embeds tempo in prompt for coherent generation
   - Can optionally quantize output to grid
6. **Simple sampling**: Temperature + top-p, no complex scheduling
   - Conservative defaults (0.9 temp, 0.95 top_p) for coherent output
   - Tweakable via CLI flags
7. **Temp MIDI files**: Each generation writes prompt to temp file
   - Acceptable for MVP (fast on modern SSDs)
   - Could be optimized to in-memory serialization later

## Future Improvements

- [ ] OSC control for live parameter tweaking (tempo, temperature, listen window)
- [ ] Waveform visualization of prompt and generated continuation
- [ ] Adaptive listen window based on silence detection (stop early if no activity)
- [ ] In-memory MIDI serialization (avoid temp files)
- [ ] MIDI learn for Ableton control surfaces
- [ ] Multi-voice conditioning (e.g., instrument/emotion embeddings)
- [ ] Analysis dashboard showing generation stats and timing
- [ ] Harmony guiding (optional chord constraints)

## License

Same as Aria repo. See main repo LICENSE.
