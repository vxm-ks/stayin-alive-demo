# Stage 3 mixing comparison — case 01

This experiment renders the same complete MIDI and heartbeat package under three
mixing configurations. All conditions use the same source assets, heartbeat
conditioning profile, FluidSynth settings, sample rate, and true-peak ceiling.

## Fixed inputs

- Complete MIDI SHA-256:
  `92dcfb0f73f785ca1781d69f0a877a0c12abdfc43aa8bcc710056dd9fa478b22`
- Heartbeat-package manifest SHA-256:
  `6fc2f755371fc06f64c2c95a6a6a5b7caa76cdb88d503ca4db1d5d0f1866b826`
- GeneralUser GS SoundFont SHA-256:
  `9575028c7a1f589f5770fccc8cff2734566af40cd26ed836944e9a5152688cfe`
- Sample rate: 48 kHz
- FluidSynth gain: 0.5
- Heartbeat conditioning: `speaker_safe_loud_v1`
- True-peak ceiling: -1 dBTP
- Published diagnostic stems: enabled

## Conditions

1. `direct_manual`: music and heartbeat are mixed at 0 dB without balance
   estimation. Only common peak protection is applied.
2. `global_relative`: music receives a fixed -4 dB gain and one global heartbeat
   gain is estimated from the median event-window RMS, targeting heartbeat 7 dB
   above music. No per-event gain or music ducking is used.
3. `adaptive_current`: the current design uses K-weighted per-event measurement,
   S1/S2 gain smoothing, bounded local music ducking, and loudness/true-peak-aware
   mastering.

The experiment compares complete system configurations. Because the direct
baseline also differs in its fixed music gain, differences must not be attributed
to a single component without a further controlled ablation.

## Reproducing the comparison

Copy `cases.example.json`, replace its placeholder paths with one or more complete
MIDI files and Stage 1 heartbeat-package directories, and run:

```powershell
python .\run_three_case_ablation.py `
  --cases-json .\cases.json `
  --general-sf2 D:\path\to\GeneralUser-GS.sf2 `
  --fluidsynth D:\path\to\fluidsynth.exe
```

Each case is rendered with all three plans. Generated audio, logs, and progress
state are intentionally excluded from version control.
