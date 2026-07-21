#!/usr/bin/env bash
set -Eeuo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/legasynth"
bin_root="${HOME}/.local/bin"
python_bin="${MUSECOCO_PYTHON:-$HOME/miniforge3/envs/MuseCoco/bin/python}"

if [[ ! -x "$python_bin" ]]; then
  echo "MuseCoco Python not found: $python_bin" >&2
  echo "Create the official MuseCoco environment first, then set MUSECOCO_PYTHON." >&2
  exit 2
fi

mkdir -p "$data_root" "$bin_root"
install -m 0644 \
  "$repo_root/musecoco_runtime_bridge/musecoco_task_queue.py" \
  "$data_root/musecoco_task_queue.py"
install -m 0755 \
  "$repo_root/musecoco_runtime_bridge/legasynth-musecoco" \
  "$bin_root/legasynth-musecoco"
install -m 0755 \
  "$repo_root/musecoco_runtime_bridge/musecoco_queue" \
  "$bin_root/musecoco_queue"
install -m 0755 \
  "$repo_root/musecoco_runtime_bridge/musecoco_task_worker" \
  "$bin_root/musecoco_task_worker"

cat <<EOF
Adapter installed.

Add this to ~/.profile if ~/.local/bin is not already on PATH:
  export PATH="\$HOME/.local/bin:\$PATH"

Recommended runtime variables:
  export MUSECOCO_RUNTIME_ROOT="$data_root/musecoco-runtime"
  export MUSECOCO_OUTPUT_ROOT="$data_root/musecoco-outputs"
  export MUSECOCO_TOOLS_ROOT="$data_root"
  export MUSECOCO_CONDA_PREFIX="$(dirname "$(dirname "$python_bin")")"
  export MUSECOCO_PYTHON="$python_bin"
  export MUSECOCO_QUEUE_SCRIPT="$data_root/musecoco_task_queue.py"

After deploying official MuseCoco, verify with:
  legasynth-musecoco status
EOF
