# Automatic heart-sound processing and diagnostics

The primary program is `heart_sound_processor.py`. It automatically performs
signal validation, preprocessing, full-cycle estimation, S1/S2 event detection,
cycle rejection, stable-region selection, event extraction, timeline
regularisation, loop construction, and diagnostic export:

```powershell
python heart_sound_processor.py "input.wav" --output-dir "processed_heartbeat"
```

No `--stable-start` or `--stable-end` input is required. The processor selects
the interval automatically at complete S1-to-S1 cycle boundaries. Defaults are
an 8–12 s allowed interval, a 10 s target, at least eight complete cycles, and a
30 s loop output.

## Output grouping and naming

Every source recording receives one analysis-group directory. Recorder names in
the form `YYYYMMDDhhmmss<ID>.wav` are split into acquisition time and sample ID.
For example, `202607071630425298.wav` produces:

```text
processed_heartbeat/
└── 5298_20260707T163042/
    ├── 5298_20260707T163042_analysis.json
    ├── 5298_20260707T163042_stable_segment_raw.wav
    ├── 5298_20260707T163042_stable_comparison.png
    └── 5298_20260707T163042_event_samples/
        └── 5298_20260707T163042_s1_001.wav
```

If the input name does not follow that recorder format, the complete sanitized
stem is used as the sample ID and the file modification time is recorded as the
fallback time. `analysis.json` records the parsed ID, time, time source, prefix,
and group directory.

Primary output suffixes (all prefixed as described above):

```text
analysis.json
cycles.csv
heartbeat_events.csv
window_energy.csv
loop_event_energy.csv
stable_segment_raw.wav
stable_segment_filtered.wav
stable_segment_event_repaired.wav
authentic_loop.wav
regularized_events.wav
stable_segment_clean.wav
stable_segment_enhanced.wav
authentic_loop_clean.wav
authentic_loop_enhanced.wav
regularized_events_clean.wav
regularized_events_enhanced.wav
events_only_monitor.wav
background_only_monitor.wav
removed_noise_monitor.wav
event_samples/s1_*.wav
event_samples/s2_*.wav
diagnostic.png
diagnostic.svg
stable_comparison.png / .svg
authentic_loop_comparison.png / .svg
regularized_comparison.png / .svg
noise_diagnostics.png / .svg
```

The original outputs are never overwritten. `*_clean.wav` applies conservative
event-aware spectral noise reduction. `*_enhanced.wav` additionally suppresses
non-heart-sound intervals, reduces crest factor, adds makeup gain, and enforces
a configurable peak ceiling. The three `*_monitor.wav` stems are deliberately
boosted inspection signals for locating noise; they are not final mix assets.

`stable_segment_raw.wav` remains the untouched audit baseline. Loop rendering
uses only vetted real events from valid cycles. `stable_segment_event_repaired.wav`
is an intermediate audit file in which excluded weak/rejected stable events are
crossfaded with same-recording exemplars. The authentic loop uses those vetted
real timbres on a median-filtered natural rhythm bounded to +/-6% period and
+/-10% S1-S2 variation. The regularized loop uses the same event bank on an
exactly constant timing grid.

`loop_event_energy.csv` records every expected loop event, its time, peak, RMS,
dBFS, energy relative to the median of the same event type, and an `OK`/`WEAK`
preservation status. The default weak threshold is 45% of the same-type median.

The four comparison figures are generated automatically on every successful
run. Each uses aligned, shared scales and contains waveform envelopes,
50-ms/10-ms-hop RMS energy, Welch power spectral density, and numerical
peak/RMS/crest-factor metrics. `stable_comparison` also marks detected S1/S2
events; the authentic and regularized figures mark every expected S1/S2 time
through both waveform and RMS panels; `noise_diagnostics` compares the
deliberately boosted monitor stems.

Nine standalone time-energy figure pairs are also written: Raw/Base, Clean, and
Enhanced for each of Stable, Authentic, and Regularized. Their filenames end in
`_time_energy.png` or `_time_energy.svg`; each figure contains that WAV's own
waveform, 50-ms/10-ms-hop RMS curve, and aligned S1/S2 markers.

Enhancement controls:

```text
--denoise-reduction-db 12
--background-attenuation-db 8
--compressor-threshold-dbfs -22
--compressor-ratio 3
--enhanced-target-rms-dbfs -14
--enhanced-peak-dbfs -1
```

`analysis.json` contains the automatic stable interval, BPM, period candidates,
S1/S2 detections, raw/smoothed/regularized timelines, rejected cycles, confidence,
warnings, source hash, parameters, versions, and citations.

If the input is silent, too short, severely clipped, non-periodic, or otherwise
fails the configured constraints, the command returns a non-zero exit code and
still writes an `analysis.json` with `status: REJECT` and explicit rejection
reasons. It does not fabricate a stable interval.

## Diagnostic-only utility

`audio_diagnostics.py` generates reusable time-domain and short-time energy
diagnostics for WAV recordings. It is deliberately independent of heartbeat
segmentation and music generation.

## Installation

```powershell
python -m pip install -r requirements.txt
```

The dependency versions are pinned for experiment reproduction. If the project
later upgrades a dependency, record the new environment in the generated JSON
and update `requirements.txt` and `THIRD_PARTY.md` together.

## Diagnostic-only use

```powershell
python audio_diagnostics.py "input.wav"
```

The default `diagnostics` directory receives:

```text
input.time_energy.png
input.time_energy.svg
input.time_energy.csv
input.time_energy.json
```

The PNG and SVG show aligned waveform-amplitude and short-time RMS-energy plots.
The CSV contains every analysis window, and the JSON records the source SHA-256,
parameters, software versions, summary metrics, and citations.

## Stable-region overlay

```powershell
python audio_diagnostics.py "input.wav" `
  --stable-start 1.87 `
  --stable-end 12.22
```

## S1/S2 and stable-region annotations

The script can consume the future analysis module's JSON:

```json
{
  "stable_region": {
    "start_s": 1.87,
    "end_s": 12.22
  },
  "events": [
    {"type": "S1", "time_s": 1.872},
    {"type": "S2", "time_s": 2.194}
  ]
}
```

```powershell
python audio_diagnostics.py "input.wav" --analysis-json "analysis.json"
```

Command-line `--stable-start` and `--stable-end` values override the stable
region in the JSON without altering its S1/S2 events.

## Useful options

```text
--window-ms 50          RMS/peak window length
--hop-ms 10             Window hop length
--energy-floor-db -60   Lower display bound
--formats png svg       Figure formats
--channel-mode mean     Average multichannel recordings
--channel-mode first    Use the first channel only
--width 1600            Output width in pixels/SVG units
--height 900            Output height in pixels/SVG units
--dpi 200               PNG resolution metadata
--no-csv                 Suppress numeric CSV output
--no-json                Suppress reproduction manifest
```

## Scope

The script supports integer-PCM and IEEE-float WAV files readable by
`scipy.io.wavfile`. It visualizes audio and annotations but does not diagnose
normality, identify disease, or independently determine S1/S2 locations.

The method and third-party attribution are documented in [METHODS.md](METHODS.md)
and [THIRD_PARTY.md](THIRD_PARTY.md).
