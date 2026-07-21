# Third-party notices

LegaSynth does not vendor model weights, SoundFonts, FluidSynth binaries, or
the official MuseCoco source tree. Users install those dependencies separately
and remain responsible for their license terms.

## Microsoft Muzic / MuseCoco

- Source: https://github.com/microsoft/muzic
- Frozen integration commit: `a2efda0bfb297b1520282a61ca12513dc3517744`
- Upstream source license: MIT
- This repository contains only LegaSynth adapters and deployment guidance.

## MuseCoco checkpoints

- Text-to-attribute: https://huggingface.co/XinXuNLPer/MuseCoco_text2attribute
- Attribute-to-music: https://huggingface.co/XinXuNLPer/MuseCoco_attribute2music
- Model cards currently identify the checkpoints as Apache-2.0.
- Checkpoints are downloaded by the user and are never committed here.

## FluidSynth

- Source: https://github.com/FluidSynth/fluidsynth
- License: GNU Lesser General Public License, as stated by upstream.
- The executable is an optional external Stage 3 runtime dependency.

## Python dependencies

NumPy, SciPy, Pillow, Pydantic, HTTPX, and pretty_midi are installed from their
respective package indexes. Individual module `THIRD_PARTY.md` files document
algorithm-level use and academic attribution where applicable.

SoundFonts have licenses independent of FluidSynth. No general or
patient-derived SoundFont is included in this repository.
