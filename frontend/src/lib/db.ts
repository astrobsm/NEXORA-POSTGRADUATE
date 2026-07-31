/**
 * The offline database (IndexedDB via Dexie).
 *
 * The design assumption is that a registrar will be in a theatre or an emergency
 * unit with no signal and will still need to record what they just did. So:
 *
 *  - Reference data (curriculum, procedures, competencies) is mirrored so forms
 *    render fully offline.
 *  - Writes go into an outbox with a client-generated UUID and are replayed when
 *    the connection returns; the server de-duplicates on that UUID.
 *  - Nothing is silently discarded: a rejected replay becomes a visible conflict.
 */

import Dexie, { type Table } from 'dexie'

export interface OutboxItem {
  id?: number
  clientUuid: string
  collection: 'log_entries' | 'assessments'
  op: 'create' | 'update'
  /** Server id, for updates. */
  serverId?: string
  /** The revision the device last saw, for conflict detection. */
  baseRevision?: number
  payload: Record<string, unknown>
  createdAt: string
  attempts: number
  lastError?: string
  status: 'queued' | 'sending' | 'failed' | 'conflict'
}

export interface CachedRecord {
  id: string
  collection: string
  data: Record<string, unknown>
  updatedAt: string
  revision?: number
}

export interface SyncMeta {
  key: string
  value: unknown
  updatedAt: string
}

export interface ConflictRecord {
  id?: number
  entityType: string
  entityId?: string
  clientRevision?: number
  serverRevision?: number
  clientPayload: Record<string, unknown>
  serverPayload: Record<string, unknown>
  detectedAt: string
  resolved: boolean
}

export class RtcDatabase extends Dexie {
  outbox!: Table<OutboxItem, number>
  records!: Table<CachedRecord, string>
  meta!: Table<SyncMeta, string>
  conflicts!: Table<ConflictRecord, number>

  constructor() {
    super('rtc')

    this.version(1).stores({
      outbox: '++id, clientUuid, collection, status, createdAt',
      // Compound index so "everything in this collection, newest first" is a
      // single index scan rather than a full table read on a low-end phone.
      records: 'id, collection, updatedAt, [collection+updatedAt]',
      meta: 'key',
      conflicts: '++id, entityType, entityId, resolved, detectedAt',
    })
  }
}

export const db = new RtcDatabase()

/** Device identity, stable across sessions — the sync cursor is keyed on it. */
export function deviceId(): string {
  const key = 'rtc.device'
  let id = localStorage.getItem(key)
  if (!id) {
    id = crypto.randomUUID()
    localStorage.setItem(key, id)
  }
  return id
}

export function newClientUuid(): string {
  return crypto.randomUUID()
}

// --------------------------------------------------------------------------
// Cache helpers
// --------------------------------------------------------------------------
export async function cacheCollection(
  collection: string,
  rows: Record<string, unknown>[],
): Promise<number> {
  if (!rows.length) return 0
  const now = new Date().toISOString()
  const records: CachedRecord[] = rows
    .filter((row) => typeof row.id === 'string')
    .map((row) => ({
      id: row.id as string,
      collection,
      data: row,
      updatedAt: (row.updated_at as string) ?? now,
      revision: row.revision as number | undefined,
    }))
  await db.records.bulkPut(records)
  return records.length
}

export async function readCollection<T = Record<string, unknown>>(
  collection: string,
  limit = 500,
): Promise<T[]> {
  const rows = await db.records.where('collection').equals(collection).limit(limit).toArray()
  return rows
    .sort((a, b) => (a.updatedAt < b.updatedAt ? 1 : -1))
    .map((row) => row.data as T)
}

export async function readRecord<T = Record<string, unknown>>(id: string): Promise<T | undefined> {
  const row = await db.records.get(id)
  return row?.data as T | undefined
}

// --------------------------------------------------------------------------
// Outbox
// --------------------------------------------------------------------------
export async function enqueue(
  item: Omit<OutboxItem, 'id' | 'createdAt' | 'attempts' | 'status'>,
): Promise<number> {
  return db.outbox.add({
    ...item,
    createdAt: new Date().toISOString(),
    attempts: 0,
    status: 'queued',
  })
}

export async function pendingCount(): Promise<number> {
  return db.outbox.where('status').anyOf('queued', 'failed').count()
}

export async function openConflictCount(): Promise<number> {
  return db.conflicts.filter((c) => !c.resolved).count()
}

export async function clearLocalData(): Promise<void> {
  await Promise.all([
    db.outbox.clear(),
    db.records.clear(),
    db.meta.clear(),
    db.conflicts.clear(),
  ])
}

export async function getMeta<T>(key: string, fallback: T): Promise<T> {
  const row = await db.meta.get(key)
  return (row?.value as T) ?? fallback
}

export async function setMeta(key: string, value: unknown): Promise<void> {
  await db.meta.put({ key, value, updatedAt: new Date().toISOString() })
}
