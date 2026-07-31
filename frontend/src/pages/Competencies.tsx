/**
 * Competency and EPA attainment against the curriculum target for the trainee's
 * current year — the view a supervisor opens for an educational review.
 */

import { useQuery } from '@tanstack/react-query'
import { Stethoscope } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDate, formatPercent, humanise } from '@/lib/utils'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Progress,
} from '@/components/ui'
import { ProgressRing, StatTile } from '@/components/charts'

interface CompetencyProgress {
  enrolment_id: string
  training_year: number
  total: number
  met: number
  unrated: number
  percent_met: number
  by_domain: Record<string, { total: number; met: number }>
  competencies: {
    competency_id: string
    code: string
    title: string
    domain: string
    is_epa: boolean
    target_level: string
    target_value: number
    current_level: string | null
    current_value: number
    rated_on: string | null
    met: boolean
    gap: number
  }[]
}

const LEVEL_LABELS = [
  'Not yet rated',
  'Observe only',
  'Direct supervision',
  'Indirect supervision',
  'Independent',
  'Supervises others',
]

/** The entrustment scale is ordinal, so it is drawn as five discrete steps. */
function EntrustmentScale({ current, target }: { current: number; target: number }) {
  return (
    <div className="flex items-center gap-1" aria-label={`Level ${current} of a target ${target}`}>
      {[1, 2, 3, 4, 5].map((step) => {
        const reached = current >= step
        const isTarget = step === target
        return (
          <span
            key={step}
            title={LEVEL_LABELS[step]}
            className="h-2.5 w-5 rounded-[3px]"
            style={{
              background: reached
                ? current >= target
                  ? 'var(--status-good)'
                  : 'var(--status-warning)'
                : 'var(--surface-3)',
              outline: isTarget ? '1.5px solid var(--text-secondary)' : 'none',
              outlineOffset: '1px',
            }}
          />
        )
      })}
    </div>
  )
}

export default function Competencies() {
  const { principal } = useAuth()
  const enrolmentId = principal?.enrolment?.id

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['competency-progress', enrolmentId],
    queryFn: () =>
      api.get<CompetencyProgress>(`/assessments/competency-progress/${enrolmentId}`),
    enabled: Boolean(enrolmentId),
  })

  if (!enrolmentId) {
    return (
      <Card>
        <EmptyState
          icon={<Stethoscope className="h-10 w-10" />}
          title="No training enrolment"
          description="Competency progress is tracked against an enrolled curriculum."
        />
      </Card>
    )
  }

  if (isLoading) return <LoadingRows rows={8} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  const epas = data.competencies.filter((c) => c.is_epa)
  const others = data.competencies.filter((c) => !c.is_epa)

  const render = (items: CompetencyProgress['competencies']) => (
    <div className="divide-y" style={{ borderColor: 'var(--border-hairline)' }}>
      {items.map((competency) => (
        <div key={competency.competency_id} className="flex flex-wrap items-center gap-3 py-3">
          <div className="min-w-[14rem] flex-1">
            <p className="text-sm font-medium">{competency.title}</p>
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {competency.code} · {humanise(competency.domain)}
              {competency.rated_on ? ` · last rated ${formatDate(competency.rated_on)}` : ''}
            </p>
          </div>
          <EntrustmentScale current={competency.current_value} target={competency.target_value} />
          <div className="w-40 text-right">
            <p className="text-xs font-medium">
              {LEVEL_LABELS[competency.current_value] ?? 'Not yet rated'}
            </p>
            <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
              Target: {LEVEL_LABELS[competency.target_value]}
            </p>
          </div>
          <Badge tone={competency.met ? 'good' : competency.current_value === 0 ? 'neutral' : 'warning'}>
            {competency.met ? 'Met' : competency.current_value === 0 ? 'Not rated' : `${competency.gap} to go`}
          </Badge>
        </div>
      ))}
    </div>
  )

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Competencies &amp; EPAs</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Measured against the entrustment level your curriculum expects by the end of year{' '}
          {data.training_year}.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Competencies met"
          value={`${data.met} / ${data.total}`}
          tone={data.percent_met >= 75 ? 'green' : data.percent_met >= 50 ? 'amber' : 'red'}
          hint={formatPercent(data.percent_met, 0)}
        />
        <StatTile
          label="Entrustable Professional Activities"
          value={`${epas.filter((e) => e.met).length} / ${epas.length}`}
          hint="EPAs at or above the year target"
        />
        <StatTile
          label="Not yet rated"
          value={data.unrated}
          tone={data.unrated > 0 ? 'amber' : 'green'}
          hint="An unrated competency never counts as met"
        />
        <Card className="flex items-center justify-center p-4">
          <ProgressRing value={data.percent_met} sublabel="of targets" />
        </Card>
      </div>

      <Card>
        <CardHeader
          title="By domain"
          description="Where the curriculum places its emphasis, and where you stand."
        />
        <CardBody className="grid gap-4 sm:grid-cols-2">
          {Object.entries(data.by_domain).map(([domain, counts]) => (
            <div key={domain}>
              <div className="mb-1 flex items-baseline justify-between text-xs">
                <span className="font-medium">{humanise(domain)}</span>
                <span className="tnum" style={{ color: 'var(--text-muted)' }}>
                  {counts.met} / {counts.total}
                </span>
              </div>
              <Progress value={counts.total ? (counts.met / counts.total) * 100 : 0} />
            </div>
          ))}
        </CardBody>
      </Card>

      {epas.length ? (
        <Card>
          <CardHeader
            title="Entrustable Professional Activities"
            description="The tasks you must be trusted to perform. The outlined step is this year's target."
          />
          <CardBody className="pt-0">{render(epas)}</CardBody>
        </Card>
      ) : null}

      {others.length ? (
        <Card>
          <CardHeader title="Supporting competencies" />
          <CardBody className="pt-0">{render(others)}</CardBody>
        </Card>
      ) : null}
    </div>
  )
}
