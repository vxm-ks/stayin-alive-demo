import React, { useState } from 'react'
import ReactDOM from 'react-dom/client'
import DemoCase from './DemoCase'
import './styles.css'

function App() {
  const [lang, setLang] = useState<'zh' | 'en'>('zh')
  return <DemoCase lang={lang} setLang={setLang} />
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
