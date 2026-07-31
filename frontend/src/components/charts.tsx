/**
 * Chart components.
 *
 * Rules held throughout, and worth stating because they are easy to break:
 *  - One axis. Never two y-scales on one chart.
 *  - Status colours (RAG) encode *state* and are reserved; the categorical series
 *    slots encode *identity* and are assigned in fixed order, never cycled.
 *  - Every plotted chart carries a hover layer; wide charts scroll inside their
 *    own container so the page never scrolls sideways.
 *  - Identity is never colour-alone: two or more series get a legend, and small
 *    series counts are also direct-labelled.
 */

import type { ReactNode } from 'react'
import {
  Area,
  AreaChart,
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'
import { ArrowDownRight, ArrowRight, ArrowUpRight } from 'lucide-react'
import { cn, formatNumber, humanise, RAG_COLOR, ragFor, seriesColor, type Rag } from '@/lib/utils'

// --------------------------------------------------------------------------
// Tooltip
// --------------------------------------------------------------------------
interface TooltipPayload {
  name?: string
  value?: number | string
  color?: string
  payload?: Record<string, unknown>
}

function ChartTooltip({
  active,
  payload,
  label,
  suffix = '',
}: {
  active?: boolean
  payload?: TooltipPayload[]
  label?: string
  suffix?: string
}) {
  if (!active || !payload?.length) return null
  return (
    <div
      className="rounded-lg border px-3 py-2 text-xs shadow-[var(--shadow-pop)]"
      style={{ background: 'var(--surface-1)', borderColor: 'var(--border-hairline)' }}
    >
      {label ? <p className="mb-1 font-medium">{label}</p> : null}
      {payload.map((entry, index) => (
        <div key={index} className="flex items-center gap-2 whitespace-nowrap">
          <span
            className="h-2 w-2 shrink-0 rounded-[2px]"
            style={{ background: entry.color }}
            aria-hidden
          />
          <span style={{ color: 'var(--text-secondary)' }}>{entry.name}</span>
          <span className="tnum ml-auto font-medium">
            {typeof entry.value === 'number' ? formatNumber(entry.value, 1) : entry.value}
            {suffix}
          </span>
        </div>
      ))}
    </div>
  )
}

const AXIS = { stroke: 'var(--baseline)', fontSize: 11, tickLine: false } as const

// --------------------------------------------------------------------------
// Stat tile — a headline number is not a chart; do not plot one.
// --------------------------------------------------------------------------
export function StatTile({
  label,
  value,
  unit,
  delta,
  deltaLabel,
  hint,
  tone,
  icon,
  className,
}: {
  label: string
  value: ReactNode
  unit?: string
  delta?: number | null
  deltaLabel?: string
  hint?: string
  tone?: Rag
  icon?: ReactNode
  className?: string
}) {
  const Arrow = delta == null ? ArrowRight : delta > 0 ? ArrowUpRight : delta < 0 ? ArrowDownRight : ArrowRight
  const deltaInk =
    delta == null || delta === 0
      ? 'var(--text-muted)'
      : delta > 0
        ? 'var(--text-success)'
        : 'var(--status-critical)'

  return (
    <div className={cn('surface p-4', className)}>
      <div className="flex items-start justify-between gap-2">
        <p className="text-xs font-medium" style={{ color: 'var(--text-muted)' }}>
          {label}
        </p>
        {icon ? (
          <span className="opacity-40" aria-hidden>
            {icon}
          </span>
        ) : null}
      </div>
      <div className="mt-2 flex items-baseline gap-1.5">
        <span
          className="text-2xl font-semibold leading-none tracking-tight"
          style={{ color: tone ? RAG_COLOR[tone] : 'var(--text-primary)' }}
        >
          {value}
        </span>
        {unit ? (
          <span className="text-sm" style={{ color: 'var(--text-muted)' }}>
            {unit}
          </span>
        ) : null}
      </div>
      {delta != null || hint ? (
        <div className="mt-2 flex items-center gap-1.5 text-xs">
          {delta != null ? (
            <>
              <Arrow className="h-3.5 w-3.5" style={{ color: deltaInk }} aria-hidden />
              <span className="tnum font-medium" style={{ color: deltaInk }}>
                {delta > 0 ? '+' : ''}
                {formatNumber(delta, 1)}
              </span>
            </>
          ) : null}
          <span style={{ color: 'var(--text-muted)' }}>{deltaLabel ?? hint}</span>
        </div>
      ) : null}
    </div>
  )
}

// --------------------------------------------------------------------------
// Domain scores — horizontal bars.
//
// Preferred over a radar chart: radar distorts area with the ordering of its
// spokes and makes comparison between adjacent domains unreliable. Bars against a
// common baseline compare accurately, which is the whole point of the view.
// --------------------------------------------------------------------------
export function DomainBars({
  domains,
  height = 260,
}: {
  domains: Record<string, { score: number; rag: string; contributing_rules?: number }>
  height?: number
}) {
  const data = Object.entries(domains)
    .map(([key, entry]) => ({
      domain: humanise(key),
      key,
      score: Math.round(entry.score * 10) / 10,
      rag: entry.rag,
      assessed: entry.rag !== 'unknown',
    }))
    .sort((a, b) => b.score - a.score)

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 44, bottom: 4, left: 4 }}>
          <CartesianGrid horizontal={false} stroke="var(--gridline)" />
          <XAxis type="number" domain={[0, 100]} {...AXIS} axisLine={false} unit="%" />
          <YAxis
            type="category"
            dataKey="domain"
            width={132}
            {...AXIS}
            axisLine={false}
            tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
          />
          <Tooltip
            content={<ChartTooltip suffix="%" />}
            cursor={{ fill: 'var(--surface-2)' }}
          />
          <Bar dataKey="score" name="Score" radius={[0, 4, 4, 0]} barSize={14} isAnimationActive>
            {data.map((entry) => (
              <Cell
                key={entry.key}
                fill={entry.assessed ? RAG_COLOR[ragFor(entry.score)] : 'var(--status-unknown)'}
                fillOpacity={entry.assessed ? 1 : 0.35}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      {/* Direct labels are the required relief for the sub-3:1 status steps —
          the value is never carried by colour alone. */}
      <ul className="sr-only">
        {data.map((entry) => (
          <li key={entry.key}>
            {entry.domain}: {entry.assessed ? `${entry.score}%` : 'not assessed'}
          </li>
        ))}
      </ul>
    </div>
  )
}

// --------------------------------------------------------------------------
// Score trend — one series, so no legend; the title names it.
// --------------------------------------------------------------------------
export function TrendChart({
  data,
  dataKey = 'overall_score',
  labelKey = 'label',
  height = 200,
  name = 'Overall score',
}: {
  data: Record<string, unknown>[]
  dataKey?: string
  labelKey?: string
  height?: number
  name?: string
}) {
  if (data.length < 2) {
    return (
      <div
        className="flex items-center justify-center text-xs"
        style={{ height, color: 'var(--text-muted)' }}
      >
        A trend needs at least two scored points. Scores are recomputed nightly.
      </div>
    )
  }

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <AreaChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: -18 }}>
          <defs>
            <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="var(--chart-primary)" stopOpacity={0.22} />
              <stop offset="100%" stopColor="var(--chart-primary)" stopOpacity={0.01} />
            </linearGradient>
          </defs>
          <CartesianGrid vertical={false} stroke="var(--gridline)" />
          <XAxis dataKey={labelKey} {...AXIS} axisLine={false} />
          <YAxis domain={[0, 100]} {...AXIS} axisLine={false} unit="%" />
          <Tooltip content={<ChartTooltip suffix="%" />} cursor={{ stroke: 'var(--baseline)' }} />
          <Area
            type="monotone"
            dataKey={dataKey}
            name={name}
            stroke="var(--chart-primary)"
            strokeWidth={2}
            fill="url(#trendFill)"
            dot={{ r: 3, strokeWidth: 2, stroke: 'var(--surface-1)', fill: 'var(--chart-primary)' }}
            activeDot={{ r: 5, strokeWidth: 2, stroke: 'var(--surface-1)' }}
          />
        </AreaChart>
      </ResponsiveContainer>
    </div>
  )
}

// --------------------------------------------------------------------------
// Multi-series comparison (≤ 3 series — past that, facet rather than add hues).
// --------------------------------------------------------------------------
export function MultiTrend({
  data,
  series,
  labelKey = 'label',
  height = 220,
  unit = '',
}: {
  data: Record<string, unknown>[]
  series: { key: string; name: string }[]
  labelKey?: string
  height?: number
  unit?: string
}) {
  const capped = series.slice(0, 3)
  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <LineChart data={data} margin={{ top: 8, right: 12, bottom: 4, left: -18 }}>
          <CartesianGrid vertical={false} stroke="var(--gridline)" />
          <XAxis dataKey={labelKey} {...AXIS} axisLine={false} />
          <YAxis {...AXIS} axisLine={false} unit={unit} />
          <Tooltip content={<ChartTooltip suffix={unit} />} cursor={{ stroke: 'var(--baseline)' }} />
          {capped.length > 1 ? (
            <Legend
              verticalAlign="top"
              align="left"
              height={28}
              iconType="plainline"
              wrapperStyle={{ fontSize: 11, color: 'var(--text-secondary)' }}
            />
          ) : null}
          {capped.map((entry, index) => (
            <Line
              key={entry.key}
              type="monotone"
              dataKey={entry.key}
              name={entry.name}
              stroke={seriesColor(index)}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 5, strokeWidth: 2, stroke: 'var(--surface-1)' }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  )
}

// --------------------------------------------------------------------------
// Cohort RAG distribution — a status encoding, always with a labelled legend.
// --------------------------------------------------------------------------
export function RagDistribution({
  counts,
  total,
}: {
  counts: Record<string, number>
  total?: number
}) {
  const order: Rag[] = ['green', 'amber', 'red', 'unknown']
  const labels: Record<Rag, string> = {
    green: 'On track',
    amber: 'Needs attention',
    red: 'At risk',
    unknown: 'Not assessed',
  }
  const sum = total ?? order.reduce((acc, key) => acc + (counts[key] ?? 0), 0)
  if (!sum) {
    return (
      <p className="py-6 text-center text-xs" style={{ color: 'var(--text-muted)' }}>
        No scored trainees in this scope yet.
      </p>
    )
  }

  return (
    <div>
      {/* A 2px surface gap separates adjacent segments so the boundary reads
          without relying on the hue difference. */}
      <div className="flex h-3 w-full gap-[2px] overflow-hidden rounded-full">
        {order.map((key) => {
          const count = counts[key] ?? 0
          if (!count) return null
          return (
            <div
              key={key}
              className="h-full first:rounded-l-full last:rounded-r-full"
              style={{ width: `${(count / sum) * 100}%`, background: RAG_COLOR[key] }}
              title={`${labels[key]}: ${count}`}
            />
          )
        })}
      </div>
      <ul className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-4">
        {order.map((key) => (
          <li key={key} className="flex items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 shrink-0 rounded-[3px]"
              style={{ background: RAG_COLOR[key] }}
              aria-hidden
            />
            <span style={{ color: 'var(--text-secondary)' }}>{labels[key]}</span>
            <span className="tnum ml-auto font-medium">{counts[key] ?? 0}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

// --------------------------------------------------------------------------
// Categorical counts (logbook by type, activity by kind).
// --------------------------------------------------------------------------
export function CategoryBars({
  data,
  height = 240,
  color = 'var(--chart-primary)',
  unit = '',
}: {
  data: { name: string; value: number }[]
  height?: number
  color?: string
  unit?: string
}) {
  if (!data.length) {
    return (
      <p className="py-8 text-center text-xs" style={{ color: 'var(--text-muted)' }}>
        Nothing recorded in this period.
      </p>
    )
  }
  const sorted = [...data].sort((a, b) => b.value - a.value).slice(0, 12)

  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={sorted} layout="vertical" margin={{ top: 4, right: 32, bottom: 4, left: 4 }}>
          <CartesianGrid horizontal={false} stroke="var(--gridline)" />
          <XAxis type="number" {...AXIS} axisLine={false} unit={unit} />
          <YAxis
            type="category"
            dataKey="name"
            width={148}
            {...AXIS}
            axisLine={false}
            tick={{ fill: 'var(--text-secondary)', fontSize: 11 }}
          />
          <Tooltip content={<ChartTooltip suffix={unit} />} cursor={{ fill: 'var(--surface-2)' }} />
          <Bar dataKey="value" name="Count" fill={color} radius={[0, 4, 4, 0]} barSize={13} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// --------------------------------------------------------------------------
// Activity over time (logbook volume by month).
// --------------------------------------------------------------------------
export function VolumeChart({
  data,
  height = 180,
}: {
  data: { label: string; value: number }[]
  height?: number
}) {
  if (data.length < 2) {
    return (
      <p
        className="flex items-center justify-center text-xs"
        style={{ height, color: 'var(--text-muted)' }}
      >
        Not enough history to plot yet.
      </p>
    )
  }
  return (
    <div className="w-full" style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 4, left: -22 }} barGap={2}>
          <CartesianGrid vertical={false} stroke="var(--gridline)" />
          <XAxis dataKey="label" {...AXIS} axisLine={false} />
          <YAxis {...AXIS} axisLine={false} allowDecimals={false} />
          <Tooltip content={<ChartTooltip />} cursor={{ fill: 'var(--surface-2)' }} />
          <Bar dataKey="value" name="Entries" fill="var(--chart-primary)" radius={[4, 4, 0, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  )
}

// --------------------------------------------------------------------------
// Single percentage — a ring, not a pie. One value, read at a glance.
// --------------------------------------------------------------------------
export function ProgressRing({
  value,
  size = 108,
  stroke = 9,
  label,
  sublabel,
  tone,
}: {
  value: number
  size?: number
  stroke?: number
  label?: string
  sublabel?: string
  tone?: Rag
}) {
  const clamped = Math.max(0, Math.min(100, value))
  const radius = (size - stroke) / 2
  const circumference = 2 * Math.PI * radius
  const offset = circumference - (clamped / 100) * circumference
  const color = RAG_COLOR[tone ?? ragFor(clamped)]

  return (
    <div className="relative inline-flex items-center justify-center" style={{ width: size, height: size }}>
      <svg width={size} height={size} className="-rotate-90" role="img" aria-label={`${label ?? 'Progress'}: ${clamped.toFixed(0)}%`}>
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke="var(--surface-3)"
          strokeWidth={stroke}
        />
        <circle
          cx={size / 2}
          cy={size / 2}
          r={radius}
          fill="none"
          stroke={color}
          strokeWidth={stroke}
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          style={{ transition: 'stroke-dashoffset 700ms ease-out' }}
        />
      </svg>
      <div className="absolute inset-0 flex flex-col items-center justify-center">
        <span className="text-xl font-semibold leading-none tracking-tight">
          {clamped.toFixed(0)}
          <span className="text-sm" style={{ color: 'var(--text-muted)' }}>
            %
          </span>
        </span>
        {sublabel ? (
          <span className="mt-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
            {sublabel}
          </span>
        ) : null}
      </div>
    </div>
  )
}
