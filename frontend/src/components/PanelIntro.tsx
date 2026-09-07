/**
 * PanelIntro — a plain-language "what this screen does" banner shown at the top
 * of every tab, so the user always knows the purpose, whether it's safe, and
 * when to use it. Reduces decision overload across the many tools.
 */
type Safety = 'safe' | 'reversible' | 'moves' | 'readonly'

const SAFETY_META: Record<Safety, { label: string; color: string }> = {
  readonly: { label: 'Read-only — nothing changes', color: 'var(--success)' },
  safe: { label: 'Safe', color: 'var(--success)' },
  reversible: { label: 'Reversible — quarantines, never deletes; Undo available', color: 'var(--warning)' },
  moves: { label: 'Moves/renames files (reversible)', color: 'var(--warning)' },
}

export function PanelIntro({
  title,
  what,
  useWhen,
  safety = 'safe',
  startHere = false,
}: {
  title: string
  what: string
  useWhen?: string
  safety?: Safety
  startHere?: boolean
}) {
  const meta = SAFETY_META[safety]
  return (
    <div className="panel-intro">
      <div className="panel-intro-head">
        <h2>{title}</h2>
        {startHere && <span className="panel-intro-starthere">Start here</span>}
        <span className="panel-intro-badge" style={{ color: meta.color, borderColor: meta.color }}>
          {meta.label}
        </span>
      </div>
      <p className="panel-intro-what">{what}</p>
      {useWhen && <p className="panel-intro-usewhen"><strong>Use this when:</strong> {useWhen}</p>}
    </div>
  )
}
