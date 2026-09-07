import { useState, useEffect, useRef } from 'react'
import { api } from '../api'
import VoiceInput from './VoiceInput'

/**
 * Dedup / Routing Advisor (Step 3a) — the "principles chat + rules engine".
 *
 * You STATE A PRINCIPLE (by voice or typing), the assistant REFLECTS its
 * understanding back and proposes a config change, you CONFIRM to apply it, then
 * you run a DRY-RUN classification to see how every file would be deduped and
 * routed (CPMS / SharePoint-US / SharePoint-China). Nothing is moved or deleted
 * here — this only produces advice. The reversible crossdedup/reconstruct tools
 * do the actual work later, on explicit approval.
 */
interface ChatMsg { role: 'user' | 'assistant'; content: string }
interface Proposal { patch: Record<string, any>; understanding: string; clarifying_question: string }

const fmtGB = (gb: number) => `${gb.toLocaleString(undefined, { maximumFractionDigits: 1 })} GB`
const num = (n: number) => (n ?? 0).toLocaleString()

const ROUTE_LABELS: Record<string, string> = {
  cpms: 'CPMS (media → AWS)',
  sharepoint_us: 'SharePoint — US',
  sharepoint_china: 'SharePoint — China',
  unrouted: 'Unrouted (needs a rule / AI)',
}
const ROT_LABELS: Record<string, string> = {
  keep: 'Keep', redundant: 'Redundant', obsolete: 'Obsolete', trivial: 'Trivial',
}

export function DedupAdvisorPanel() {
  const [messages, setMessages] = useState<ChatMsg[]>([{
    role: 'assistant',
    content:
      "Tell me a principle for cleaning up these files — for example, " +
      "\"terminated employees' files are duplicates, keep the active copy,\" or " +
      "\"anything under HPL China goes to the China SharePoint.\" I'll reflect back " +
      "what I understood and propose a rule; you confirm before it's applied.",
  }])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [proposal, setProposal] = useState<Proposal | null>(null)
  const [config, setConfig] = useState<any>(null)
  const [classify, setClassify] = useState<any>(null)
  const [classifying, setClassifying] = useState(false)
  const [error, setError] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  const loadConfig = async () => {
    try { setConfig(await api.advisorConfig()) } catch { /* ignore */ }
  }
  useEffect(() => { loadConfig() }, [])
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const send = async (text?: string) => {
    const msg = (text ?? draft).trim()
    if (!msg || busy) return
    setError('')
    setDraft('')
    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(m => [...m, { role: 'user', content: msg }])
    setBusy(true)
    try {
      const r = await api.advisorChat(msg, history)
      const reply = r.understanding || 'Understood.'
      setMessages(m => [...m, { role: 'assistant', content: reply }])
      if (r.patch && Object.keys(r.patch).length > 0) {
        setProposal({ patch: r.patch, understanding: reply, clarifying_question: r.clarifying_question || '' })
      } else {
        setProposal(null)
      }
    } catch (e: any) {
      setError(e.message || 'Chat failed')
      setMessages(m => [...m, { role: 'assistant', content: '(Bedrock call failed — check AWS credentials / model access.)' }])
    } finally {
      setBusy(false)
    }
  }

  const applyProposal = async () => {
    if (!proposal) return
    setBusy(true); setError('')
    try {
      const updated = await api.advisorUpdateConfig(proposal.patch)
      setConfig(updated)
      setMessages(m => [...m, { role: 'assistant', content: '✓ Applied. The rule is now part of the engine. Run a classification to see the effect.' }])
      setProposal(null)
    } catch (e: any) {
      setError(e.message || 'Apply failed')
    } finally {
      setBusy(false)
    }
  }

  const runClassify = async () => {
    setClassifying(true); setError('')
    try {
      setClassify(await api.advisorClassify(12))
    } catch (e: any) {
      setError(e.message || 'Classification failed')
    } finally {
      setClassifying(false)
    }
  }

  const s = classify?.summary

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Dedup &amp; Routing Advisor</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          State your cleanup principles — by voice or typing. I reflect back what I understood and
          propose a rule; you confirm before anything is applied. Then run a <strong>dry-run
          classification</strong> to see how every file would be deduped and routed. This screen is
          <strong> advisory only</strong> — nothing is moved or deleted here.
        </p>
        {error && <div className="alert alert-error">{error}</div>}

        {/* Chat transcript */}
        <div ref={scrollRef} style={{
          maxHeight: 340, overflowY: 'auto', border: '1px solid var(--border)',
          borderRadius: 'var(--radius)', padding: 12, background: 'var(--bg-primary)',
          display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 12,
        }}>
          {messages.map((m, i) => (
            <div key={i} style={{
              alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
              maxWidth: '85%', padding: '8px 12px', borderRadius: 12, fontSize: '0.86rem',
              background: m.role === 'user' ? 'var(--accent)' : 'var(--bg-tertiary)',
              color: m.role === 'user' ? '#fff' : 'var(--text-primary)',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}>{m.content}</div>
          ))}
          {busy && (
            <div style={{ alignSelf: 'flex-start', color: 'var(--text-muted)', fontSize: '0.8rem' }}>
              <span className="spinner" style={{ width: 12, height: 12, verticalAlign: 'middle', marginRight: 6 }} />
              thinking…
            </div>
          )}
        </div>

        {/* Proposal confirm card */}
        {proposal && (
          <div className="alert" style={{ background: 'var(--bg-tertiary)', border: '1px solid var(--accent)', display: 'block' }}>
            <div style={{ fontSize: '0.82rem', marginBottom: 8 }}>
              <strong>Proposed rule change</strong>
              {proposal.clarifying_question && (
                <div style={{ color: 'var(--warning)', marginTop: 4 }}>❓ {proposal.clarifying_question}</div>
              )}
            </div>
            <pre style={{
              fontSize: '0.76rem', background: 'var(--bg-primary)', padding: 8,
              borderRadius: 6, overflowX: 'auto', margin: '0 0 8px',
            }}>{JSON.stringify(proposal.patch, null, 2)}</pre>
            <div style={{ display: 'flex', gap: 8 }}>
              <button className="btn btn-primary btn-sm" onClick={applyProposal} disabled={busy}>Confirm &amp; apply</button>
              <button className="btn btn-secondary btn-sm" onClick={() => setProposal(null)} disabled={busy}>Dismiss</button>
            </div>
          </div>
        )}

        {/* Input row with voice input. position:relative anchors the mic overlay. */}
        <div style={{ position: 'relative', display: 'flex', gap: 8, alignItems: 'flex-end' }}>
          <textarea
            value={draft}
            onChange={e => setDraft(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
            placeholder="State a principle… (Enter to send, Shift+Enter for newline)"
            rows={2}
            style={{ flex: 1, fontSize: '0.86rem', resize: 'vertical' }}
            disabled={busy}
          />
          <VoiceInput
            onTranscript={(t) => setDraft(t)}
            onSubmit={() => send()}
            disabled={busy}
          />
          <button className="btn btn-primary" onClick={() => send()} disabled={busy || !draft.trim()}>Send</button>
        </div>
      </div>

      {/* Current rules summary */}
      {config && (
        <div className="card">
          <div className="card-header"><h3>Current rules</h3></div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: 10, fontSize: '0.8rem' }}>
            <div><span style={{ color: 'var(--text-muted)' }}>Keeper preference:</span> <strong>{config.prefer_folder}</strong></div>
            <div><span style={{ color: 'var(--text-muted)' }}>Obsolete after:</span> <strong>{config.obsolete_years} yrs</strong></div>
            <div><span style={{ color: 'var(--text-muted)' }}>China signals:</span> <strong>{(config.china_path_signals || []).join(', ') || '—'}</strong>{config.china_use_cjk ? ' + CJK' : ''}</div>
            <div><span style={{ color: 'var(--text-muted)' }}>Transfer root:</span> <span style={{ fontFamily: 'monospace' }}>{config.transfer_root}</span></div>
          </div>
          {(config.principle_notes || []).length > 0 && (
            <div style={{ marginTop: 10, fontSize: '0.78rem' }}>
              <div style={{ color: 'var(--text-muted)', marginBottom: 4 }}>Captured principles:</div>
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {config.principle_notes.map((n: string, i: number) => <li key={i}>{n}</li>)}
              </ul>
            </div>
          )}
        </div>
      )}

      {/* Classification (dry run) */}
      <div className="card">
        <div className="card-header">
          <h3>Classification (dry run)</h3>
          <button className="btn btn-secondary btn-sm" onClick={runClassify} disabled={classifying}>
            {classifying ? <><span className="spinner" /> Classifying…</> : 'Run classification'}
          </button>
        </div>
        <p style={{ color: 'var(--text-muted)', fontSize: '0.78rem' }}>
          Runs the current rules over both MD5 indexes (~1.3M files, ~30s). No files change.
        </p>

        {s && (
          <>
            <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', margin: '12px 0' }}>
              <Stat label="Total files" value={num(s.total_files)} />
              <Stat label="Unique (by MD5)" value={num(s.unique_md5)} />
              <Stat label="Redundant" value={num(s.verdict?.redundant)} color="var(--danger)" />
              <Stat label="Reclaimable" value={fmtGB(s.reclaimable_gb)} color="var(--warning)" />
              <Stat label="Transferred-redundant" value={`${num(s.transferred_redundant_files)} · ${fmtGB(s.transferred_redundant_gb)}`} color="var(--warning)" />
            </div>

            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
              <Breakdown title="ROT buckets" data={s.rot} labels={ROT_LABELS} />
              <Breakdown title="Routing" data={s.route} labels={ROUTE_LABELS} />
            </div>

            {classify.examples?.length > 0 && (
              <div style={{ marginTop: 16 }}>
                <h4 style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: 6 }}>
                  Example transferred-employee redundancies
                </h4>
                {classify.examples.map((ex: any, i: number) => (
                  <div key={i} style={{ borderBottom: '1px solid var(--border)', padding: '6px 0', fontSize: '0.76rem' }}>
                    <div style={{ color: 'var(--danger)', fontFamily: 'monospace', wordBreak: 'break-all' }}>✕ {ex.redundant}</div>
                    <div style={{ color: 'var(--success)', fontFamily: 'monospace', wordBreak: 'break-all' }}>✓ keep: {ex.keeper}</div>
                    <div style={{ color: 'var(--text-muted)' }}>{ex.route} · {ex.size_mb} MB · {ex.reason}</div>
                  </div>
                ))}
              </div>
            )}
            <div style={{ marginTop: 10, fontSize: '0.72rem', color: 'var(--text-muted)' }}>
              ran in {s.elapsed_seconds}s · keeper preference: {s.prefer_folder}
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div style={{ fontSize: '1.3rem', fontWeight: 700, color: color || 'var(--text-primary)' }}>{value}</div>
      <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>{label}</div>
    </div>
  )
}

function Breakdown({ title, data, labels }: { title: string; data: Record<string, number>; labels: Record<string, string> }) {
  const total = Object.values(data || {}).reduce((a, b) => a + b, 0) || 1
  return (
    <div>
      <h4 style={{ fontSize: '0.82rem', color: 'var(--text-secondary)', marginBottom: 8 }}>{title}</h4>
      {Object.entries(data || {}).map(([k, v]) => (
        <div key={k} style={{ marginBottom: 6 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.78rem' }}>
            <span>{labels[k] || k}</span>
            <span style={{ color: 'var(--text-muted)' }}>{num(v)}</span>
          </div>
          <div style={{ height: 6, background: 'var(--bg-tertiary)', borderRadius: 4, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${(v / total) * 100}%`, background: 'var(--accent)' }} />
          </div>
        </div>
      ))}
    </div>
  )
}
