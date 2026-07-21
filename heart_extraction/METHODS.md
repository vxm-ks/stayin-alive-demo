# Time–energy diagnostic method

## Automatic PCG processing pipeline

`heart_sound_processor.py` first removes the robust DC estimate, resamples to a
configurable analysis rate (2 kHz by default), and applies a fourth-order
zero-phase Butterworth band-pass filter. Its default 20–200 Hz band reflects the
observed recording device and remains configurable rather than being claimed as
universal.

The event envelope is a weighted fusion of robustly scaled Hilbert amplitude
and average Shannon energy. Autocorrelation is searched only over the configured
heart-rate range. Competing lags are retained, and a supported double-length lag
can replace the strongest lag to reduce half-period S1/S2 errors.

For the selected complete period, the envelope is folded into phase bins. The
algorithm searches for an ordered pair satisfying the configured S1-to-S2
duration range. Predicted S1 and S2 positions are refined to local envelope
maxima. The implementation then applies the state order

```text
S1 -> systole -> S2 -> diastole -> S1
```

and rejects cycles using robust period, S1-to-S2 duration, amplitude, and
physiological-duration constraints.

All complete-cycle windows satisfying the minimum duration and cycle-count
requirements are evaluated. The stable-region score rewards valid-cycle ratio,
mean cycle quality, and proximity to the target length, and penalizes the
coefficient of variation of the full cycle and S1-to-S2 interval. Stable-region
boundaries are therefore automatically selected S1 boundaries, not manually
entered times.

For audio looping, the same offset of at most 100 ms is applied before both S1
boundaries. The offset minimizes the sum of local edge energies. Using one shared
offset preserves the exact S1-to-S1 stable duration and avoids changing the
average BPM while reducing discontinuity at the repeated waveform boundary.

This is an original deterministic heuristic inspired by duration-constrained
PCG segmentation research. It does **not** claim to reproduce the trained
logistic-regression HSMM of Springer et al. or the DHMM of Schmidt et al.

Relevant primary references:

1. Schmidt, S. E., Holst-Hansen, C., Graff, C., Toft, E., & Struijk, J. J.
   (2010). *Segmentation of heart sound recordings by a duration-dependent
   hidden Markov model*. Physiological Measurement, 31(4), 513–529.
   <https://doi.org/10.1088/0967-3334/31/4/004>
2. Springer, D. B., Tarassenko, L., & Clifford, G. D. (2016). *Logistic
   Regression-HSMM-Based Heart Sound Segmentation*. IEEE Transactions on
   Biomedical Engineering, 63(4), 822–832.
   <https://doi.org/10.1109/TBME.2015.2475278>
3. Lee, S. et al. (2024). *A Noise-Robust Heart Sound Segmentation Algorithm
   Based on Shannon Energy*. IEEE Access, 12, 7747–7761.
   <https://doi.org/10.1109/ACCESS.2024.3351570>

## Event-aware noise reduction and loudness enhancement

Enhancement is performed only on additional output variants; raw files are
retained unchanged. A wider rendering band is used than the detection band.
Short-time Fourier frames outside the detected S1/S2 activity mask estimate the
local noise power spectrum. The estimated noise power is softly subtracted with
frequency/time smoothing and a configurable gain floor, avoiding hard spectral
zeroing.

The approach is based on the spectral-subtraction principle introduced by:

S. Boll (1979), *Suppression of Acoustic Noise in Speech Using Spectral
Subtraction*, IEEE Transactions on Acoustics, Speech, and Signal Processing,
27(2), 113–120. <https://doi.org/10.1109/TASSP.1979.1163209>

This project does not copy Boll's implementation. It uses S1/S2-derived
non-event frames as the noise profile and adds a spectral floor and two-axis
gain smoothing. The spectral floor is recorded in `analysis.json`.

After denoising, non-event intervals are attenuated with a smooth activity mask.
A 10-ms envelope compressor with 5-ms attack and 100-ms release reduces the
large measured crest factor before makeup gain. A continuous hyperbolic-tangent
soft limiter contains remaining transient peaks at the configured ceiling; the
signal is not hard-clipped. Because compression and soft limiting change dynamics,
enhanced output is not described as mathematically lossless.

## Purpose

The diagnostic output is intended for engineering inspection of the audio
pipeline. It shows whether events occur at plausible times, whether transient
energy has been lost, whether clipping or DC offset is present, and whether a
selected stable region corresponds to visibly consistent cardiac cycles. It is
not a clinical diagnostic.

## Input conversion

Integer PCM samples are converted to full-scale floating-point units (FS) using
the representable magnitude of their data type. Unsigned PCM is first centred
around zero. IEEE-float WAV values are retained. Multichannel input is averaged
by default; `--channel-mode first` instead selects channel zero.

The conversion does not peak-normalize the recording. Consequently, amplitude
differences between recordings remain visible and the clipping metric retains
its meaning.

## Waveform display

Plotting every audio sample can be slow and may cause a renderer to hide narrow
transients. The script partitions the waveform into at most
`--max-waveform-bins` consecutive bins and retains the minimum and maximum of
each bin. The filled region between these extrema is therefore a display
downsampling operation, not a modification of the analysed audio.

## Short-time energy

For a window containing \(N\) full-scale samples \(x[n]\), root-mean-square
amplitude is

\[
x_{\mathrm{RMS}} = \sqrt{\frac{1}{N}\sum_{n=0}^{N-1}x[n]^2}.
\]

It is converted to dBFS by

\[
E_{\mathrm{dBFS}} = 20\log_{10}(\max(x_{\mathrm{RMS}}, 10^{-12})).
\]

The default window is 50 ms and the default hop is 10 ms. Every window's start,
end, centre, peak, RMS, and dBFS value is exported to CSV. The final partial
window is retained and divided by its actual number of samples. Because a time
length may not correspond to an integer number of samples (for example at
11,025 Hz), the requested milliseconds, rounded sample counts, and resulting
effective milliseconds are all recorded in JSON.

## Audio comparison figures

Successful heart-sound processing automatically produces paired PNG/SVG
comparison figures for the stable segment, authentic loop, regularized loop,
and monitoring stems. Signals within one figure share the same time and
waveform-amplitude scales. The energy panel uses the same 50-ms window and
10-ms hop described above. Peak level, RMS level, and crest factor are computed
from the full signal rather than the display envelope.

The frequency panel estimates power spectral density with Welch's averaged
periodogram method using Hann windows and 50% overlap. It displays 20 Hz to the
smaller of 500 Hz and Nyquist, matching the band relevant to the rendered PCG
while retaining evidence of higher-frequency noise. Reference:

P. D. Welch (1967), *The Use of Fast Fourier Transform for the Estimation of
Power Spectra: A Method Based on Time Averaging Over Short, Modified
Periodograms*, IEEE Transactions on Audio and Electroacoustics, 15(2), 70–73.
<https://doi.org/10.1109/TAU.1967.1161901>

## Event-preserving loop rendering

The stable raw segment is retained unchanged for audit. Loop assets apply a
separate event-preservation policy. S1 and S2 reference banks admit events from
valid stable cycles, require at least 45% event confidence, and reject amplitudes
below 55% of the same-type median. If fewer than three candidates remain, the
highest-confidence valid candidates are retained as a conservative fallback.

The repaired stable audit segment replaces excluded events with same-type
exemplars from the same recording using a 12-ms squared-sine crossfade.

Loop rendering itself uses the vetted event bank. Authentic timing is derived
only from valid stable cycles: periods and S1-S2 intervals receive a three-cycle
median filter, periods are bounded to +/-6% of their median, and S1-S2 intervals
are bounded to +/-10% of their median. This retains limited physiological timing
variation without repeating outlier periods. Regularized rendering uses the same
real-event bank with one constant median period and one constant median S1-S2
interval. No synthetic oscillator or generated heart sound is introduced.

Every expected event is measured from its event time through 180 ms for S1 or
150 ms for S2. `loop_event_energy.csv` reports peak, RMS, dBFS, and RMS relative
to the median for the same loop, variant, and event type. Values below 45% are
marked `WEAK`. Authentic and regularized comparison figures place distinct S1
and S2 lines through both waveform and short-time RMS panels so a missing event
can be checked visually against the numeric table.

In addition to the three-way comparison figures, every Raw/Base, Clean, and
Enhanced WAV in the Stable, Authentic, and Regularized families receives its
own PNG/SVG time-energy plot. These nine figures prevent shared plot scales or
overlaid RMS curves from hiding a weak event in one processing level.

## Reproducibility

Every JSON manifest records:

- SHA-256 of the original WAV;
- source sample rate, channels, dtype, and duration;
- all analysis and rendering parameters;
- Python, NumPy, SciPy, and Pillow versions;
- platform identifier;
- summary amplitude and clipping metrics;
- stable-region and S1/S2 annotations;
- software citations.

The SVG embeds the same manifest in its `<metadata>` element. The PNG embeds it
in the `AnalysisMetadata` text field.

## Software citations

1. Harris, C. R., Millman, K. J., van der Walt, S. J., et al. (2020).
   *Array programming with NumPy*. Nature, 585, 357–362.
   <https://doi.org/10.1038/s41586-020-2649-2>
2. Virtanen, P., Gommers, R., Oliphant, T. E., et al. (2020).
   *SciPy 1.0: fundamental algorithms for scientific computing in Python*.
   Nature Methods, 17, 261–272.
   <https://doi.org/10.1038/s41592-019-0686-2>
3. Pillow contributors. *Pillow documentation and source code*.
   <https://python-pillow.github.io/>

Pillow performs raster rendering only. SVG output is generated directly by this
project's Python code using the W3C SVG format.
