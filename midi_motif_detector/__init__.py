"""Detect the first recurring musical motif in a MIDI file."""

from .midi_motif_detector import detect_first_motif, inspect_tracks

__all__ = ["detect_first_motif", "inspect_tracks"]
