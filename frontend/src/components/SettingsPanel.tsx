import { useEffect, useState } from 'react'
import { api } from '../api'

/**
 * Settings — pick which AWS Bedrock model each analysis function uses, so you can
 * try different models per function without editing code or restarting. Each
 * function is independent: e.g. run Smart Naming on cheap DeepSeek while keeping
 * the Dedup Advisor on a stronger model. Changes take effect immediately.
 */
interface FnSetting {
  label: string
  value: string
  default: string
  env_var: string
  is_custom: boolean
}

export function SettingsPanel() {
  const [functions, setFunctions] = useState<Record<string, FnSetting>>({})
  const [available, setAvailable] = useState<any[]>([])
  const [loading, setLoading] = useState(true)
  const [savingKey, setSavingKey] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState('')

  useEffect(() => { load() }, [])

  const load = async () => {
    setLoading(true); setError('')
    try {
      const data = await api.getSettings()
      setFunctions(data.functions || {})
      setAvailable(data.available_models || [])
    } catch (e: any) {
      setError(e.message || 'Failed to load settings')
    } finally {
      setLoading(false)
    }
  }

  const change = async (key: string, modelId: string) => {
    setSavingKey(key); setError(''); setSaved('')
    try {
      await api.setSettingModel(key, modelId)
      setSaved(`Saved: ${functions[key]?.label}`)
      await load()
    } catch (e: any) {
      setError(e.message || 'Failed to save')
    } finally {
      setSavingKey(null)
    }
  }

  const reset = async (key: string) => {
    setSavingKey(key); setError(''); setSaved('')
    try {
      await api.resetSettingModel(key)
      await load()
    } catch (e: any) {
      setError(e.message || 'Failed to reset')
    } finally {
      setSavingKey(null)
    }
  }

  // build the select options; include the current value even if not in the known list
  const optionsFor = (current: string) => {
    const ids = new Set(available.map((m) => m.model_id))
    const extra = current && !ids.has(current) ? [{ model_id: current, model_name: current + ' (custom)', provider: '' }] : []
    return [...available, ...extra]
  }

  return (
    <div>
      <div className="card">
        <div className="card-header"><h2>Settings — AI Models</h2></div>
        <p style={{ color: 'var(--text-secondary)', fontSize: '0.85rem' }}>
          Choose which AWS Bedrock model each analysis function uses. Functions are independent —
          set a cheap model for bulk naming and a stronger one for high-stakes decisions like the
          dedup advisor. All models run inside AWS Bedrock. Changes apply immediately.
        </p>

        {error && <div className="alert alert-error">{error}</div>}
        {saved && <div className="alert alert-success">{saved}</div>}
        {loading && <div><span className="spinner" /> Loading…</div>}

        {!loading && Object.entries(functions).map(([key, fn]) => (
          <div key={key} style={{
            display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap',
            padding: '10px 0', borderBottom: '1px solid var(--border)',
          }}>
            <div style={{ flex: '1 1 240px' }}>
              <div style={{ fontWeight: 600, fontSize: '0.88rem' }}>{fn.label}</div>
              <div style={{ fontSize: '0.72rem', color: 'var(--text-muted)' }}>
                {fn.is_custom ? 'custom' : 'default'} · env: {fn.env_var}
              </div>
            </div>
            <select
              value={fn.value}
              disabled={savingKey === key}
              onChange={(e) => change(key, e.target.value)}
              style={{ flex: '2 1 320px' }}
            >
              {optionsFor(fn.value).map((m) => (
                <option key={m.model_id} value={m.model_id}>
                  {m.model_name}{m.provider ? ` (${m.provider})` : ''}
                  {m.supports_video ? ' — video' : m.supports_images ? ' — vision' : ''}
                </option>
              ))}
            </select>
            {fn.is_custom && (
              <button className="btn btn-secondary btn-sm" disabled={savingKey === key}
                onClick={() => reset(key)}>Reset</button>
            )}
            {savingKey === key && <span className="spinner" />}
          </div>
        ))}
      </div>

      <div className="card">
        <div className="card-header"><h3>Available Bedrock models</h3></div>
        <table className="file-table">
          <thead><tr><th>Model</th><th>Provider</th><th>Vision</th><th>Video</th></tr></thead>
          <tbody>
            {available.map((m) => (
              <tr key={m.model_id}>
                <td>{m.model_name}<div style={{ fontSize: '0.68rem', color: 'var(--text-muted)', fontFamily: 'monospace' }}>{m.model_id}</div></td>
                <td>{m.provider}</td>
                <td>{m.supports_images ? '✓' : '—'}</td>
                <td>{m.supports_video ? '✓' : '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <p style={{ fontSize: '0.75rem', color: 'var(--text-muted)' }}>
          To use a model not listed (e.g. a DeepSeek inference profile), set it via the function's
          environment variable, or it will appear here once enabled in your Bedrock account.
        </p>
      </div>
    </div>
  )
}
