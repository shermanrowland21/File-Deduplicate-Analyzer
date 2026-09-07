import { useState, useRef, useEffect, useCallback } from 'react'
import { api } from '../api'

/**
 * Voice-driven keeper-guidance chat for dedup screens. Composer visuals mirror
 * the AI-Research-Cowork app: one rounded input bar, auto-grow textarea, inline
 * mic + send on the right, and a live recording state (transcript + waveform +
 * accept/discard). You speak criteria; Bedrock turns them into a keeper RULE,
 * reflects it back, and on confirm applies it (keepers re-select instantly).
 * A separate action hands still-ambiguous groups to AI for per-file judgment.
 */
interface Msg { role: 'user' | 'assistant'; content: string }

export function KeeperGuidanceChat({
  onRuleApplied,
  ambiguousCount = 0,
  resolveOpts,
  placeholder = 'Tell me which copy to keep…',
}: {
  onRuleApplied: () => void
  ambiguousCount?: number
  resolveOpts: { preferFolder?: string; snapshotOnly?: boolean }
  placeholder?: string
}) {
  const [messages, setMessages] = useState<Msg[]>([])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const [recording, setRecording] = useState(false)
  const [proposal, setProposal] = useState<{ patch: Record<string, any>; question: string } | null>(null)
  const [lastGuidance, setLastGuidance] = useState('')
  const [error, setError] = useState('')

  const scrollRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const waveRef = useRef<HTMLCanvasElement>(null)
  const recRef = useRef<any>(null)
  const streamRef = useRef<MediaStream | null>(null)
  const audioCtxRef = useRef<AudioContext | null>(null)
  const rafRef = useRef<number | null>(null)
  const transcriptRef = useRef('')

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const send = useCallback(async (text?: string) => {
    const msg = (text ?? input).trim()
    if (!msg || busy) return
    setError(''); setInput(''); setLastGuidance(msg)
    if (inputRef.current) inputRef.current.style.height = 'auto'
    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(m => [...m, { role: 'user', content: msg }])
    setBusy(true)
    try {
      const r = await api.crossdedupGuidanceChat(msg, history)
      setMessages(m => [...m, { role: 'assistant', content: r.understanding || 'Understood.' }])
      if (r.rule_patch && Object.keys(r.rule_patch).length > 0) {
        setProposal({ patch: r.rule_patch, question: r.clarifying_question || '' })
      } else {
        setProposal(null)
      }
    } catch (e: any) {
      setError(e.message || 'Chat failed')
      setMessages(m => [...m, { role: 'assistant', content: '(Bedrock call failed — check AWS access.)' }])
    } finally {
      setBusy(false)
    }
  }, [input, busy, messages])

  const applyRule = async () => {
    if (!proposal) return
    setBusy(true); setError('')
    try {
      await api.crossdedupSetRule(proposal.patch)
      setMessages(m => [...m, { role: 'assistant', content: '✓ Applied. Keepers updated — see the list.' }])
      setProposal(null)
      onRuleApplied()
    } catch (e: any) {
      setError(e.message || 'Apply failed')
    } finally {
      setBusy(false)
    }
  }

  const resolveAmbiguous = async () => {
    setBusy(true); setError('')
    try {
      const r = await api.crossdedupResolveAmbiguous({ ...resolveOpts, guidance: lastGuidance })
      setMessages(m => [...m, { role: 'assistant', content: `AI resolved ${r.resolved} of ${r.ambiguous} ambiguous groups.` }])
      onRuleApplied()
    } catch (e: any) {
      setError(e.message || 'AI resolve failed')
    } finally {
      setBusy(false)
    }
  }

  // ---- voice (Web Speech API + live waveform) ----
  useEffect(() => {
    if (!recording) return
    let dead = false
    const canvas = waveRef.current
    if (!canvas) return
    navigator.mediaDevices.getUserMedia({ audio: true }).then(stream => {
      if (dead) { stream.getTracks().forEach(t => t.stop()); return }
      streamRef.current = stream
      const actx = new (window.AudioContext || (window as any).webkitAudioContext)()
      audioCtxRef.current = actx
      const src = actx.createMediaStreamSource(stream)
      const an = actx.createAnalyser(); an.fftSize = 256; an.smoothingTimeConstant = 0.5
      src.connect(an)
      const buf = new Float32Array(an.fftSize)
      const accent = getComputedStyle(document.documentElement).getPropertyValue('--accent').trim() || '#4f9cf7'
      const ctx = canvas.getContext('2d')!
      const rect = canvas.getBoundingClientRect()
      canvas.width = rect.width * 2; canvas.height = rect.height * 2
      const w = canvas.width, h = canvas.height, midY = h / 2
      const barW = 3, gap = 1, step = barW + gap
      const maxBars = Math.floor(w / step)
      const bars: number[] = []
      const draw = () => {
        if (dead) return
        rafRef.current = requestAnimationFrame(draw)
        an.getFloatTimeDomainData(buf)
        let sum = 0; for (let i = 0; i < buf.length; i++) sum += buf[i] * buf[i]
        bars.push(Math.min(Math.sqrt(sum / buf.length) * 6, 1))
        if (bars.length > maxBars) bars.shift()
        ctx.clearRect(0, 0, w, h)
        ctx.fillStyle = accent
        const startX = w - bars.length * step
        for (let i = 0; i < bars.length; i++) {
          const bh = Math.max(bars[i] * (h - 4), 2)
          ctx.fillRect(startX + i * step, midY - bh / 2, barW, bh)
        }
      }
      draw()
    }).catch(() => {})
    return () => {
      dead = true
      if (rafRef.current) cancelAnimationFrame(rafRef.current)
      if (streamRef.current) streamRef.current.getTracks().forEach(t => t.stop())
      if (audioCtxRef.current) audioCtxRef.current.close().catch(() => {})
      streamRef.current = null; audioCtxRef.current = null; rafRef.current = null
    }
  }, [recording])

  const startVoice = () => {
    const SR = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition
    if (!SR) { setError('Voice needs Chrome or Edge.'); return }
    transcriptRef.current = ''; setInput('')
    const rec = new SR(); rec.continuous = true; rec.interimResults = true
    rec.lang = localStorage.getItem('dedup-language') || 'en-US'
    rec.onresult = (ev: any) => {
      let f = '', i = ''
      for (let x = 0; x < ev.results.length; x++) {
        if (ev.results[x].isFinal) f += ev.results[x][0].transcript
        else i += ev.results[x][0].transcript
      }
      transcriptRef.current = f + i; setInput(f + i)
    }
    rec.onend = () => { if (recRef.current === rec) try { rec.start() } catch { /* */ } }
    rec.onerror = (e: any) => { if (e.error !== 'no-speech' && e.error !== 'aborted') { recRef.current = null; setRecording(false) } }
    recRef.current = rec; setRecording(true)
    try { rec.start() } catch { setRecording(false) }
  }
  const acceptVoice = () => {
    if (recRef.current) try { recRef.current.stop() } catch { /* */ }
    recRef.current = null; setRecording(false)
    setTimeout(() => inputRef.current?.focus(), 50)
  }
  const discardVoice = () => {
    if (recRef.current) try { recRef.current.stop() } catch { /* */ }
    recRef.current = null; setInput(''); transcriptRef.current = ''; setRecording(false)
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (recording) acceptVoice(); else send()
    }
  }

  return (
    <div className="card">
      <div className="card-header"><h3>Keeper guidance</h3></div>
      {error && <div className="alert alert-error">{error}</div>}

      {messages.length > 0 && (
        <div ref={scrollRef} className="guidance-transcript">
          {messages.map((m, i) => (
            <div key={i} className={`guidance-bubble ${m.role}`}>{m.content}</div>
          ))}
          {busy && <div className="guidance-bubble assistant"><span className="spinner" style={{ width: 12, height: 12, verticalAlign: 'middle', marginRight: 6 }} />thinking…</div>}
        </div>
      )}

      {proposal && (
        <div className="alert" style={{ background: 'var(--bg-tertiary)', border: '1px solid var(--accent)', display: 'block' }}>
          <div style={{ fontSize: '0.82rem', marginBottom: 6 }}>
            <strong>Proposed keeper rule</strong>
            {proposal.question && <div style={{ color: 'var(--warning)', marginTop: 4 }}>❓ {proposal.question}</div>}
          </div>
          <pre style={{ fontSize: '0.74rem', background: 'var(--bg-primary)', padding: 8, borderRadius: 6, overflowX: 'auto', margin: '0 0 8px' }}>
            {JSON.stringify(proposal.patch, null, 2)}
          </pre>
          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn btn-primary btn-sm" onClick={applyRule} disabled={busy}>Apply &amp; re-select keepers</button>
            <button className="btn btn-secondary btn-sm" onClick={() => setProposal(null)} disabled={busy}>Dismiss</button>
          </div>
        </div>
      )}

      {/* Composer — mirrors the AI-Research-Cowork input bar */}
      <div className="input-bar">
        <div className="input-box">
          {recording ? (
            <>
              <div className="voice-transcript">{input || <span className="voice-listening">Listening…</span>}</div>
              <div className="voice-bar-row">
                <canvas ref={waveRef} className="input-waveform" />
                <button className="btn-voice-discard" onClick={discardVoice} title="Discard">&times;</button>
                <button className="btn-voice-accept" onClick={acceptVoice} title="Accept">&#x2713;</button>
              </div>
            </>
          ) : (
            <>
              <textarea
                ref={inputRef}
                value={input}
                onChange={e => {
                  setInput(e.target.value)
                  const el = e.target; el.style.height = 'auto'
                  el.style.height = Math.min(el.scrollHeight, 160) + 'px'
                }}
                onKeyDown={handleKeyDown}
                placeholder={placeholder}
                rows={1}
                disabled={busy}
              />
              <div className="input-bar-footer">
                <div className="input-bar-left">
                  {ambiguousCount > 0 && (
                    <button className="chip-ambiguous" onClick={resolveAmbiguous} disabled={busy}
                      title="Let AI decide the groups the rule couldn't">
                      {ambiguousCount.toLocaleString()} unresolved · let AI decide
                    </button>
                  )}
                </div>
                <div className="input-bar-right">
                  <button className="btn-mic" onClick={startVoice} disabled={busy} title="Voice input">
                    <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V5a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm-1-9a1 1 0 1 1 2 0v6a1 1 0 1 1-2 0V5zm6 6c0 2.76-2.24 5-5 5s-5-2.24-5-5H5c0 3.53 2.61 6.43 6 6.92V21h2v-3.08c3.39-.49 6-3.39 6-6.92h-2z" /></svg>
                  </button>
                  {input.trim() && <button className="btn-send" onClick={() => send()} disabled={busy} title="Send">&#x2191;</button>}
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
