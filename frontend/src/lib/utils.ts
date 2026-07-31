import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// --------------------------------------------------------------------------
// Formatting
// --------------------------------------------------------------------------
const dateFormatter = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
})

const dateTimeFormatter = new Intl.DateTimeFormat('en-GB', {
  day: 'numeric',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

export function formatDate(value?: string | Date | null): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  return Number.isNaN(date.getTime()) ? '—' : dateFormatter.format(date)
}

export function formatDateTime(value?: string | Date | null): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  return Number.isNaN(date.getTime()) ? '—' : dateTimeFormatter.format(date)
}

export function formatRelative(value?: string | Date | null): string {
  if (!value) return '—'
  const date = typeof value === 'string' ? new Date(value) : value
  if (Number.isNaN(date.getTime())) return '—'

  const seconds = Math.round((date.getTime() - Date.now()) / 1000)
  const abs = Math.abs(seconds)
  const rtf = new Intl.RelativeTimeFormat('en-GB', { numeric: 'auto' })

  if (abs < 60) return rtf.format(seconds, 'second')
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute')
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour')
  if (abs < 2592000) return rtf.format(Math.round(seconds / 86400), 'day')
  if (abs < 31536000) return rtf.format(Math.round(seconds / 2592000), 'month')
  return rtf.format(Math.round(seconds / 31536000), 'year')
}

export function formatNumber(value?: number | null, decimals = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return value.toLocaleString('en-GB', {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  })
}

export function formatPercent(value?: number | null, decimals = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${value.toFixed(decimals)}%`
}

/** "senior_registrar" → "Senior registrar" */
export function humanise(value?: string | null): string {
  if (!value) return '—'
  const text = value.replace(/_/g, ' ').trim()
  return text.charAt(0).toUpperCase() + text.slice(1)
}

/** "senior_registrar" → "Senior Registrar" — for names and titles. */
export function titleCase(value?: string | null): string {
  if (!value) return '—'
  return value
    .replace(/_/g, ' ')
    .split(' ')
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ')
}

export function initials(name?: string | null): string {
  if (!name) return '?'
  const parts = name.replace(/^(Dr|Prof|Mr|Mrs|Ms)\.?\s+/i, '').split(/\s+/).filter(Boolean)
  if (!parts.length) return '?'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

export function daysBetween(from: string | Date, to: string | Date = new Date()): number {
  const a = typeof from === 'string' ? new Date(from) : from
  const b = typeof to === 'string' ? new Date(to) : to
  return Math.round((b.getTime() - a.getTime()) / 86_400_000)
}

// --------------------------------------------------------------------------
// RAG status
// --------------------------------------------------------------------------
export type Rag = 'green' | 'amber' | 'red' | 'unknown'

export const RAG_LABEL: Record<Rag, string> = {
  green: 'On track',
  amber: 'Needs attention',
  red: 'At risk',
  unknown: 'Not assessed',
}

/** Status colours are reserved; they are never reused as a series colour. */
export const RAG_COLOR: Record<Rag, string> = {
  green: 'var(--status-good)',
  amber: 'var(--status-warning)',
  red: 'var(--status-critical)',
  unknown: 'var(--status-unknown)',
}

export const RAG_WASH: Record<Rag, string> = {
  green: 'var(--status-good-wash)',
  amber: 'var(--status-warning-wash)',
  red: 'var(--status-critical-wash)',
  unknown: 'var(--status-unknown-wash)',
}

export function ragFor(score?: number | null): Rag {
  if (score === null || score === undefined) return 'unknown'
  if (score >= 75) return 'green'
  if (score >= 55) return 'amber'
  return 'red'
}

/** The categorical series slots, in fixed order. Never cycled past slot 8. */
export const SERIES = [
  'var(--series-1)',
  'var(--series-2)',
  'var(--series-3)',
  'var(--series-4)',
  'var(--series-5)',
  'var(--series-6)',
  'var(--series-7)',
  'var(--series-8)',
] as const

export function seriesColor(index: number): string {
  return SERIES[index] ?? 'var(--text-muted)'
}

// --------------------------------------------------------------------------
export function download(filename: string, content: string, mime = 'text/csv'): void {
  const blob = new Blob([content], { type: `${mime};charset=utf-8;` })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export function toCsv(rows: Record<string, unknown>[]): string {
  if (!rows.length) return ''
  const headers = Object.keys(rows[0])
  const escape = (value: unknown) => {
    const text = value === null || value === undefined ? '' : String(value)
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text
  }
  return [
    headers.join(','),
    ...rows.map((row) => headers.map((header) => escape(row[header])).join(',')),
  ].join('\n')
}
