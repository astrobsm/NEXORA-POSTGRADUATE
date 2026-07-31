/**
 * The synchronisation engine.
 *
 * Push first, then pull. Pushing first means a record captured offline reaches the
 * server before the pull overwrites the local cache with the server's older view of
 * the world — the reverse order silently loses work.
 */

import { api, OfflineError } from './api'
import {
  cacheCollection,
  db,
  deviceId,
  getMeta,
  setMeta,
  type OutboxItem,
} from './db'

export interface SyncResult {
  pushed: number
  pulled: number
  conflicts: number
  rejected: number
  at: string
  error?: string
}

export type SyncState = 'idle' | 'syncing' | 'error' | 'offline'

type Listener = (state: SyncState, result?: SyncResult) => void

const listeners = new Set<Listener>()
let state: SyncState = 'idle'
let running = false

export function onSyncChange(listener: Listener): () => void {
  listeners.add(listener)
  listener(state)
  return () => listeners.delete(listener)
}

function emit(next: SyncState, result?: SyncResult) {
  state = next
  listeners.forEach((listener) => listener(next, result))
}

export function syncState(): SyncState {
  return state
}

/** Collections the device mirrors, in dependency order. */
const PULL_COLLECTIONS = [
  'curriculum_versions',
  'training_years',
  'rotation_templates',
  'competencies',
  'requirement_rules',
  'procedure_catalogue',
  'assessment_templates',
  'enrolments',
  'rotation_assignments',
  'log_entries',
  'assessments',
  'academic_activities',
  'duty_shifts',
  'cme_assignments',
  'notifications',
]

// --------------------------------------------------------------------------
async function pushOutbox(): Promise<{ pushed: number; conflicts: number; rejected: number }> {
  const queued = await db.outbox
    .where('status')
    .anyOf('queued', 'failed')
    .sortBy('createdAt')

  if (!queued.length) return { pushed: 0, conflicts: 0, rejected: 0 }

  // Batch, but not unboundedly — a device offline for a week could hold hundreds
  // of entries and a single enormous request is the one most likely to time out.
  const batch = queued.slice(0, 50)
  await db.outbox.bulkPut(batch.map((item) => ({ ...item, status: 'sending' as const })))

  const items = batch.map((item: OutboxItem) => ({
    collection: item.collection,
    op: item.op,
    id: item.serverId,
    client_uuid: item.clientUuid,
    base_revision: item.baseRevision,
    data: item.payload,
  }))

  try {
    const response = await api.post<{
      applied: { client_uuid?: string; id: string; status: string; revision: number }[]
      conflicts: {
        id: string
        client_revision: number
        server_revision: number
        server_payload: Record<string, unknown>
        message: string
      }[]
      rejected: { client_uuid?: string; id?: string; reason: string }[]
      summary: { applied: number; conflicts: number; rejected: number }
    }>('/sync/push', {
      device_id: deviceId(),
      device_label: navigator.userAgent.slice(0, 120),
      app_version: __APP_VERSION__,
      items,
    })

    const appliedUuids = new Set(response.applied.map((a) => a.client_uuid).filter(Boolean))
    const rejectedUuids = new Map(
      response.rejected.map((r) => [r.client_uuid ?? r.id ?? '', r.reason]),
    )

    for (const item of batch) {
      if (appliedUuids.has(item.clientUuid)) {
        await db.outbox.delete(item.id!)
        continue
      }
      const reason = rejectedUuids.get(item.clientUuid) ?? rejectedUuids.get(item.serverId ?? '')
      if (reason) {
        // A rejection is permanent — retrying an ill-formed payload forever would
        // just wedge the queue behind it.
        await db.outbox.update(item.id!, {
          status: 'failed',
          attempts: item.attempts + 1,
          lastError: reason,
        })
        continue
      }
      await db.outbox.update(item.id!, {
        status: 'queued',
        attempts: item.attempts + 1,
      })
    }

    for (const conflict of response.conflicts) {
      const source = batch.find((item) => item.serverId === conflict.id)
      await db.conflicts.add({
        entityType: source?.collection ?? 'log_entries',
        entityId: conflict.id,
        clientRevision: conflict.client_revision,
        serverRevision: conflict.server_revision,
        clientPayload: source?.payload ?? {},
        serverPayload: conflict.server_payload,
        detectedAt: new Date().toISOString(),
        resolved: false,
      })
      if (source?.id) {
        await db.outbox.update(source.id, { status: 'conflict', lastError: conflict.message })
      }
    }

    return {
      pushed: response.summary.applied,
      conflicts: response.summary.conflicts,
      rejected: response.summary.rejected,
    }
  } catch (error) {
    // Put everything back in the queue; nothing is lost by a failed push.
    await db.outbox.bulkPut(
      batch.map((item) => ({
        ...item,
        status: 'queued' as const,
        attempts: item.attempts + 1,
        lastError: error instanceof Error ? error.message : String(error),
      })),
    )
    throw error
  }
}

async function pull(): Promise<number> {
  const since = await getMeta<string | null>('lastPulledAt', null)
  let total = 0
  let hasMore = true
  let guard = 0

  while (hasMore && guard < 20) {
    guard += 1
    const response = await api.get<{
      server_time: string
      cursors: Record<string, string>
      has_more: boolean
      data: Record<string, Record<string, unknown>[]>
    }>('/sync/pull', {
      device_id: deviceId(),
      collections: PULL_COLLECTIONS,
      ...(since && guard === 1 ? { since } : {}),
    })

    for (const [collection, rows] of Object.entries(response.data)) {
      total += await cacheCollection(collection, rows)
    }
    await setMeta('lastPulledAt', response.server_time)
    await setMeta('cursors', response.cursors)
    hasMore = response.has_more
  }
  return total
}

// --------------------------------------------------------------------------
export async function synchronise(): Promise<SyncResult> {
  if (running) {
    return { pushed: 0, pulled: 0, conflicts: 0, rejected: 0, at: new Date().toISOString() }
  }
  if (!navigator.onLine) {
    emit('offline')
    return {
      pushed: 0,
      pulled: 0,
      conflicts: 0,
      rejected: 0,
      at: new Date().toISOString(),
      error: 'Device is offline.',
    }
  }

  running = true
  emit('syncing')
  try {
    const push = await pushOutbox()
    const pulled = await pull()
    const result: SyncResult = {
      ...push,
      pulled,
      at: new Date().toISOString(),
    }
    await setMeta('lastSyncResult', result)
    emit('idle', result)
    return result
  } catch (error) {
    const offline = error instanceof OfflineError
    const result: SyncResult = {
      pushed: 0,
      pulled: 0,
      conflicts: 0,
      rejected: 0,
      at: new Date().toISOString(),
      error: error instanceof Error ? error.message : String(error),
    }
    emit(offline ? 'offline' : 'error', result)
    return result
  } finally {
    running = false
  }
}

let timer: number | undefined

/**
 * Start background synchronisation.
 *
 * Syncs on an interval, whenever the browser regains connectivity, and when the
 * tab becomes visible again — the last one matters because a phone in a pocket
 * suspends timers, and the user expects a fresh view when they look at it.
 */
export function startAutoSync(intervalMs = 120_000): () => void {
  const trigger = () => {
    if (navigator.onLine) void synchronise()
  }

  timer = window.setInterval(trigger, intervalMs)
  window.addEventListener('online', trigger)
  const onVisible = () => {
    if (document.visibilityState === 'visible') trigger()
  }
  document.addEventListener('visibilitychange', onVisible)
  const onOffline = () => emit('offline')
  window.addEventListener('offline', onOffline)

  trigger()

  return () => {
    if (timer) window.clearInterval(timer)
    window.removeEventListener('online', trigger)
    window.removeEventListener('offline', onOffline)
    document.removeEventListener('visibilitychange', onVisible)
  }
}
