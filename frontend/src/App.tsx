import { useEffect, useState } from 'react'
import { api, type Health, type Stats } from './lib/api'

type Tone = 'neutral' | 'warning' | 'critical'

const toneStyles: Record<Tone, { bar: string; icon: string; label: string }> = {
  neutral: { bar: 'bg-hairline', icon: '', label: '' },
  warning: { bar: 'bg-warning', icon: '▲', label: 'Attention' },
  critical: { bar: 'bg-critical', icon: '●', label: 'Overdue' },
}

function StatTile({ label, value, hint, tone = 'neutral' }: {
  label: string; value: number | string; hint?: string; tone?: Tone
}) {
  const t = toneStyles[tone]
  return (
    <div className="relative overflow-hidden rounded-xl border border-hairline bg-surface p-4">
      <div className={`absolute inset-y-0 left-0 w-1 ${t.bar}`} aria-hidden />
      <p className="text-sm text-ink-2">{label}</p>
      <p className="mt-1 text-3xl font-semibold tabular-nums">{value}</p>
      {(hint || t.label) && (
        <p className="mt-1 text-xs text-muted">
          {t.icon && <span aria-hidden className="mr-1">{t.icon}</span>}
          {t.label && <span className="mr-1 font-medium text-ink-2">{t.label}</span>}
          {hint}
        </p>
      )}
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [stats, setStats] = useState<Stats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    Promise.all([api.health(), api.stats()])
      .then(([h, s]) => { setHealth(h); setStats(s) })
      .catch((e: Error) => setError(e.message))
  }, [])

  return (
    <div className="mx-auto max-w-6xl px-4 py-8">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <h1 className="text-2xl font-semibold">Case Triage Console</h1>
        {health && (
          <p className="text-xs text-muted">
            API {health.status} · {health.database} · {health.model}
          </p>
        )}
      </header>

      {error && (
        <p role="alert" className="rounded-lg border border-critical/40 bg-critical/10 p-3 text-sm">
          Could not reach the API: {error}
        </p>
      )}

      {stats && (
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4" aria-label="Summary">
          <StatTile label="Cases triaged" value={stats.total} />
          <StatTile label="Awaiting review" value={stats.awaiting_review}
            tone={stats.awaiting_review > 0 ? 'warning' : 'neutral'} hint="human triage lead" />
          <StatTile label="Expedited due soon" value={stats.expedited_due_soon}
            tone={stats.expedited_due_soon > 0 ? 'warning' : 'neutral'}
            hint={`within ${stats.due_soon_days} days`} />
          <StatTile label="Overdue in review" value={stats.expedited_overdue_in_review}
            tone={stats.expedited_overdue_in_review > 0 ? 'critical' : 'neutral'} hint="past regulatory due date" />
        </section>
      )}
    </div>
  )
}
