"""Local single-job orchestration for the complete LegaSynth pipeline."""

from .pipeline import PipelineConfig, PipelineError, run_pipeline

__all__ = ["PipelineConfig", "PipelineError", "run_pipeline"]
