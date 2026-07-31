/**
 * The promotion engine, made legible.
 *
 * The engine recommends; a committee decides. The screen shows every gate and its
 * result so a "not recommended" is never a black box the trainee cannot argue with.
 */

import { useQuery } from '@tanstack/react-query'
import { Award, CheckCircle2, Clock, XCircle } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatNumber, humanise, ragFor } from '@/lib/utils'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Progress,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { ProgressRing, StatTile } from '@/components/charts'

interface Assessment {
  enrolment_id: string
  from_level: string
  to_level: string
  from_year: number
  to_year: number
  outcome: string
  readiness_percent: number
  rationale: string
  time_served_months: number
  minimum_months_required: number
  blocking: { label: string; measured: number; target: number; progress_percent: number }[]
  advisories: { label: string; measured: number; target: number; progress_percent: number }[]
  checks: Record<string, any>
}

const OUTCOME_TONE: Record<string, 'good' | 'warning' | 'critical' | 'neutral'> = {
  recommended: 'good',
  deferred: 'warning',
  not_recommended: 'critical',
  approved: 'good',
  declined: 'critical',
  conditional: 'warning',
}

function GateRow({
  label,
  passed,
  detail,
}: {
  label: string
  passed: boolean
  detail: string
}) {
  const Icon = passed ? CheckCircle2 : XCircle
  return (
    <div className="flex items-start gap-2.5 py-2">
      <Icon
        className="mt-0.5 h-4 w-4 shrink-0"
        style={{ color: passed ? 'var(--status-good)' : 'var(--status-critical)' }}
        aria-hidden
      />
      <div className="min-w-0">
        <p className="text-sm font-medium">{label}</p>
        <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
          {detail}
        </p>
      </div>
      <Badge tone={passed ? 'good' : 'critical'} className="ml-auto shrink-0">
        {passed ? 'Passed' : 'Not met'}
      </Badge>
    </div>
  )
}

function OwnReadiness({ enrolmentId }: { enrolmentId: string }) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['promotion', enrolmentId],
    queryFn: () => api.get<Assessment>(`/analytics/enrolments/${enrolmentId}/promotion`),
  })

  const { data: eligibility } = useQuery({
    queryKey: ['exam-eligibility', enrolmentId],
    queryFn: () => api.get<any>(`/analytics/enrolments/${enrolmentId}/exam-eligibility`),
  })

  if (isLoading) return <LoadingRows rows={5} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  const checks = data.checks

  return (
    <div className="space-y-5">
      <Card>
        <CardHeader
          title={`${humanise(data.from_level)} → ${humanise(data.to_level)}`}
          description={`Year ${data.from_year} to year ${data.to_year}`}
          action={<Badge tone={OUTCOME_TONE[data.outcome] ?? 'neutral'}>{humanise(data.outcome)}</Badge>}
        />
        <CardBody className="flex flex-col gap-6 sm:flex-row sm:items-center">
          <ProgressRing
            value={data.readiness_percent}
            size={128}
            sublabel="readiness"
            tone={ragFor(data.readiness_percent)}
          />
          <div className="min-w-0 flex-1">
            <p className="text-sm leading-relaxed">{data.rationale}</p>
            <div className="mt-4 grid gap-3 sm:grid-cols-3">
              <div>
                <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                  Time served
                </p>
                <p className="tnum text-sm font-medium">
                  {data.time_served_months} / {data.minimum_months_required} months
                </p>
              </div>
              <div>
                <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                  Requirements met
                </p>
                <p className="tnum text-sm font-medium">
                  {checks.requirements?.met} / {checks.requirements?.total}
                </p>
              </div>
              <div>
                <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                  Rotations closed
                </p>
                <p className="tnum text-sm font-medium">
                  {checks.rotations?.completed} / {checks.rotations?.planned}
                </p>
              </div>
            </div>
          </div>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Promotion gates"
          description="Every gate must pass. A committee can override, but must record why."
        />
        <CardBody className="divide-y" style={{ borderColor: 'var(--border-hairline)' }}>
          <GateRow
            label="Curriculum requirements"
            passed={Boolean(checks.requirements?.passed)}
            detail={`${checks.requirements?.met} of ${checks.requirements?.total} met · ${checks.requirements?.blocking_unmet} mandatory outstanding`}
          />
          <GateRow
            label="Minimum training time"
            passed={Boolean(checks.time_served?.passed)}
            detail={`${checks.time_served?.months_served} months served against a minimum of ${checks.time_served?.months_required}${
              checks.time_served?.interruption_days
                ? ` (${checks.time_served.interruption_days} days of approved interruption excluded)`
                : ''
            }`}
          />
          <GateRow
            label="Rotation completion"
            passed={Boolean(checks.rotations?.passed)}
            detail={
              checks.rotations?.outstanding?.length
                ? `Awaiting sign-off: ${checks.rotations.outstanding.map((r: any) => r.name).join(', ')}`
                : 'All rotations for this year are closed as completed'
            }
          />
          <GateRow
            label="Enrolment standing"
            passed={Boolean(checks.standing?.passed)}
            detail={`Status is ${humanise(checks.standing?.status)}`}
          />
        </CardBody>
      </Card>

      {data.blocking.length ? (
        <Card>
          <CardHeader
            title="What is blocking promotion"
            description="Each of these must be met before the engine will recommend you."
          />
          <CardBody className="space-y-3">
            {data.blocking.map((item, index) => (
              <div key={index} className="space-y-1.5">
                <div className="flex items-baseline justify-between gap-3">
                  <p className="text-sm font-medium">{item.label}</p>
                  <span className="tnum shrink-0 text-xs" style={{ color: 'var(--text-secondary)' }}>
                    {formatNumber(item.measured, 1)} / {formatNumber(item.target, 1)}
                  </span>
                </div>
                <Progress value={item.progress_percent} tone={ragFor(item.progress_percent)} />
              </div>
            ))}
          </CardBody>
        </Card>
      ) : null}

      {eligibility ? (
        <Card>
          <CardHeader
            title="College examination eligibility"
            description={
              eligibility.awarding_body
                ? `Assessed against ${String(eligibility.awarding_body).toUpperCase()} requirements`
                : undefined
            }
            action={
              <Badge tone={eligibility.eligible ? 'good' : 'critical'}>
                {eligibility.eligible ? 'Eligible' : 'Not yet eligible'}
              </Badge>
            }
          />
          <CardBody>
            {eligibility.requirements?.length ? (
              <div className="divide-y" style={{ borderColor: 'var(--border-hairline)' }}>
                {eligibility.requirements.map((requirement: any, index: number) => (
                  <div key={index} className="flex items-center gap-3 py-2">
                    {requirement.met ? (
                      <CheckCircle2 className="h-4 w-4 shrink-0" style={{ color: 'var(--status-good)' }} />
                    ) : (
                      <Clock className="h-4 w-4 shrink-0" style={{ color: 'var(--status-warning)' }} />
                    )}
                    <p className="min-w-0 flex-1 text-sm">{requirement.label}</p>
                    <span className="tnum shrink-0 text-xs" style={{ color: 'var(--text-muted)' }}>
                      {formatNumber(requirement.measured, 1)} / {formatNumber(requirement.target, 1)}
                    </span>
                  </div>
                ))}
              </div>
            ) : (
              <EmptyState
                title="No exam eligibility rules defined"
                description="Your programme has not yet configured examination eligibility requirements."
              />
            )}
          </CardBody>
        </Card>
      ) : null}
    </div>
  )
}

function CohortReadiness() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['promotion-cohort'],
    queryFn: () => api.get<any>('/analytics/promotion/cohort'),
  })

  if (isLoading) return <LoadingRows rows={6} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Assessed" value={formatNumber(data.total)} />
        <StatTile
          label="Recommended"
          value={formatNumber(data.summary.recommended ?? 0)}
          tone="green"
        />
        <StatTile label="Deferred" value={formatNumber(data.summary.deferred ?? 0)} tone="amber" />
        <StatTile
          label="Not recommended"
          value={formatNumber(data.summary.not_recommended ?? 0)}
          tone={(data.summary.not_recommended ?? 0) > 0 ? 'red' : 'green'}
        />
      </div>

      <Card>
        <CardHeader
          title="Cohort readiness"
          description="Computed live from each trainee's curriculum — no manual calculation."
        />
        {!data.trainees?.length ? (
          <EmptyState title="No active trainees in this scope" />
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th>Trainee</Th>
                <Th>Transition</Th>
                <Th>Time served</Th>
                <Th>Readiness</Th>
                <Th>Blocking</Th>
                <Th>Engine verdict</Th>
              </tr>
            </thead>
            <tbody>
              {data.trainees.map((trainee: any) => (
                <tr key={trainee.enrolment_id}>
                  <Td className="font-medium">{trainee.trainee}</Td>
                  <Td className="text-xs">
                    {humanise(trainee.from_level)} → {humanise(trainee.to_level)}
                  </Td>
                  <Td className="tnum text-xs">
                    {trainee.months_served} / {trainee.months_required} mo
                  </Td>
                  <Td className="w-36">
                    <div className="flex items-center gap-2">
                      <span className="tnum w-9 text-xs font-medium">
                        {formatNumber(trainee.readiness_percent, 0)}%
                      </span>
                      <div className="flex-1">
                        <Progress
                          value={trainee.readiness_percent}
                          tone={ragFor(trainee.readiness_percent)}
                        />
                      </div>
                    </div>
                  </Td>
                  <Td className="tnum text-xs">{trainee.blocking_count}</Td>
                  <Td>
                    <Badge tone={OUTCOME_TONE[trainee.outcome] ?? 'neutral'}>
                      {humanise(trainee.outcome)}
                    </Badge>
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>
    </div>
  )
}

export default function Promotion() {
  const { principal, can } = useAuth()

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Promotion</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          The engine evaluates every gate and shows its reasoning. Committees ratify; any
          departure from the recommendation is recorded with a reason.
        </p>
      </header>

      {principal?.enrolment ? (
        <OwnReadiness enrolmentId={principal.enrolment.id} />
      ) : can('promotion.readiness.read') ? (
        <CohortReadiness />
      ) : (
        <Card>
          <EmptyState
            icon={<Award className="h-10 w-10" />}
            title="Nothing to show"
            description="You have no training enrolment and no promotion oversight responsibilities."
          />
        </Card>
      )}

      {principal?.enrolment && can('promotion.decide') ? <CohortReadiness /> : null}
    </div>
  )
}
