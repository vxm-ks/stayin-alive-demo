import { useEffect, useState } from 'react'
import './demo-case.css'

type Lang = 'zh' | 'en'
type Localized = { zh: string; en: string }
type MaybeAsset = { src: string | null; label: Localized }

type DemoManifest = {
  schema_version: string
  status: 'placeholder' | 'ready'
  case_id: string
  title: Localized
  subtitle: Localized
  story: { text: string | null }
  audio: {
    original_heartbeat: MaybeAsset
    regularized_heartbeat: MaybeAsset
    final_mix: MaybeAsset
  }
  figures: {
    regularized_time_energy: MaybeAsset
    heartbeat_bar_time_energy: MaybeAsset
    stage1_midi_previews: MaybeAsset[]
    stage2_midi_preview: MaybeAsset
  }
  downloads: {
    stage1_midis: Array<{ src: string | null; label: string }>
    stage2_midi: { src: string | null; label: string }
  }
  metrics: {
    form: string | null
    bpm: number | null
    tonality: string | null
    bars: number | null
    final_lufs: number | null
    true_peak_dbtp: number | null
  }
}

const fallback: DemoManifest = {
  schema_version: 'legasynth-demo-case-v1',
  status: 'placeholder',
  case_id: 'case-01',
  title: { zh: '从心音与故事到一首完整音乐', en: 'From Heart Sound and Story to a Complete Piece' },
  subtitle: {
    zh: '一条可追溯的生成链路：真实心音提取、叙事规划、主题生成、乐曲补全与逐事件混音。',
    en: 'A traceable pipeline for heartbeat extraction, story planning, theme generation, MIDI completion, and event-level mixing.',
  },
  story: { text: null },
  audio: {
    original_heartbeat: { src: null, label: { zh: '原始心音', en: 'Original heart sound' } },
    regularized_heartbeat: { src: null, label: { zh: '规则化心音', en: 'Regularized heartbeat' } },
    final_mix: { src: null, label: { zh: '最终混音', en: 'Final mix' } },
  },
  figures: {
    regularized_time_energy: { src: null, label: { zh: '规则化心音：时间—能量图', en: 'Regularized heartbeat: time–energy plot' } },
    heartbeat_bar_time_energy: { src: null, label: { zh: '单小节心跳：时间—能量图', en: 'One-bar heartbeat: time–energy plot' } },
    stage1_midi_previews: [
      { src: null, label: { zh: '主题 A', en: 'Theme A' } },
      { src: null, label: { zh: '主题 B', en: 'Theme B' } },
      { src: null, label: { zh: '主题 C', en: 'Theme C' } },
    ],
    stage2_midi_preview: { src: null, label: { zh: '完整 MIDI 乐谱', en: 'Completed MIDI score' } },
  },
  downloads: {
    stage1_midis: [],
    stage2_midi: { src: null, label: 'complete.mid' },
  },
  metrics: {
    form: null, bpm: null, tonality: null, bars: null, final_lufs: null, true_peak_dbtp: null,
  },
}

const content = {
  zh: {
    brand: 'stayin’ alive',
    back: '返回创作界面',
    badge: '完整案例 · 数据待载入',
    trace: ['原始输入', '心音处理', '故事规划', '主题生成', '乐曲补全', '最终混音'],
    inputTitle: '01 · 输入与心音素材',
    inputDesc: '展示同一案例的故事文本、原始心音及 Stage 1 生成的规则化真实心音。',
    story: '故事输入',
    waitingStory: '真实故事文本将在数据整理后载入。',
    figuresTitle: '02 · 心音处理证据',
    figuresDesc: '时间—能量图用于检查事件检测、S1/S2 排列及处理前后的能量变化。',
    plansTitle: '03 · 规划与控制文件',
    plansDesc: '仅展示关键字段摘录；完整 JSON 仍保留在可追溯任务包中。',
    midiTitle: '04 · 从主题到完整乐曲',
    midiDesc: 'Stage 1 生成主题动机，Stage 2 按结构计划装配并通过 MIDI-GPT 完成乐曲。',
    stage1: 'Stage 1 · 主题 MIDI',
    stage2: 'Stage 2 · 完整 MIDI',
    outputTitle: '05 · 最终渲染与结果',
    outputDesc: 'Stage 3 渲染 MIDI，并通过逐事件响度平衡把真实心音清晰混入音乐。',
    waiting: '待载入',
    listen: '音频将在真实案例导出后显示',
    image: '图像将在真实案例导出后显示',
    form: '曲式', bpm: '速度', tonality: '调性', bars: '小节数', lufs: '最终响度', peak: '真峰值',
    provenance: '可追溯性与隐私',
    provenanceText: '公开版本将只包含经授权或匿名化的案例素材。展示文件由单个任务目录导出，并通过清单保持故事、图像、MIDI、JSON 与最终音频的一一对应。',
  },
  en: {
    brand: 'stayin’ alive',
    back: 'Back to studio',
    badge: 'Complete case · Data pending',
    trace: ['Raw input', 'Heartbeat processing', 'Story planning', 'Theme generation', 'MIDI completion', 'Final mix'],
    inputTitle: '01 · Inputs and Heartbeat Material',
    inputDesc: 'The story, original recording, and regularized real-heartbeat material from one traceable case.',
    story: 'Story input',
    waitingStory: 'The real story text will be loaded after case curation.',
    figuresTitle: '02 · Heartbeat Processing Evidence',
    figuresDesc: 'Time–energy plots expose event detection, S1/S2 placement, and energy changes after processing.',
    plansTitle: '03 · Planning and Control Artifacts',
    plansDesc: 'Only selected fields are shown; complete JSON files remain in the traceable job package.',
    midiTitle: '04 · From Themes to a Complete Piece',
    midiDesc: 'Stage 1 produces themes; Stage 2 assembles the form and completes the score with MIDI-GPT.',
    stage1: 'Stage 1 · Theme MIDI',
    stage2: 'Stage 2 · Complete MIDI',
    outputTitle: '05 · Rendering and Final Result',
    outputDesc: 'Stage 3 renders the MIDI and balances real heartbeat events against the music.',
    waiting: 'Pending',
    listen: 'Audio appears after the real case is exported',
    image: 'Image appears after the real case is exported',
    form: 'Form', bpm: 'Tempo', tonality: 'Tonality', bars: 'Bars', lufs: 'Final loudness', peak: 'True peak',
    provenance: 'Traceability and privacy',
    provenanceText: 'The public version will contain only authorized or anonymized materials. Every asset is exported from one job directory, while the manifest keeps the story, figures, MIDI, JSON, and final audio aligned.',
  },
}

const jsonCards = [
  ['form_scale_decision.json', ['section_count', 'form', 'theme_reuse', 'global_bpm', 'global_tonality']],
  ['content_plan.json', ['sections', 'emotion', 'energy', 'tension', 'theme_id']],
  ['heartbeat_processing_plan.json', ['target_bpm', 'meter', 'event_pattern', 'beat_peak']],
  ['stage2_plan.json', ['sections', 'source_theme', 'motif_bars', 'fill_bars', 'protected_track']],
]

function resolveAsset(path: string | null) {
  if (!path) return null
  return `${import.meta.env.BASE_URL}${path.replace(/^\/+/, '')}`
}

function AudioCard({ asset, lang, empty }: { asset: MaybeAsset; lang: Lang; empty: string }) {
  const source = resolveAsset(asset.src)
  return <article className="demoAudioCard">
    <div className="demoAudioIcon"><span/><span/></div>
    <div>
      <h3>{asset.label[lang]}</h3>
      {source ? <audio controls preload="metadata" src={source}/> : <p className="demoPending">{empty}</p>}
    </div>
  </article>
}

function FigureCard({ asset, lang, empty, compact = false }: { asset: MaybeAsset; lang: Lang; empty: string; compact?: boolean }) {
  const source = resolveAsset(asset.src)
  return <figure className={`demoFigure ${compact ? 'compact' : ''}`}>
    {source
      ? <img src={source} alt={asset.label[lang]}/>
      : <div className="demoPlotPlaceholder" aria-label={empty}>
          <div className="demoPlotGrid"/><div className="demoPlotWave"/>
          <span>{empty}</span>
        </div>}
    <figcaption>{asset.label[lang]}</figcaption>
  </figure>
}

export default function DemoCase({ lang, setLang }: { lang: Lang; setLang: (lang: Lang) => void }) {
  const [manifest, setManifest] = useState<DemoManifest>(fallback)
  const t = content[lang]

  useEffect(() => {
    fetch(`${import.meta.env.BASE_URL}demo/case-01/demo_manifest.json`)
      .then((response) => response.ok ? response.json() : Promise.reject())
      .then((data: DemoManifest) => setManifest(data))
      .catch(() => setManifest(fallback))
  }, [])

  const metrics = [
    [t.form, manifest.metrics.form],
    [t.bpm, manifest.metrics.bpm ? `${manifest.metrics.bpm} BPM` : null],
    [t.tonality, manifest.metrics.tonality],
    [t.bars, manifest.metrics.bars],
    [t.lufs, manifest.metrics.final_lufs ? `${manifest.metrics.final_lufs} LUFS` : null],
    [t.peak, manifest.metrics.true_peak_dbtp ? `${manifest.metrics.true_peak_dbtp} dBTP` : null],
  ]

  return <main className="demoCase">
    <header className="demoHeader">
      <a className="demoBrand" href={import.meta.env.BASE_URL}><i/><span>{t.brand}</span></a>
      <a className="demoBack" href={import.meta.env.BASE_URL}>← {t.back}</a>
      <div className="demoLanguage">
        <button className={lang === 'zh' ? 'selected' : ''} onClick={() => setLang('zh')}>中</button>
        <span/>
        <button className={lang === 'en' ? 'selected' : ''} onClick={() => setLang('en')}>EN</button>
      </div>
    </header>

    <section className="demoHero">
      <p className="demoBadge">{t.badge}</p>
      <h1>{manifest.title[lang]}</h1>
      <p>{manifest.subtitle[lang]}</p>
      <div className="demoTrace">
        {t.trace.map((label, index) => <div key={label}>
          <b>{String(index + 1).padStart(2, '0')}</b><span>{label}</span>
        </div>)}
      </div>
    </section>

    <section className="demoSection">
      <div className="demoSectionHead"><h2>{t.inputTitle}</h2><p>{t.inputDesc}</p></div>
      <div className="demoInputGrid">
        <article className="demoStory">
          <span>{t.story}</span>
          <p>{manifest.story.text || t.waitingStory}</p>
        </article>
        <div className="demoAudioStack">
          <AudioCard asset={manifest.audio.original_heartbeat} lang={lang} empty={t.listen}/>
          <AudioCard asset={manifest.audio.regularized_heartbeat} lang={lang} empty={t.listen}/>
        </div>
      </div>
    </section>

    <section className="demoSection demoTint">
      <div className="demoSectionHead"><h2>{t.figuresTitle}</h2><p>{t.figuresDesc}</p></div>
      <div className="demoFigureGrid">
        <FigureCard asset={manifest.figures.regularized_time_energy} lang={lang} empty={t.image}/>
        <FigureCard asset={manifest.figures.heartbeat_bar_time_energy} lang={lang} empty={t.image}/>
      </div>
    </section>

    <section className="demoSection">
      <div className="demoSectionHead"><h2>{t.plansTitle}</h2><p>{t.plansDesc}</p></div>
      <div className="demoJsonGrid">
        {jsonCards.map(([name, keys]) => <article className="demoJson" key={name as string}>
          <div><i/><i/><i/><span>{name as string}</span></div>
          <pre>{`{\n${(keys as string[]).map((key) => `  "${key}": {{…}}`).join(',\n')}\n}`}</pre>
        </article>)}
      </div>
    </section>

    <section className="demoSection demoTint">
      <div className="demoSectionHead"><h2>{t.midiTitle}</h2><p>{t.midiDesc}</p></div>
      <h3 className="demoSubhead">{t.stage1}</h3>
      <div className="demoMidiThemes">
        {manifest.figures.stage1_midi_previews.map((asset) =>
          <FigureCard key={asset.label.en} asset={asset} lang={lang} empty={t.image} compact/>)}
      </div>
      <h3 className="demoSubhead">{t.stage2}</h3>
      <FigureCard asset={manifest.figures.stage2_midi_preview} lang={lang} empty={t.image}/>
    </section>

    <section className="demoSection demoFinal">
      <div className="demoSectionHead"><h2>{t.outputTitle}</h2><p>{t.outputDesc}</p></div>
      <AudioCard asset={manifest.audio.final_mix} lang={lang} empty={t.listen}/>
      <div className="demoMetrics">
        {metrics.map(([label, value]) => <div key={String(label)}>
          <span>{label}</span><strong>{value ?? t.waiting}</strong>
        </div>)}
      </div>
      <aside className="demoProvenance"><b>{t.provenance}</b><p>{t.provenanceText}</p></aside>
    </section>
  </main>
}
