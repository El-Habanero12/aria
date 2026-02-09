"""Aria model inference wrapper for real-time generation."""

import logging
import os
import time
import tempfile
from typing import List, Optional, Dict, Any

import torch

logger = logging.getLogger(__name__)


class AriaEngine:
    """
    Wraps Aria generation: loads model once, provides generate() method.
    """

    def __init__(
        self,
        checkpoint_path: str,
        device: str = "cuda",
        config_name: str = "medium",
    ):
        """
        Load the Aria model once at initialization.

        Args:
            checkpoint_path: Path to .safetensors checkpoint
            device: 'cuda' or 'cpu' (use cuda for real-time)
            config_name: Model config name (e.g., 'medium', 'large')
        """
        self.checkpoint_path = checkpoint_path
        self.device = device
        self.config_name = config_name
        self.model = None
        self.tokenizer = None
        self.dtype = None

        self._load_model()
        logger.info(
            f"AriaEngine initialized: {config_name} on {device}, "
            f"checkpoint={os.path.basename(checkpoint_path)}"
        )

    def _load_model(self) -> None:
        """Load model and tokenizer from checkpoint."""
        try:
            from safetensors.torch import load_file
            from aria.inference.model_cuda import TransformerLM
            from aria.model import ModelConfig
            from aria.config import load_model_config
            from ariautils.tokenizer import AbsTokenizer
        except ImportError as e:
            raise ImportError(
                f"Failed to import Aria dependencies: {e}. "
                "Ensure aria is installed and in PYTHONPATH."
            )

        # Detect dtype
        self.dtype = (
            torch.bfloat16
            if torch.cuda.is_available() and torch.cuda.is_bf16_supported()
            else torch.float16
        )

        # Load config and model
        try:
            model_config = ModelConfig(**load_model_config(name=self.config_name))
            model_config.set_vocab_size(AbsTokenizer().vocab_size)
            self.model = TransformerLM(model_config)

            state_dict = load_file(filename=self.checkpoint_path)
            self.model.load_state_dict(state_dict=state_dict, strict=False)
            self.model = self.model.to(self.device)
            self.model.eval()

            self.tokenizer = AbsTokenizer()

            logger.debug(
                f"Model loaded: {self.model.__class__.__name__}, "
                f"vocab_size={model_config.vocab_size}, "
                f"dtype={self.dtype}"
            )
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
            raise

    def generate(
        self,
        prompt_midi_path: str,
        prompt_duration_s: int = 4,
        horizon_s: float = 0.6,
        temperature: float = 0.9,
        top_p: Optional[float] = 0.95,
        min_p: Optional[float] = None,
        max_new_tokens: Optional[int] = None,
    ) -> str:
        """
        Generate continuation from a prompt MIDI file.

        Args:
            prompt_midi_path: Path to .mid file
            prompt_duration_s: How many seconds of prompt to use
            horizon_s: How many seconds to generate (~0.6s for MVP)
            temperature: Sampling temperature (0.9 recommended)
            top_p: Top-p sampling (0.95 recommended)
            min_p: Min-p sampling (alternative to top_p)
            max_new_tokens: Max tokens to generate (auto-set if None)

        Returns:
            Path to the generated MIDI file (temporary file, caller must clean up).
        """
        try:
            from aria.inference import get_inference_prompt
            from aria.inference.sample_cuda import sample_batch
            from ariautils.midi import MidiDict

            # Get and tokenize prompt
            midi_dict = MidiDict.from_midi(prompt_midi_path)
            prompt = get_inference_prompt(
                midi_dict=midi_dict,
                tokenizer=self.tokenizer,
                prompt_len_ms=int(1e3 * prompt_duration_s),
            )

            # Estimate tokens for horizon:
            # Aria typically generates ~0.5-1 token/ms at 0.6s = 600ms
            # Conservative: 1 token/ms => 600 tokens for 0.6s, but we cap it smaller
            if max_new_tokens is None:
                # Very conservative for low-latency: ~2-4 tokens per 100ms
                max_new_tokens = min(128, int(horizon_s * 200))

            max_new_tokens = min(8096 - len(prompt), max_new_tokens)

            if max_new_tokens <= 0:
                logger.warning("Prompt too long, no room to generate.")
                return None

            logger.debug(
                f"Generating: prompt_len={len(prompt)}, "
                f"max_new_tokens={max_new_tokens}, temp={temperature}, top_p={top_p}"
            )

            # Sample
            start_time = time.time()
            with torch.inference_mode():
                results = sample_batch(
                    model=self.model,
                    tokenizer=self.tokenizer,
                    prompt=prompt,
                    num_variations=1,
                    max_new_tokens=max_new_tokens,
                    temp=temperature,
                    force_end=False,
                    top_p=top_p,
                    min_p=min_p,
                    compile=False,
                )

            gen_time = time.time() - start_time
            logger.debug(f"Generation took {gen_time:.2f}s, produced {len(results[0])} tokens")

            # Detokenize to MIDI dict and save to temp file
            if results:
                tokenized_seq = results[0]
                midi_dict = self.tokenizer.detokenize(tokenized_seq)
                midi_obj = midi_dict.to_midi()

                # Save to temp file
                tmp = tempfile.NamedTemporaryFile(suffix='.mid', delete=False)
                tmp.close()
                midi_obj.save(tmp.name)
                
                logger.debug(f"Generated MIDI saved to {tmp.name}")
                return tmp.name
            else:
                return None

        except Exception as e:
            logger.exception(f"Generation error: {e}")
            return None

    def _midi_to_events(self, midi_obj) -> List[Dict[str, Any]]:
        """
        Convert mido.MidiFile to event list with relative times.

        Returns a list of dicts:
        [
            {'type': 'note_on', 'note': p, 'velocity': v, 'time': t_sec},
            {'type': 'note_off', 'note': p, 'time': t_sec},
            ...
        ]
        """
        events = []
        current_time = 0.0  # In seconds

        # Standard: 120 BPM = 500ms per quarter note
        # Use MIDI file ticks_per_beat if available
        ticks_per_beat = midi_obj.ticks_per_beat or 480
        ms_per_beat = 500  # 120 BPM
        ms_per_tick = ms_per_beat / ticks_per_beat

        for track in midi_obj.tracks:
            current_time = 0.0
            for msg in track:
                # Accumulate time
                current_time += msg.time * ms_per_tick / 1000.0  # Convert to seconds

                if msg.type == 'note_on':
                    events.append({
                        'type': 'note_on',
                        'note': msg.note,
                        'velocity': msg.velocity,
                        'time': current_time,
                    })
                elif msg.type == 'note_off':
                    events.append({
                        'type': 'note_off',
                        'note': msg.note,
                        'time': current_time,
                    })
                elif msg.type == 'control_change' and msg.control == 64:
                    # Sustain pedal
                    events.append({
                        'type': 'control_change',
                        'control': 64,
                        'value': msg.value,
                        'time': current_time,
                    })

        return events
