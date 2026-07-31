/**
 * Examination readiness.
 *
 * Two presentation decisions carry the honesty of the whole screen.
 *
 * The confidence interval is drawn, not tucked into a tooltip. A score of 82
 * built on two logbook entries and one CBT sitting is genuinely uncertain, and
 * a bare number would tell a trainee something the platform does not know.
 *
 * Unassessed components are listed separately with an explanation rather than
 * shown as zero bars. A department that has not run a journal club has produced
 * no evidence; a 0% bar would read as a failing on the trainee's part.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Gauge, Info, TrendingDown, TrendingUp } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatNumber, humanise } from '@/lib/utils'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Progress,
  Select,
} from '@/components/ui'
import { StatTile } from '@/components/charts'

interface Component {
  label: string
  weight: number
  score: number | null
  assessed: boolean
  evidence_count: number
  evidence_target: number
  evidence_ratio: number
  detail: Record<string, unknown>
}

interface Factor {
  component: string
  label: string
  status: 'assessed' | 'unassessed'
  current_score: number | null
  weight: number
  headroom: number | null
  readiness_gain_if_improved: number | null
  action: string
}

interface Readiness {
  user_id: string
  as_of: string
  score: number
  category: string
  confidence_low: number
  confidence_high: number
  evidence_coverage: number
  components: Record<string, Component>
  unassessed_components: string[]
  influential_factors: Factor[]
  indices: Record<string, number | null>
  weights_used: Record<string, number>
  category_boundaries: Record<string, number>
  notes: string[]
}

/**
 * Category colours.
 *
 * These are the reserved status colours, used here for their reserved meaning —
 * a readiness category *is* a status. They are never reused as a series colour
 * anywhere in this file, and every one is paired with its written label so the
 * category survives being read in greyscale or by a colour-blind reader.
 */
const CATEGORY_TONE: Record<string, 'good' | 'warning' | 'critical' | 'neutral'> = {
  outstanding: 'good',
  examination_ready: 'good',
  nearly_ready: 'warning',
  needs_improvement: 'warning',
  intensive_remediation: 'critical',
}

const INDEX_LABELS: Record<string, string> = {
  knowledge: 'Knowledge',
  clinical_competency: 'Clinical competency',
  procedural_competency: 'Procedural competency',
  critical_thinking: 'Critical thinking',
  consistency: 'Consistency',
  improvement_rate: 'Improvement rate',
  learning_velocity: 'Learning velocity',
  retention: 'Retention',
  examination_prediction: 'Examination prediction',
}

/** Indices measured in their own units rather than on a 0-100 scale. */
const NON_PERCENT_INDICES = new Set(['improvement_rate', 'learning_velocity'])

export default function Readiness() {
  const { principal, can } = useAuth()
  const [windowDays, setWindowDays] = useState('90')
  const canViewOthers = can('analytics.supervised.read')

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['readiness', windowDays],
    queryFn: () =>
      api.get<Readiness>('/learning/readiness', { window_days: Number(windowDays) }),
  })

  if (isLoading) return <LoadingRows rows={8} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return <EmptyState title="No readiness data" />

  const tone = CATEGORY_TONE[data.category] ?? 'neutral'
  const bandWidth = data.confidence_high - data.confidence_low
  const assessed = Object.entries(data.components).filter(([, c]) => c.assessed)
  const unassessed = Object.entries(data.components).filter(([, c]) => !c.assessed)

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold text-[var(--text-primary)]">
            Examination readiness
          </h1>
          <p className="mt-1 text-sm text-[var(--text-secondary)]">
            {canViewOthers
              ? 'Your readiness. Supervisors can view a trainee from the analytics screen.'
              : 'How ready you are for your next examination, and what would move it most.'}
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm text-[var(--text-secondary)]">
          Window
          <Select value={windowDays} onChange={(e) => setWindowDays(e.target.value)}>
            <option value="30">30 days</option>
            <option value="90">90 days</option>
            <option value="180">180 days</option>
            <option value="365">1 year</option>
          </Select>
        </label>
      </header>

      {/* ---- The headline number, with its interval ---------------------- */}
      <Card>
        <CardBody className="space-y-5">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-2">
            <span className="text-5xl font-semibold tabular-nums text-[var(--text-primary)]">
              {formatNumber(data.score, 1)}
            </span>
            <span className="text-lg text-[var(--text-secondary)]">/ 100</span>
            <Badge tone={tone}>{humanise(data.category)}</Badge>
            <span className="text-sm text-[var(--text-muted)]">
              as at {new Date(data.as_of).toLocaleDateString()}
            </span>
          </div>

          {/* The interval as a drawn band. A point estimate would overstate
              what is known when evidence is thin. */}
          <div>
            <div className="relative h-8">
              <div className="absolute inset-x-0 top-3 h-2 rounded-full bg-[var(--surface-3)]" />
              <div
                className="absolute top-3 h-2 rounded-full bg-[var(--chart-primary)] opacity-30"
                style={{
                  left: `${data.confidence_low}%`,
                  width: `${Math.max(bandWidth, 0.5)}%`,
                }}
              />
              <div
                className="absolute top-1 h-6 w-1 rounded-full bg-[var(--chart-primary)]"
                style={{ left: `calc(${data.score}% - 2px)` }}
              />
            </div>
            <p className="mt-1 text-sm text-[var(--text-secondary)]">
              Likely between{' '}
              <strong className="tabular-nums">{formatNumber(data.confidence_low, 1)}</strong>{' '}
              and{' '}
              <strong className="tabular-nums">{formatNumber(data.confidence_high, 1)}</strong>.
              {bandWidth > 15 && (
                <>
                  {' '}
                  That is a wide band: there is not yet enough evidence to be
                  precise.
                </>
              )}
            </p>
          </div>

          <div className="grid gap-3 sm:grid-cols-3">
            <StatTile
              label="Evidence coverage"
              value={`${formatNumber(data.evidence_coverage * 100, 0)}%`}
              hint="Share of the weighting that has real evidence behind it"
            />
            <StatTile
              label="Components assessed"
              value={`${assessed.length} of ${Object.keys(data.components).length}`}
            />
            <StatTile
              label="Interval width"
              value={`${formatNumber(bandWidth, 1)} points`}
              hint="Narrows as evidence accumulates"
            />
          </div>
        </CardBody>
      </Card>

      {/* ---- What would move it most ------------------------------------ */}
      <Card>
        <CardHeader
          title="What would move this most"
          description="Ranked by the effect on your score, not by how heavily each is weighted."
        />
        <CardBody className="space-y-3">
          {data.influential_factors.slice(0, 5).map((factor) => (
            <div
              key={factor.component}
              className="rounded-lg border border-[var(--border-hairline)] p-3"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <span className="font-medium text-[var(--text-primary)]">
                  {factor.label}
                </span>
                <span className="flex items-center gap-2 text-sm">
                  {factor.status === 'assessed' ? (
                    <>
                      <span className="tabular-nums text-[var(--text-secondary)]">
                        {formatNumber(factor.current_score ?? 0, 0)}%
                      </span>
                      {(factor.readiness_gain_if_improved ?? 0) > 0 && (
                        <Badge tone="neutral">
                          <TrendingUp className="mr-1 h-3 w-3" aria-hidden />+
                          {formatNumber(factor.readiness_gain_if_improved ?? 0, 1)}
                        </Badge>
                      )}
                    </>
                  ) : (
                    <Badge tone="neutral">No evidence yet</Badge>
                  )}
                </span>
              </div>
              <p className="mt-1 text-sm text-[var(--text-secondary)]">{factor.action}</p>
            </div>
          ))}
        </CardBody>
      </Card>

      {/* ---- Assessed components ---------------------------------------- */}
      <Card>
        <CardHeader
          title="Assessed components"
          description="Each contributes at the weight your institution has set."
        />
        <CardBody className="space-y-4">
          {assessed.map(([key, component]) => (
            <div key={key}>
              <div className="flex items-baseline justify-between gap-3 text-sm">
                <span className="font-medium text-[var(--text-primary)]">
                  {component.label}
                </span>
                <span className="text-[var(--text-secondary)]">
                  <span className="tabular-nums">
                    {formatNumber(component.score ?? 0, 1)}%
                  </span>
                  <span className="ml-2 text-[var(--text-muted)]">
                    weight {formatNumber(component.weight * 100, 0)}%
                  </span>
                </span>
              </div>
              <Progress value={component.score ?? 0} />
              <p className="mt-1 text-xs text-[var(--text-muted)]">
                {component.evidence_count} of {component.evidence_target} records
                toward a fully-established measure
                {component.evidence_ratio < 1 && ' — this widens the interval above'}
              </p>
            </div>
          ))}
        </CardBody>
      </Card>

      {/* ---- Unassessed components -------------------------------------- */}
      {unassessed.length > 0 && (
        <Card>
          <CardHeader
            title="Not yet assessed"
            description="Excluded from the score, and the remaining weights renormalised."
          />
          <CardBody className="space-y-3">
            <p className="flex gap-2 rounded-lg bg-[var(--surface-2)] p-3 text-sm text-[var(--text-secondary)]">
              <Info className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span>
                These are <strong>not</strong> scored as zero. No evidence and poor
                performance are different things, and only the second is your
                responsibility.
              </span>
            </p>
            <ul className="space-y-2">
              {unassessed.map(([key, component]) => (
                <li key={key} className="flex items-center justify-between gap-3 text-sm">
                  <span className="text-[var(--text-primary)]">{component.label}</span>
                  <span className="text-[var(--text-muted)]">
                    would carry {formatNumber(component.weight * 100, 0)}%
                  </span>
                </li>
              ))}
            </ul>
          </CardBody>
        </Card>
      )}

      {/* ---- The nine indices ------------------------------------------- */}
      <Card>
        <CardHeader
          title="Learning indices"
          description="Named views over the same evidence. A dash means not yet measurable."
        />
        <CardBody>
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {Object.entries(data.indices).map(([key, value]) => (
              <div key={key} className="rounded-lg border border-[var(--border-hairline)] p-3">
                <dt className="text-sm text-[var(--text-secondary)]">
                  {INDEX_LABELS[key] ?? humanise(key)}
                </dt>
                <dd className="mt-1 text-xl font-medium tabular-nums text-[var(--text-primary)]">
                  {value === null ? (
                    <span className="text-base text-[var(--text-muted)]">
                      Not yet measurable
                    </span>
                  ) : NON_PERCENT_INDICES.has(key) ? (
                    <span className="flex items-center gap-1">
                      {value > 0 ? (
                        <TrendingUp className="h-4 w-4" aria-hidden />
                      ) : value < 0 ? (
                        <TrendingDown className="h-4 w-4" aria-hidden />
                      ) : null}
                      {formatNumber(value, 2)}
                    </span>
                  ) : (
                    `${formatNumber(value, 1)}%`
                  )}
                </dd>
              </div>
            ))}
          </dl>
        </CardBody>
      </Card>

      {/* ---- How it is computed ----------------------------------------- */}
      <Card>
        <CardHeader title="How this is computed" />
        <CardBody className="space-y-2 text-sm text-[var(--text-secondary)]">
          {data.notes.map((note) => (
            <p key={note} className="flex gap-2">
              <Gauge className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              <span>{note}</span>
            </p>
          ))}
          <table className="mt-3 w-full text-left">
            <caption className="sr-only">Weighting in force</caption>
            <thead>
              <tr className="text-xs uppercase tracking-wide text-[var(--text-muted)]">
                <th scope="col" className="py-1">Component</th>
                <th scope="col" className="py-1 text-right">Weight</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(data.weights_used).map(([key, weight]) => (
                <tr key={key} className="border-t border-[var(--border-hairline)]">
                  <td className="py-1">{data.components[key]?.label ?? humanise(key)}</td>
                  <td className="py-1 text-right tabular-nums">
                    {formatNumber(weight * 100, 0)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-xs text-[var(--text-muted)]">
            Signed in as {principal?.user.full_name ?? 'this trainee'}. Weights are set per
            institution and per curriculum version; changing them does not require
            a software change.
          </p>
        </CardBody>
      </Card>
    </div>
  )
}
