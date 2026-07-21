#!/usr/bin/env bash
set -Eeuo pipefail

commit="${MUSECOCO_MUZIC_COMMIT:-a2efda0bfb297b1520282a61ca12513dc3517744}"
data_root="${XDG_DATA_HOME:-$HOME/.local/share}/legasynth"
source_root="${MUSECOCO_SOURCE_ROOT:-$data_root/muzic}"
runtime_root="${MUSECOCO_RUNTIME_ROOT:-$data_root/musecoco-runtime}"

if [[ -e "$source_root" ]]; then
  echo "Refusing to replace existing source root: $source_root" >&2
  exit 2
fi
if [[ -e "$runtime_root" ]]; then
  echo "Refusing to replace existing runtime root: $runtime_root" >&2
  exit 2
fi

mkdir -p "$(dirname "$source_root")"
git clone --filter=blob:none --no-checkout https://github.com/microsoft/muzic.git "$source_root"
git -C "$source_root" sparse-checkout init --cone
git -C "$source_root" sparse-checkout set musecoco
git -C "$source_root" checkout --detach "$commit"

cp -a "$source_root/musecoco" "$runtime_root"
mkdir -p "$runtime_root/task_queue" "$runtime_root/task_running" \
  "$runtime_root/task_failed" "$runtime_root/task_done"

cat <<EOF
Official MuseCoco source prepared at:
  $runtime_root

No checkpoint was downloaded. Follow docs/MUSECOCO_DEPLOYMENT.md to create the
official Python environment and place attribute2music.pt at the exact official
checkpoint path. Then run scripts/install_musecoco_adapter_wsl.sh.
EOF
