[CmdletBinding()]
param([string]$Python = "python")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

& $Python -m compileall -q `
    heart_extraction tempo_bar_renderer heartbeat_stage1 heartbeat_midi_exporter `
    midi_motif_detector stage1_story_agent midigpt_scaffold_builder `
    musecoco_runtime_bridge stage3_midi_renderer wav_track_mixer

& $Python .\scripts\run_offline_tests.py
