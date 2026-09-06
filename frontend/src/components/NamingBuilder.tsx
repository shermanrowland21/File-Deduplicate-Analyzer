import { useMemo, useState } from 'react'

/**
 * Drag-and-drop naming-convention builder. Follows the element-stack pattern used
 * by tools like Adobe Bridge's Batch Rename: the filename is composed of ordered
 * ELEMENTS (AI/metadata tokens + literal text), reorderable and removable, with a
 * live preview. Compiles to the plain template string the backend already
 * understands (e.g. "{date}_{category}_{suggested_name}.{ext}") — no backend change.
 */

export interface NameToken {
  id: string        // unique element id
  token: string     // template token like "{date}", "sep", or "lit:<text>"
  label: string     // display label
  kind: 'token' | 'literal' | 'sep'
  value?: string    // for literal/sep: the literal text
}

// Palette of available tokens (must match what the backend resolves).
const PALETTE: { token: string; label: string; hint: string }[] = [
  { token: '{date}', label: 'Date', hint: 'File date' },
  { token: '{category}', label: 'AI Category', hint: 'e.g. photo, invoice, contract' },
  { token: '{suggested_name}', label: 'AI Name', hint: 'AI descriptive name' },
  { token: '{description}', label: 'Description', hint: 'Short AI description' },
  { token: '{tags}', label: 'Tags', hint: 'Top tags' },
  { token: '{original}', label: 'Original Name', hint: 'Existing filename' },
  { token: '{counter}', label: 'Counter', hint: '001, 002, …' },
  { token: '{mime}', label: 'Type', hint: 'image/document/video…' },
  { token: '{hash}', label: 'Hash', hint: 'First 8 chars' },
]

const SAMPLE: Record<string, string> = {
  '{date}': '2024-01-15',
  '{category}': 'agate',
  '{suggested_name}': 'laguna-agate-slab-polished',
  '{description}': 'polished laguna agate slab',
  '{tags}': 'agate-lapidary-red',
  '{original}': 'IMG_4523',
  '{counter}': '001',
  '{mime}': 'image',
  '{hash}': 'a8d89ecf',
}

let _uid = 0
const uid = () => `t${++_uid}`

function parseTemplate(template: string, separator: string): NameToken[] {
  // Split a template like "{date}_{category}_{suggested_name}.{ext}" into elements.
  // Strip the trailing .{ext} (extension is always appended by the engine).
  let body = template
  const extIdx = body.indexOf('.{ext}')
  if (extIdx >= 0) body = body.slice(0, extIdx)
  const parts = body.split(/(\{[a-z_]+\})/g).filter((s) => s !== '')
  const els: NameToken[] = []
  for (const p of parts) {
    if (/^\{[a-z_]+\}$/.test(p)) {
      const meta = PALETTE.find((x) => x.token === p)
      els.push({ id: uid(), token: p, label: meta?.label || p, kind: 'token' })
    } else if (p === separator) {
      els.push({ id: uid(), token: 'sep', label: 'sep', kind: 'sep', value: separator })
    } else {
      els.push({ id: uid(), token: `lit:${p}`, label: p, kind: 'literal', value: p })
    }
  }
  return els
}

function compile(elements: NameToken[], separator: string): string {
  const body = elements.map((e) =>
    e.kind === 'sep' ? separator : e.kind === 'literal' ? (e.value || '') : e.token
  ).join('')
  return `${body}.{ext}`
}

interface Props {
  template: string
  separator: string
  onChange: (template: string) => void
}

export function NamingBuilder({ template, separator, onChange }: Props) {
  const [elements, setElements] = useState<NameToken[]>(() => parseTemplate(template, separator))
  const [dragId, setDragId] = useState<string | null>(null)
  const [advanced, setAdvanced] = useState(false)

  const setEls = (next: NameToken[]) => {
    setElements(next)
    onChange(compile(next, separator))
  }

  const addToken = (token: string) => {
    const meta = PALETTE.find((x) => x.token === token)
    const next = [...elements]
    // auto-insert a separator between two tokens for readability
    if (next.length && next[next.length - 1].kind !== 'sep') {
      next.push({ id: uid(), token: 'sep', label: 'sep', kind: 'sep', value: separator })
    }
    next.push({ id: uid(), token, label: meta?.label || token, kind: 'token' })
    setEls(next)
  }

  const addLiteral = () => {
    const text = window.prompt('Literal text to insert (e.g. "HPL", "-v"):', '')
    if (text == null || text === '') return
    setEls([...elements, { id: uid(), token: `lit:${text}`, label: text, kind: 'literal', value: text }])
  }

  const removeEl = (id: string) => setEls(elements.filter((e) => e.id !== id))

  // native HTML5 drag reordering
  const onDrop = (targetId: string) => {
    if (!dragId || dragId === targetId) return
    const from = elements.findIndex((e) => e.id === dragId)
    const to = elements.findIndex((e) => e.id === targetId)
    if (from < 0 || to < 0) return
    const next = [...elements]
    const [moved] = next.splice(from, 1)
    next.splice(to, 0, moved)
    setDragId(null)
    setEls(next)
  }

  const preview = useMemo(() => {
    const body = elements.map((e) =>
      e.kind === 'sep' ? separator : e.kind === 'literal' ? (e.value || '')
        : (SAMPLE[e.token] ?? e.token)
    ).join('')
    return `${body}.jpg`
  }, [elements, separator])

  const chipStyle = (kind: string): React.CSSProperties => ({
    display: 'inline-flex', alignItems: 'center', gap: 6,
    padding: '4px 10px', borderRadius: 6, fontSize: '0.8rem', cursor: 'grab',
    background: kind === 'token' ? 'var(--accent)' : kind === 'sep' ? 'var(--bg-primary)' : 'var(--bg-tertiary)',
    color: kind === 'token' ? 'white' : 'var(--text-secondary)',
    border: kind === 'sep' ? '1px dashed var(--border)' : '1px solid var(--border)',
    fontFamily: kind === 'literal' || kind === 'sep' ? 'monospace' : 'inherit',
  })

  return (
    <div>
      {/* Palette */}
      <label style={{ fontSize: '0.8rem', color: 'var(--text-muted)' }}>
        Click to add naming elements (drag placed elements to reorder):
      </label>
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, margin: '6px 0 12px' }}>
        {PALETTE.map((p) => (
          <button key={p.token} type="button" title={p.hint} onClick={() => addToken(p.token)}
            style={{ ...chipStyle('token'), cursor: 'pointer', opacity: 0.9 }}>
            + {p.label}
          </button>
        ))}
        <button type="button" onClick={addLiteral}
          style={{ ...chipStyle('literal'), cursor: 'pointer' }}>+ Text…</button>
      </div>

      {/* The assembled name (drop zone) */}
      <div style={{
        display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center',
        minHeight: 44, padding: 10, borderRadius: 8, background: 'var(--bg-secondary)',
        border: '1px solid var(--border)',
      }}>
        {elements.length === 0 && (
          <span style={{ color: 'var(--text-muted)', fontSize: '0.8rem' }}>
            Add elements above to build your filename…
          </span>
        )}
        {elements.map((e) => (
          <span key={e.id}
            draggable
            onDragStart={() => setDragId(e.id)}
            onDragOver={(ev) => ev.preventDefault()}
            onDrop={() => onDrop(e.id)}
            style={chipStyle(e.kind)}>
            {e.kind === 'sep' ? (separator === ' ' ? '␣' : separator) : e.label}
            <span onClick={() => removeEl(e.id)} title="remove"
              style={{ cursor: 'pointer', fontWeight: 700, opacity: 0.7 }}>×</span>
          </span>
        ))}
        <span style={{ ...chipStyle('token'), background: 'var(--bg-primary)', color: 'var(--text-muted)', cursor: 'default', border: '1px solid var(--border)' }}>
          .ext
        </span>
      </div>

      {/* Live preview */}
      <div style={{ marginTop: 10, fontSize: '0.85rem' }}>
        <span style={{ color: 'var(--text-muted)' }}>Preview: </span>
        <span style={{ fontFamily: 'monospace', color: 'var(--success)' }}>{preview}</span>
      </div>

      {/* Advanced: raw template */}
      <div style={{ marginTop: 10 }}>
        <button type="button" onClick={() => setAdvanced(!advanced)}
          style={{ background: 'none', border: 'none', color: 'var(--accent)', cursor: 'pointer', fontSize: '0.78rem', padding: 0 }}>
          {advanced ? '▾ Hide' : '▸ Show'} raw template
        </button>
        {advanced && (
          <input type="text" value={template}
            onChange={(e) => { onChange(e.target.value); setElements(parseTemplate(e.target.value, separator)) }}
            style={{ fontFamily: 'monospace', marginTop: 6, width: '100%' }} />
        )}
      </div>
    </div>
  )
}
