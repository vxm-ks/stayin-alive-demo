import { useEffect, useMemo, useRef, useState } from 'react'

type Lang = 'zh' | 'en'
type View = 'create' | 'generating' | 'result'
type TaskState = {
  task_id: string
  status: string
  current_stage: string
  progress: number
  audio_ready: boolean
  failure?: { message?: string } | null
}

const copy = {
  zh: {
    navCreate: '创作', navWork: '作品', eyebrow: '让生命留下旋律',
    titleA: '每一次心跳，', titleB: '都有自己的故事。',
    intro: '上传一段心音，写下一个故事。LegaSynth 会把真实的生命节律，编织成只属于你的音乐。',
    step1: '01 · 心跳样本', step2: '02 · 你的故事', wavOnly: '支持 WAV · 建议 10–60 秒',
    upload: '拖入心音文件', browse: '或点击选择本地 WAV', replace: '点击更换文件',
    storyLabel: '写下你想让音乐讲述的故事', placeholder: '例如：那年夏天，我们沿着海岸一直走，晚风带着盐的味道……',
    hint: '一段具体、有情绪变化的故事，会让音乐更有层次。', demo: '演示模式',
    demoHint: '快速生成，不调用模型', generate: '开始生成音乐', privacy: '你的心音仅用于本次创作，不会离开本机。',
    generating: '正在聆听你的故事', generatingSub: '我们正在把心跳的节律、故事的情绪与音乐结构融合。请保持页面开启。',
    stages: ['解析心跳', '理解故事', '谱写旋律', '融合生命节律', '完成作品'],
    stageDesc: ['提取真实心音事件', '分析情绪与叙事弧线', '构建主题与完整乐章', '渲染真实 S1 / S2 音色', '为你保存这一刻'],
    resultEyebrow: '你的心跳，成为了音乐', resultTitle: '生命的回声', resultSub: '一段由真实心跳与故事共同生成的音乐作品',
    play: '播放', pause: '暂停', download: '下载 WAV', again: '创作新作品', error: '生成遇到问题', retry: '返回修改',
    uploadError: '请选择 WAV 格式的心音文件。', required: '请先上传心音并输入故事。', serverError: '无法连接生成服务，请确认后端已启动。',
  },
  en: {
    navCreate: 'Create', navWork: 'Your piece', eyebrow: 'A melody made from life',
    titleA: 'Every heartbeat', titleB: 'has a story to tell.',
    intro: 'Upload a heartbeat and write a story. LegaSynth weaves a real human rhythm into music that belongs only to you.',
    step1: '01 · Heartbeat', step2: '02 · Your story', wavOnly: 'WAV · 10–60 seconds recommended',
    upload: 'Drop your heartbeat here', browse: 'or choose a WAV from your device', replace: 'Click to choose another file',
    storyLabel: 'What story should this music tell?', placeholder: 'For example: That summer, we walked along the coast until dusk, with salt carried on the wind…',
    hint: 'A vivid story with emotional movement creates a richer musical arc.', demo: 'Demo mode',
    demoHint: 'Fast preview without models', generate: 'Create my music', privacy: 'Your heartbeat stays on this device and is used only for this piece.',
    generating: 'Listening to your story', generatingSub: 'We are bringing heartbeat, emotion and musical form together. Please keep this page open.',
    stages: ['Reading heartbeat', 'Understanding story', 'Writing melody', 'Blending life rhythm', 'Your piece is ready'],
    stageDesc: ['Extracting authentic heart events', 'Tracing emotion and narrative', 'Building themes and a complete score', 'Rendering real S1 / S2 timbres', 'Preserving this moment for you'],
    resultEyebrow: 'Your heartbeat became music', resultTitle: 'Echoes of Life', resultSub: 'An original piece created from a real heartbeat and your story',
    play: 'Play', pause: 'Pause', download: 'Download WAV', again: 'Create another', error: 'Something interrupted the music', retry: 'Go back',
    uploadError: 'Please choose a WAV heartbeat recording.', required: 'Upload a heartbeat and enter a story first.', serverError: 'Cannot reach the generation service. Make sure the API is running.',
  },
}

const stageOrder = ['heartbeat_stage1', 'story_and_musecoco', 'stage2_midigpt', 'stage3_render', 'completed']

function LogoMark() {
  return <svg viewBox="0 0 44 44" aria-hidden="true"><path d="M5 23h7l3-10 6 21 5-24 5 14h8"/><circle cx="22" cy="22" r="19"/></svg>
}

function Waveform({ src, lang }: { src: string; lang: Lang }) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const audioRef = useRef<HTMLAudioElement>(null)
  const beatsRef = useRef<number[]>([])
  const historyRef = useRef<number[]>([])
  const sampleTimeRef = useRef(0)
  const beatIndexRef = useRef(0)
  const playingRef = useRef(false)
  const [playing, setPlaying] = useState(false)
  const [duration, setDuration] = useState(0)
  const [current, setCurrent] = useState(0)
  const [detected, setDetected] = useState(0)
  const t = copy[lang]

  useEffect(() => {
    let cancelled = false
    fetch(src).then(response => response.arrayBuffer()).then(async buffer => {
      const audioContext = new AudioContext()
      const decoded = await audioContext.decodeAudioData(buffer)
      await audioContext.close()
      if (cancelled) return
      const channels = Array.from({ length: decoded.numberOfChannels }, (_, index) => decoded.getChannelData(index))
      const fps = 80, step = Math.max(1, Math.floor(decoded.sampleRate / fps)), envelope: number[] = []
      for (let start = 0; start < decoded.length; start += step) {
        let sum = 0, count = 0
        for (let index = start; index < Math.min(start + step, decoded.length); index += 2) {
          let sample = 0
          for (const channel of channels) sample += channel[index] / channels.length
          sum += sample * sample; count++
        }
        envelope.push(Math.sqrt(sum / Math.max(1, count)))
      }
      const smooth = envelope.map((_, index) => {
        let sum = 0, count = 0
        for (let cursor = Math.max(0, index - 2); cursor <= Math.min(envelope.length - 1, index + 2); cursor++) { sum += envelope[cursor]; count++ }
        return sum / count
      })
      const scores = smooth.map((value, index) => {
        let local = 0, count = 0
        for (let cursor = Math.max(0, index - 32); cursor < index - 5; cursor++) { local += smooth[cursor]; count++ }
        return Math.max(0, value - local / Math.max(1, count) * .72)
      })
      const sorted = [...scores].sort((a, b) => a - b), threshold = sorted[Math.floor(sorted.length * .86)] || 0
      const candidates: { time: number; score: number }[] = []
      for (let index = 3; index < scores.length - 3; index++) {
        if (scores[index] >= threshold && scores[index] >= scores[index - 1] && scores[index] > scores[index + 1]) candidates.push({ time: index / fps, score: scores[index] })
      }
      const selected: { time: number; score: number }[] = []
      for (const candidate of candidates) {
        const previous = selected.at(-1)
        if (!previous || candidate.time - previous.time >= .42) selected.push(candidate)
        else if (candidate.score > previous.score) selected[selected.length - 1] = candidate
      }
      beatsRef.current = selected.map(item => item.time)
      setDetected(selected.length)
    }).catch(() => { beatsRef.current = []; setDetected(0) })
    return () => { cancelled = true }
  }, [src])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    let animation = 0
    const resize = () => {
      const rect = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1
      canvas.width = Math.round(rect.width * dpr); canvas.height = Math.round(rect.height * dpr)
      historyRef.current = Array(Math.ceil(rect.width) + 2).fill(0)
    }
    const ecg = (phase: number) => {
      const gaussian = (center: number, width: number, amplitude: number) => amplitude * Math.exp(-Math.pow((phase - center) / width, 2))
      return gaussian(.12, .042, .105) + gaussian(.265, .012, -.13) + gaussian(.292, .010, 1.38) + gaussian(.323, .017, -.34) + gaussian(.58, .075, .16)
    }
    const seek = () => {
      const position = audioRef.current?.currentTime || 0, beats = beatsRef.current
      beatIndexRef.current = beats.findIndex(time => time >= position)
      if (beatIndexRef.current < 0) beatIndexRef.current = beats.length
      sampleTimeRef.current = position
      historyRef.current.fill(0)
    }
    const draw = () => {
      const audio = audioRef.current, rect = canvas.getBoundingClientRect(), context = canvas.getContext('2d')!, dpr = window.devicePixelRatio || 1
      if (audio && playingRef.current) {
        const position = audio.currentTime, beats = beatsRef.current
        if (position < sampleTimeRef.current - .05) seek()
        let guard = 0
        while (sampleTimeRef.current + 1 / 60 <= position && guard++ < 600) {
          sampleTimeRef.current += 1 / 60
          while (beatIndexRef.current < beats.length && beats[beatIndexRef.current] < sampleTimeRef.current - .72) beatIndexRef.current++
          let value = Math.sin(sampleTimeRef.current * 7.1) * .0032 + Math.sin(sampleTimeRef.current * 3.4) * .0018
          const beat = beats[beatIndexRef.current]
          if (beat !== undefined && sampleTimeRef.current >= beat && sampleTimeRef.current < beat + .72) value += ecg((sampleTimeRef.current - beat) / .72)
          historyRef.current.push(value); historyRef.current.shift()
        }
      }
      context.setTransform(dpr, 0, 0, dpr, 0, 0); context.clearRect(0, 0, rect.width, rect.height)
      context.lineWidth = 1; context.strokeStyle = 'rgba(230,106,114,.035)'
      for (let x = 0; x < rect.width; x += 22) { context.beginPath(); context.moveTo(x, 0); context.lineTo(x, rect.height); context.stroke() }
      for (let y = 0; y < rect.height; y += 22) { context.beginPath(); context.moveTo(0, y); context.lineTo(rect.width, y); context.stroke() }
      const middle = rect.height * .55, amplitude = Math.min(rect.height * .36, 72)
      context.lineJoin = 'round'; context.lineCap = 'round'
      for (const [width, alpha, blur] of [[11, .055, 22], [5, .17, 12], [2, 1, 5]] as const) {
        context.beginPath(); historyRef.current.forEach((value, x) => x ? context.lineTo(x, middle - value * amplitude) : context.moveTo(x, middle - value * amplitude))
        context.strokeStyle = `rgba(230,106,114,${alpha})`; context.lineWidth = width; context.shadowColor = '#e05d68'; context.shadowBlur = blur; context.stroke()
      }
      context.shadowBlur = 0
      animation = requestAnimationFrame(draw)
    }
    resize(); window.addEventListener('resize', resize); audioRef.current?.addEventListener('seeked', seek); animation = requestAnimationFrame(draw)
    return () => { cancelAnimationFrame(animation); window.removeEventListener('resize', resize); audioRef.current?.removeEventListener('seeked', seek) }
  }, [src])

  const format = (seconds: number) => `${Math.floor(seconds / 60)}:${Math.floor(seconds % 60).toString().padStart(2, '0')}`
  const toggle = () => {
    const audio = audioRef.current!; audio.paused ? audio.play() : audio.pause()
  }
  return <div className="player">
    <audio ref={audioRef} src={src} onLoadedMetadata={e => { setDuration(e.currentTarget.duration); sampleTimeRef.current = 0 }} onTimeUpdate={e => setCurrent(e.currentTarget.currentTime)} onPlay={() => { playingRef.current = true; setPlaying(true) }} onPause={() => { playingRef.current = false; setPlaying(false) }} onEnded={() => { playingRef.current = false; setPlaying(false) }}/>
    <div className="playerTop">
      <button className="playButton" onClick={toggle} aria-label={playing ? t.pause : t.play}>
        {playing ? <span className="pauseIcon"/> : <span className="playIcon"/>}
      </button>
      <div className="trackMeta"><b>{t.resultTitle}</b><span>LegaSynth · 2026 · {detected} {lang === 'zh' ? '次心跳' : 'heartbeats'}</span></div>
      <span className="time">{format(current)} / {format(duration || 0)}</span>
    </div>
    <div className="waveWrap" onClick={e => { const rect = e.currentTarget.getBoundingClientRect(); if (audioRef.current && duration) audioRef.current.currentTime = ((e.clientX - rect.left) / rect.width) * duration }}>
      <canvas ref={canvasRef}/><div className="ecgScan"/>
    </div>
  </div>
}

function WaveDemo({ lang, setLang }: { lang: Lang; setLang: (lang: Lang) => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [audioUrl, setAudioUrl] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  useEffect(() => {
    if (!file) { setAudioUrl(''); return }
    const url = URL.createObjectURL(file); setAudioUrl(url)
    return () => URL.revokeObjectURL(url)
  }, [file])
  const zh = lang === 'zh'
  return <main className="app demoApp">
    <div className="ambient ambientOne"/><div className="ambient ambientTwo"/><div className="grain"/>
    <header>
      <a className="brand" href="/"><LogoMark/><span>LegaSynth</span></a>
      <span className="demoBadge">WAVEFORM LAB · 波形检验台</span>
      <div className="language"><button className={lang === 'zh' ? 'selected' : ''} onClick={() => setLang('zh')}>中</button><i/><button className={lang === 'en' ? 'selected' : ''} onClick={() => setLang('en')}>EN</button></div>
    </header>
    <section className="waveDemoPage">
      <div className="eyebrow"><span/><b>HEARTBEAT VISUALIZATION</b><span/></div>
      <h1>{zh ? '听见声音，看见心跳。' : 'Hear the sound. See the heartbeat.'}</h1>
      <p>{zh ? '选择一段已有音乐，在播放过程中实时检验动态波形效果。音频仅在当前浏览器中读取。' : 'Choose an existing track and inspect the animated waveform while it plays. Audio stays entirely in your browser.'}</p>
      <input ref={inputRef} hidden type="file" accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg" onChange={e => setFile(e.target.files?.[0] || null)}/>
      {!file ? <button className="demoDrop" onClick={() => inputRef.current?.click()} onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); setFile(e.dataTransfer.files[0] || null) }}>
        <span className="demoHeart">♥<i/><i/></span>
        <b>{zh ? '选择或拖入一段音乐' : 'Choose or drop a track'}</b>
        <small>WAV · MP3 · FLAC · M4A · OGG</small>
      </button> : <div className="demoResult">
        <div className="demoFile"><span className="filePulse"><i/><i/><i/><i/><i/></span><div><b>{file.name}</b><small>{(file.size / 1024 / 1024).toFixed(2)} MB · {zh ? '仅本地读取' : 'Local only'}</small></div><button onClick={() => inputRef.current?.click()}>{zh ? '更换音乐' : 'Change track'}</button></div>
        {audioUrl && <Waveform src={audioUrl} lang={lang}/>}
        <div className="demoGuide"><span>01</span><p><b>{zh ? '点击播放' : 'Press play'}</b><small>{zh ? '波形会跟随实际音频振幅显示' : 'The waveform follows the actual audio amplitude'}</small></p><i/><span>02</span><p><b>{zh ? '拖动定位' : 'Seek anywhere'}</b><small>{zh ? '点击波形任意位置跳转' : 'Click anywhere on the waveform to seek'}</small></p><i/><span>03</span><p><b>{zh ? '观察心跳' : 'Watch it pulse'}</b><small>{zh ? '亮色区域表示已经播放的部分' : 'The illuminated area shows playback progress'}</small></p></div>
      </div>}
      <a className="backStudio" href="/">← {zh ? '返回创作界面' : 'Back to studio'}</a>
    </section>
  </main>
}

export default function App() {
  const [lang, setLang] = useState<Lang>('zh')
  const [view, setView] = useState<View>('create')
  const [file, setFile] = useState<File | null>(null)
  const [story, setStory] = useState('')
  const [demo, setDemo] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [task, setTask] = useState<TaskState | null>(null)
  const [message, setMessage] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const t = copy[lang]
  const audioUrl = task ? `/api/tasks/${task.task_id}/audio` : ''

  const stageIndex = useMemo(() => task ? Math.max(0, stageOrder.indexOf(task.current_stage)) : 0, [task])

  useEffect(() => {
    if (!task || view !== 'generating' || ['COMPLETED', 'FAILED'].includes(task.status)) return
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch(`/api/tasks/${task.task_id}`)
        const next: TaskState = await response.json(); setTask(next)
        if (next.status === 'COMPLETED') setTimeout(() => setView('result'), 650)
        if (next.status === 'FAILED') setMessage(next.failure?.message || t.serverError)
      } catch { setMessage(t.serverError) }
    }, 1200)
    return () => clearInterval(timer)
  }, [task, view, t.serverError])

  const choose = (candidate?: File) => {
    if (!candidate) return
    if (!candidate.name.toLowerCase().endsWith('.wav')) { setMessage(t.uploadError); return }
    setFile(candidate); setMessage('')
  }
  const submit = async () => {
    if (!file || !story.trim()) { setMessage(t.required); return }
    setMessage(''); setView('generating')
    const form = new FormData(); form.append('heartbeat', file); form.append('story', story); form.append('dry_run', String(demo))
    try {
      const response = await fetch('/api/tasks', { method: 'POST', body: form })
      const data = await response.json()
      if (!response.ok) throw new Error(data.detail || t.serverError)
      setTask(data)
    } catch (error) { setMessage(error instanceof Error ? error.message : t.serverError) }
  }
  const reset = () => { setView('create'); setTask(null); setFile(null); setStory(''); setMessage('') }

  if (window.location.pathname === '/wave-demo') return <WaveDemo lang={lang} setLang={setLang}/>

  return <main className={`app view-${view}`}>
    <div className="ambient ambientOne"/><div className="ambient ambientTwo"/><div className="grain"/>
    <header>
      <button className="brand" onClick={reset}><LogoMark/><span>LegaSynth</span></button>
      <nav><button className={view === 'create' ? 'active' : ''} onClick={reset}>{t.navCreate}</button><button className={view === 'result' ? 'active' : ''}>{t.navWork}</button></nav>
      <div className="language"><button className={lang === 'zh' ? 'selected' : ''} onClick={() => setLang('zh')}>中</button><i/><button className={lang === 'en' ? 'selected' : ''} onClick={() => setLang('en')}>EN</button></div>
    </header>

    {view === 'create' && <section className="createPage">
      <div className="hero">
        <div className="eyebrow"><span/><b>{t.eyebrow}</b><span/></div>
        <h1>{t.titleA}<em>{t.titleB}</em></h1><p>{t.intro}</p>
      </div>
      <div className="studioCard">
        <div className="field uploadField">
          <div className="fieldHead"><b>{t.step1}</b><span>{t.wavOnly}</span></div>
          <button className={`dropzone ${dragging ? 'dragging' : ''} ${file ? 'hasFile' : ''}`} onClick={() => inputRef.current?.click()} onDragOver={e => { e.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={e => { e.preventDefault(); setDragging(false); choose(e.dataTransfer.files[0]) }}>
            <input ref={inputRef} type="file" accept=".wav,audio/wav" hidden onChange={e => choose(e.target.files?.[0])}/>
            {file ? <><span className="filePulse"><i/><i/><i/><i/><i/></span><strong>{file.name}</strong><small>{(file.size / 1024 / 1024).toFixed(1)} MB · {t.replace}</small></> : <><span className="heartIcon">♥<i/></span><strong>{t.upload}</strong><small>{t.browse}</small></>}
          </button>
        </div>
        <div className="divider"><span>×</span></div>
        <div className="field storyField">
          <div className="fieldHead"><b>{t.step2}</b><span>{story.length} / 8000</span></div>
          <label>{t.storyLabel}</label>
          <textarea value={story} maxLength={8000} placeholder={t.placeholder} onChange={e => setStory(e.target.value)}/>
          <p className="tip"><span>✦</span>{t.hint}</p>
        </div>
        <div className="cardFooter">
          <label className="demoToggle"><input type="checkbox" checked={demo} onChange={e => setDemo(e.target.checked)}/><span className="switch"><i/></span><b>{t.demo}</b><small>{t.demoHint}</small></label>
          <button className="generate" onClick={submit}><span>{t.generate}</span><i>→</i></button>
        </div>
      </div>
      {message && <p className="errorMessage">{message}</p>}
      <p className="privacy"><span>◇</span>{t.privacy}</p>
    </section>}

    {view === 'generating' && <section className="generatingPage">
      <div className="orb"><div className="orbCore"><LogoMark/></div><i/><i/><i/></div>
      <div className="eyebrow"><span/><b>{Math.round(task?.progress || 4)}%</b><span/></div>
      <h2>{message ? t.error : t.generating}</h2><p>{message || t.generatingSub}</p>
      <div className="progressLine"><i style={{ width: `${task?.progress || 4}%` }}/><b style={{ left: `${task?.progress || 4}%` }}/></div>
      <div className="stageList">{t.stages.map((label, i) => <div key={label} className={`${i < stageIndex ? 'done' : ''} ${i === stageIndex ? 'current' : ''}`}><span>{i < stageIndex ? '✓' : String(i + 1).padStart(2, '0')}</span><b>{label}</b><small>{t.stageDesc[i]}</small></div>)}</div>
      {message && <button className="secondaryButton" onClick={() => setView('create')}>{t.retry}</button>}
    </section>}

    {view === 'result' && task && <section className="resultPage">
      <div className="resultArt"><div className="vinyl"><i/><div className="vinylLabel"><LogoMark/></div></div><div className="orbit orbitA"/><div className="orbit orbitB"/></div>
      <div className="eyebrow"><span/><b>{t.resultEyebrow}</b><span/></div><h2>{t.resultTitle}</h2><p>{t.resultSub}</p>
      <Waveform src={audioUrl} lang={lang}/>
      <div className="resultActions"><a className="download" href={audioUrl} download><span>↓</span>{t.download}</a><button onClick={reset}>＋ {t.again}</button></div>
    </section>}
    <footer><span>HEART · STORY · MUSIC</span><b>Made with life itself</b></footer>
  </main>
}
