// Typed client for the FastAPI backend. All calls are same-origin /api
// (proxied by Vite in dev, served by FastAPI in production).

export type Health = { status: string; database: string; model: string }

export type Stats = {
  total: number
  by_status: Record<string, number>
  by_destination: Record<string, number>
  awaiting_review: number
  expedited_due_soon: number
  expedited_overdue_in_review: number
  due_soon_days: number
}

async function get<T>(path: string): Promise<T> {
  const res = await fetch(path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`)
  return res.json() as Promise<T>
}

export const api = {
  health: () => get<Health>('/health'),
  stats: () => get<Stats>('/api/stats'),
}
