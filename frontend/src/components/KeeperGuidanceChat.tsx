import { useState, useRef, useEffect } from 'react'
import { api } from '../api'
import VoiceInput from './VoiceInput'

/**
 * Reusable voice-driven keeper-guidance chat for dedup screens.
 *
 * You speak (or type) criteria like "prefer files under Social Media Marketing"
 * or "never keep a copy in someone's transferred files folder". Bedrock turns it
 * into a keeper RULE, reflects back what it understood, and on confirm applies
 * the rule (keepers re-selected instantly across all groups). A separate action
 * hands the still-ambiguous groups to AI for per-file judgment.
 *
 * Props let it drive whichever panel embeds it:
 *  - onRuleApplied: called after a rule is applied so the panel reloads the list
 *  - ambiguousCount: current count of groups the rule can't decide
 *  - resolveOpts: passed to the AI ambiguity resolver
 */
interface Msg { role: 'user' | 'assistant'; content: string }

export function KeeperGuidanceChat({
  onRuleApplied,
  ambiguousCount = 0,
  resolveOpts,
}: {
  onRuleApplied: () => void
  ambiguousCount?: number
  resolveOpts: { preferFolder?: string; snapshotOnly?: boolean }
}) {
  const [messages, setMessages] = useState<Msg[]>([{
    role: 'assistant',
    content:
      "Tell me how to choose which copy to KEEP. For example: “prefer files under " +
      "Social Media Marketing over Cutting videos,” or “never keep a copy in a " +
      "person’s transferred-files folder,” or “keep the newest.” I’ll reflect it " +
      "back and apply it — you’ll see the keepers change.",
  }])
  const [draft, setDraft] = useState('')
  const [busy, setBusy] = useState(false)
  const [proposal, setProposal] = useState<{ patch: Record<string, any>; understanding: string; question: string } | null>(null)
  const [lastGuidance, setLastGuidance] = useState('')
  const [error, setError] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' })
  }, [messages])

  const send = async (text?: string) => {
    const msg = (text ?? draft).trim()
    if (!msg || busy) return
    setError(''); setDraft(''); setLastGuidance(msg)
    const history = messages.map(m => ({ role: m.role, content: m.content }))
    setMessages(m => [...m, { role: 'user', content: msg }])
    setBusy(true)
    try {
      const r = await api.crossdedupGuidanceChat(msg, history)
      setMessages(m => [...m, { role: 'assistant', content: r.understanding || 'Understood.' }])
      if (r.rule_patch && Object.keys(r.rule_patch).length > 0) {
        setProposal({ patch: r.rule_patch, understanding: r.understanding, question: r.clarifying_question || '' })
      } else {
        setProposal(null)
      }
    } catch (e: any) {
      setError(e.message || 'Chat failed')
      setMessages(m => [...m, { role: 'assistant', content: '(Bedrock call failed — check AWS access.)' }])
    } finally {
      setBusy(false)
    }
  }

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

  return (
    <div className="card">
      <div className="card-header"><h3>Keeper guidance</h3></div>
      {error && <div className="alert alert-error">{error}</div>}

      <div ref={scrollRef} style={{
        maxHeight: 220, overflowY: 'auto', border: '1px solid var(--border)',
        borderRadius: 'var(--radius)', padding: 10, background: 'var(--bg-primary)',
        display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 10,
      }}>
        {messages.map((m, i) => (
          <div key={i} style={{
            alignSelf: m.role === 'user' ? 'flex-end' : 'flex-start',
            maxWidth: '85%', padding: '7px 11px', borderRadius: 12, fontSize: '0.84rem',
            background: m.role === 'user' ? 'var(--accent)' : 'var(--bg-tertiary)',
            color: m.role === 'user' ? '#fff' : 'var(--text-primary)',
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
          }}>{m.content}</div>
        ))}
        {busy && <div style={{ alignSelf: 'flex-start', color: 'var(--text-muted)', fontSize: '0.8rem' }}>
          <span className="spinner" style={{ width: 12, height: 12, verticalAlign: 'middle', marginRight: 6 }} />thinking…
        </div>}
      </div>

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

      <div style={{ position: 'relative', display: 'flex', gap: 8, alignItems: 'flex-end' }}>
        <textarea value={draft} onChange={e => setDraft(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send() } }}
          placeholder="Speak or type your keeper guidance…" rows={2}
          style={{ flex: 1, fontSize: '0.84rem', resize: 'vertical' }} disabled={busy} />
        <VoiceInput onTranscript={t => setDraft(t)} onSubmit={() => send()} disabled={busy} />
        <button className="btn btn-primary" onClick={() => send()} disabled={busy || !draft.trim()}>Send</button>
      </div>

      {ambiguousCount > 0 && (
        <div style={{ marginTop: 10, display: 'flex', alignItems: 'center', gap: 10, fontSize: '0.8rem' }}>
          <span style={{ color: 'var(--warning)' }}>{ambiguousCount.toLocaleString()} groups the rule can’t decide</span>
          <button className="btn btn-secondary btn-sm" onClick={resolveAmbiguous} disabled={busy}>Let AI decide these</button>
        </div>
      )}
    </div>
  )
}
