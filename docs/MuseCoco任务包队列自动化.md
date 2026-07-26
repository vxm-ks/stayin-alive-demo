# MuseCoco 任务包队列自动化

## 2026-07-24 Showcase 输出完整性门禁

Showcase wrapper 已升级到 `1.3.0`。官方生成进程返回零不再等同于任务成功；worker
必须同时满足以下条件才会返回零并把任务移入 `task_done`：

1. `result.remi.txt` 是非空真实文件，包含且只包含一个 `<sep>`；
2. `<sep>` 后的 REMIGEN2 音乐 token 类型合法，`d-*` 必须紧跟 `p-*`，
   `v-*` 必须紧跟 `d-*`，结尾不得是未完成事件；
3. REMI 至少包含一个小节 token 和一个完整音符事件；
4. `result.mid` 必须存在且为非空真实文件，并通过 `MThd`、轨道数和 PPQ
   的基础结构检查；
5. 归档后的 REMI/MIDI 再次验证并记录 SHA-256。

如果官方脚本只生成 REMI、REMI 语法非法、官方解码器没有生成 MIDI，或 MIDI
结构非法，worker 会返回非零。既有队列会在每次严格快照和清理后自动重试，
默认最多 3 次；不会把 REMI-only 结果误标为成功。

在进入重试前允许一种严格受限的截断恢复：若任务声明的
`generation_target_bars` 个完整 `b-*` 小节边界已经全部生成，wrapper 可将
该边界之后的 token 丢弃，再使用 MuseCoco 已安装的官方 `MidiDecoder`
解码截断副本。原始结果保存在 `result.remi.raw.txt`，正式交付仍是
`result.remi.txt`。如果非法 token 位于目标范围内、完整边界不足或截断副本仍
无法解码，恢复失败并进入正常重试；wrapper 不猜测或补写任何音符 token。

`result_audit.json` 新增 `output_validation`，记录 REMI token/小节/完整音符数量、
MIDI 大小/轨道数/PPQ以及门禁状态。结果收集器只接受
`output_validation.status=passed`、同时具有 REMI/MIDI 哈希且现场文件哈希一致的
输出。MuseCoco 官方生成和解码代码未修改。

## 最重要的运行命令

在项目根目录的 PowerShell 中，把 Stage1 已生成的 `*-musecoco` 目录入队并串行运行：

```powershell
D:\conda\python.exe -m stage1_story_agent enqueue-musecoco `
  --musecoco-dir .\stage1_story_agent\outputs\<时间戳>-musecoco `
  --run `
  --max-attempts 3
```

该命令成功后还会自动把官方结果收回原 Stage1 MuseCoco 目录，并发布精确长度、速度和调性已统一的主题：

```text
<时间戳>-musecoco/
  raw_results/
  generated_themes/
    theme-A/
      raw.mid
      bars-normalized.mid
      tempo-normalized.mid
      final.mid
      normalization_audit.json
    theme_manifest.json
```

默认生成目标是约 12 小节，最终输出是 8 小节。可在 `plan` 命令中使用 `--musecoco-generation-bars` 与 `--musecoco-output-bars` 调整。过长结果按精确 tick 裁剪并补 Note Off；过短结果报错，不补静音、不发布半成品。

只入队、不运行：

```powershell
D:\conda\python.exe -m stage1_story_agent enqueue-musecoco `
  --musecoco-dir .\stage1_story_agent\outputs\<时间戳>-musecoco
```

只运行一个待处理任务，用于首次烟雾测试：

```powershell
wsl.exe -d Ubuntu -- $HOME/musecoco_queue --max-tasks 1 --max-attempts 3
```

只读查看状态：

```powershell
wsl.exe -d Ubuntu -- $HOME/miniforge3/envs/MuseCoco/bin/python `
  $HOME/musecoco_tools/musecoco_task_queue.py status
```

不要同时启动两个 worker，也不要在 worker 运行时手工编辑或清理 MuseCocoRuntime。

## 流程边界

自动化只安装在 `$HOME/musecoco_tools/` 与 `$HOME/musecoco_queue`、`$HOME/musecoco_task_worker`，不修改 MuseCoco 官方代码。正式流程为：

```text
Stage1 编码器
  -> 每个主题拆成一个单样本任务包
  -> 原子复制到 task_queue
  -> 原子移动到 task_running
  -> 安装三个官方运行输入
  -> 未修改的 stage2_pre.py
  -> 未修改的 interactive_1billion.sh
  -> 保存结果、输入备份和审计
  -> 成功移到 task_done；失败移到 task_failed
  -> 严格清理官方运行时临时产物
  -> 外部 wrapper 收回结果并验证哈希
  -> Stage1 外部后处理统一小节、速度和调性
```

队列目录：

```text
$HOME/musecoco_runtime/task_queue/
$HOME/musecoco_runtime/task_running/
$HOME/musecoco_runtime/task_failed/
$HOME/musecoco_runtime/task_done/
```

任务通过隐藏的 `.incoming-*` 暂存目录完整复制并验证，再用同一文件系统上的原子重命名进入 `task_queue`。worker 同样以原子重命名把整个任务目录移到 running、done 或 failed，不处理裸 JSON。

## 单样本任务包契约

每个任务目录必须且只能包含：

```text
task_xxx/
  predict_attributes.json
  predict.json
  predict_index.json
  audit.json
```

约束：

- `predict_attributes.json` 必须按官方 `att_key.json` 的 60 个键及其顺序排列，每个键只含一个样本；
- 每个属性向量必须有正确长度并且为 one-hot；
- `predict.json` 必须只含对应主题的一条真实文本，不允许 wrapper 临时伪造；
- `predict_index.json` 只记录这个样本的原始索引、故事、主题家族和来源；
- `audit.json` 必须记录包内三个输入文件的 SHA-256，并包含本文规定的概率和官方代码边界标记；
- 多主题批量输出必须先拆成多个单样本任务包，每个包对应一次 MuseCoco 生成。

任务名包含内容哈希；同一任务重复入队会报告 `already_present`，不同内容不得覆盖同名任务。

## deterministic_one_hot 契约

官方 `stage2_pre.py` 强制读取：

```text
1-text2attribute_model/tmp/predict_attributes.json
1-text2attribute_model/tmp/softmax_probs.json
1-text2attribute_model/data/predict.json
```

本项目跳过官方 Text-to-Attribute 模型，所以 worker 将任务包的 `predict_attributes.json` 按字节复制为 `softmax_probs.json`，并在继续前验证两者 SHA-256 完全一致。审计固定包含：

```json
{
  "probability_source": "deterministic_one_hot",
  "uses_stage1_model": false,
  "uses_official_stage2_pre": true,
  "uses_official_stage2_generation": true
}
```

这里的 `softmax_probs.json` 只是满足官方预处理输入形状的确定性控制表达，不是真实 softmax，也不代表置信度。

## 严格清理规程

清理是每次正式运行必须执行的安全步骤，不是可选维护操作。wrapper 只清理固定白名单中的运行时产物：

```text
1-text2attribute_model/tmp/
1-text2attribute_model/data/predict.json
1-text2attribute_model/infer_test.bin
2-attribute2music_model/infer_input/infer_test.bin
2-attribute2music_model/generation/.../infer_test/
官方推理日志目录
```

规程如下：

1. 队列启动时，先把白名单中所有现存内容完整快照到 `$HOME/MuseCoco_outputs/runtime_state_backups/`，生成 SHA-256 清单，然后才清理。
2. 每个任务安装前再次快照并清理，确保不继承上一个任务或人工试验的状态。
3. 成功或失败后，先把当次运行时状态快照进该任务的输出目录，再清理。
4. 单次生成失败且清理验证通过时，允许重试同一任务；默认最多尝试 3 次，每次都创建独立结果目录、日志和清理快照。
5. 每项清理都采用固定绝对路径和父目录校验；不接受任务输入指定清理路径，不使用通配删除。
6. 清理后逐项验证：临时文件、生成目录和日志目录均不存在，`tmp/` 只保留为空目录。清理失败属于基础设施异常，会停止队列，不进入下一次重试。
7. 单个任务用尽重试次数后移到 `task_failed`；其他有效任务可以继续处理。队列最终报告 `PARTIAL_FAILURE` 并返回非零退出码。
8. 未完成的 `task_running` 不会自动继续未知的那一次尝试；下次启动时会移动到 `task_failed`，避免中断点状态不明时直接续跑。需要重试时，从原始 Stage1 任务包重新入队。

不得先清理再备份。wrapper 不会删除 `task_done`、`task_failed` 或 `$HOME/MuseCoco_outputs` 中的结果和审计。

## 输出与审计

每次运行输出到：

```text
$HOME/MuseCoco_outputs/from_task_<时间戳>_<task_id>/
```

其中包括：任务包输入备份、实际安装的 `predict_attributes.json`、`softmax_probs.json`、预处理与推理日志、`infer_test.bin` 哈希、原始 `result.remi.raw.txt`、正式 `result.remi.txt`、必须存在的 `result.mid`、运行时快照和 `result_audit.json`。

`result_audit.json` 记录官方关键文件实际哈希、预置哈希和输出完整性门禁。每个队列任务另在 `queue_logs/` 生成 `*.attempts.json`，记录最大次数、每次返回码和对应日志。任务包/官方文件/环境验证失败或清理不完整属于流程基础设施冲突，会 fail closed；普通 Coco 生成失败（包括非法 REMI 或缺失 MIDI）则按上限重试，耗尽后移到 `task_failed`。

## 备份与官方代码保护

本次实施前备份位于：

```text
<repository-root>\backups\musecoco-task-queue-20260720-212517
```

`BACKUP_MANIFEST.sha256` 可校验备份，`BACKUP_NOTES.md` 记录来源。官方 `stage2_pre.py`、`att_key.json`、Stage2 启动脚本及核心 Python 文件只做只读备份和哈希冻结；自动化不会写入这些文件。若未来官方文件确需修改，必须停止并取得用户单独批准。

## 中断与恢复

前台运行时按 `Ctrl+C`。中断可能留下 `task_running` 和官方临时文件；不要手工删除。下一次运行会先把 running 残留移到 failed，再对临时状态做备份和严格清理。确认失败任务的输出、日志和备份后，修正原因并从原始 Stage1 任务包重新入队。
