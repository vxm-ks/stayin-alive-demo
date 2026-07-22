# MuseCoco external deployment

LegaSynth includes its own deterministic encoder, task-package contract,
Windows-to-WSL bridge, fail-closed queue, result collection, and MIDI
normalization. It intentionally does not vendor Microsoft Muzic source or any
checkpoint.

## Frozen upstream contract

- Repository: `https://github.com/microsoft/muzic.git`
- Commit: `a2efda0bfb297b1520282a61ca12513dc3517744`
- Lock and expected official-file hashes: `third_party/muzic.lock.json`
- Attribute-to-music checkpoint:
  `https://huggingface.co/XinXuNLPer/MuseCoco_attribute2music`

The current LegaSynth flow bypasses the text-to-attribute checkpoint. Stage 1
creates audited deterministic one-hot attributes, while the unmodified official
`stage2_pre.py` and attribute-to-music generation remain in use.

## 1. Prepare WSL

Install WSL2 with an Ubuntu distribution, Git, Miniforge/Conda, and the CUDA
stack required by the official MuseCoco release. The historical official
environment uses Linux, Python 3.8, PyTorch 1.11, Fairseq, and a large
attribute-to-music checkpoint. Keep this environment separate from the Windows
Python 3.11+ LegaSynth environment.

Clone this repository inside WSL or access it through `/mnt/<drive>/...`, then:

```bash
bash scripts/prepare_musecoco_source_wsl.sh
```

The script sparse-checks out only `musecoco/` at the frozen commit and creates a
separate runtime copy. It refuses to overwrite an existing source or runtime.

## 2. Create the official environment

Create the environment at any location. For compatibility with the default
adapter configuration, this is convenient:

```bash
conda create -n MuseCoco python=3.8
conda activate MuseCoco
```

Install the dependencies specified by the frozen upstream MuseCoco README and
verify that the official preprocessing and inference scripts import correctly.
Dependency versions must follow upstream/CUDA compatibility; they are not
installed into LegaSynth's Windows environment.

## 3. Place the checkpoint

Download `attribute2music.pt` from the official model repository. Do not commit
it to this repository. Place or symlink it as:

```text
$MUSECOCO_RUNTIME_ROOT/
  2-attribute2music_model/
    checkpoints/
      linear_mask-1billion/
        checkpoint_2_280000.pt
```

Record its SHA-256 locally. The file is roughly 14.5 GB, so leave it in WSL or
a model cache with sufficient space.

## 4. Install the LegaSynth adapter

```bash
export MUSECOCO_PYTHON="$HOME/miniforge3/envs/MuseCoco/bin/python"
bash scripts/install_musecoco_adapter_wsl.sh
```

Add the variables printed by the installer to `~/.profile`, reopen WSL, and
verify:

```bash
legasynth-musecoco status
```

The adapter verifies the frozen official source hashes before it accepts a
task. It never edits official source code. It only writes documented runtime
inputs, outputs, queue state, logs, and pre-cleanup snapshots.

## 5. Invoke from Windows

Copy `.env.example` to `.env`. To avoid dependence on WSL login-shell `PATH`,
configure the deployed interpreter and queue script explicitly:

```dotenv
MUSECOCO_QUEUE_PYTHON=/home/blue/miniforge3/envs/MuseCoco/bin/python
MUSECOCO_QUEUE_SCRIPT=/home/blue/musecoco_tools/musecoco_task_queue.py
```

An installed `legasynth-musecoco` command remains supported through
`MUSECOCO_QUEUE_COMMAND`, but it is used only when the explicit pair is absent.

Then run:

```powershell
python -m stage1_story_agent enqueue-musecoco `
  --musecoco-dir .\stage1_story_agent\outputs\<run>-musecoco `
  --run `
  --max-attempts 3 `
  --wsl-distro Ubuntu
```

The Windows bridge converts the task-package path with `wslpath`, invokes the
installed WSL command, collects verified results, and runs Stage 1 bar/tempo/key
normalization. No model path is embedded in the Windows project.

## Upgrade policy

To change the upstream commit or checkpoint:

1. create a separate runtime;
2. recompute every official-file and checkpoint SHA-256;
3. update `third_party/muzic.lock.json` and the adapter constants together;
4. run adapter contract tests and one isolated generation task;
5. retain the previous runtime until audit comparison succeeds.
