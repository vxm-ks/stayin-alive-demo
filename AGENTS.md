# LegaSynth repository instructions

## Architecture authority

Read `docs/三阶段职责边界.md` before cross-stage work.

- Stage 1 owns story planning, patient heartbeat sample preparation, MuseCoco
  motif generation, and motif bar/tempo/key normalization.
- Stage 2 owns complete heartbeat MIDI construction, deterministic score
  assembly, MIDI-GPT completion, and protected-event validation.
- Stage 3 renders the immutable complete MIDI and assigns real S1/S2 timbres.
  It must not regenerate or retime heartbeat events.

`heartbeat_post_renderer` is retained only as deprecated experimental history
and must not become the formal Stage 3 entry point.

## Safety and data handling

- Never commit `.env`, API keys, patient audio, patient-derived SoundFonts,
  raw model responses, model weights, runtime queues, or generated outputs.
- Public tests must generate synthetic audio/MIDI at runtime.
- Preserve input hashes and fail closed at every cross-stage boundary.
- Never stretch or pitch-shift patient S1/S2 samples unless a future audited
  contract explicitly changes this rule.

## MuseCoco boundary

- Official Microsoft Muzic/MuseCoco source and checkpoints are external.
- Do not patch official source files. Work only through
  `musecoco_runtime_bridge/` and Stage 1 task packages.
- Pin the official commit and verify the hashes frozen in the queue adapter.
- The official runtime stays in WSL; all other project modules remain portable
  Python unless their own documentation says otherwise.

## Required verification

Run `python -m compileall -q .` and `python scripts/run_offline_tests.py` for
broad offline validation. Model, network, FluidSynth, and DeepSeek integration tests
must remain explicit opt-in checks.

When changing a cross-stage contract, update its Schema, tests, module README,
`docs/三阶段职责边界.md`, and `docs/项目全流程与Agent同步.md` together.
