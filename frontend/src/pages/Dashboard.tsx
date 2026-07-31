/**
 * The dashboard is role-aware: it renders the view that matches what the caller
 * can see, rather than one page with everything hidden behind conditionals.
 */

import { useQuery } from '@tanstack/react-query'
import {
  Activity,
  AlertTriangle,
  BookOpen,
  Building2,
  CalendarClock,
  ClipboardCheck,
  FlaskConical,
  GraduationCap,
  TrendingUp,
  Users,
} from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import {
  daysBetween,
  formatDate,
  formatNumber,
  formatPercent,
  humanise,
  ragFor,
  titleCase,
} from '@/lib/utils'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LinkButton,
  LoadingRows,
  Progress,
  RagBadge,
  Tabs,
} from '@/components/ui'
import {
  CategoryBars,
  DomainBars,
  ProgressRing,
  RagDistribution,
  StatTile,
  TrendChart,
} from '@/components/charts'
import { useState } from 'react'

// ==========================================================================
// Trainee
// ==========================================================================
interface TraineeDashboard {
  has_enrolment: boolean
  enrolment?: {
    id: string
    programme: string | null
    level: string
    year: number
    start_date: string
    expected_end_date: string
    months_served: number
  }
  scores?: {
    overall_score: number
    overall_rag: string
    promotion_readiness_score: number
    domains: Record<string, { domain: string; score: number; rag: string; contributing_rules: number }>
    gaps: { label: string; severity: string; measured: number; target: number; progress_percent: number; guidance?: string | null }[]
    metrics: Record<string, any>
    unassessed_domains: string[]
  }
  current_rotation?: {
    id: string
    name: string
    start_date: string
    end_date: string
    days_remaining: number
    supervisor: string | null
    completion_percent: number
  } | null
  pending_validations: number
  promotion: { outcome: string; readiness_percent: number; blocking_count: number }
  top_gaps: { label: string; severity: string; measured: number; target: number; progress_percent: number }[]
  upcoming_activities: { id: string; title: string; kind: string; scheduled_at: string; venue: string | null }[]
}

function TraineeView() {
  const { principal } = useAuth()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', 'trainee'],
    queryFn: () => api.get<TraineeDashboard>('/analytics/dashboard/trainee'),
  })

  const { data: history } = useQuery({
    queryKey: ['score-history', data?.enrolment?.id],
    queryFn: () =>
      api.get<any[]>(`/analytics/enrolments/${data!.enrolment!.id}/score/history`, { limit: 12 }),
    enabled: Boolean(data?.enrolment?.id),
  })

  if (isLoading) return <LoadingRows rows={6} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data?.has_enrolment) {
    return (
      <Card>
        <EmptyState
          icon={<GraduationCap className="h-10 w-10" />}
          title="No active training enrolment"
          description="Your account is not currently enrolled on a training programme. Your departmental training coordinator can enrol you."
        />
      </Card>
    )
  }

  const scores = data.scores!
  const enrolment = data.enrolment!
  const trend = (history ?? []).map((row) => ({
    label: new Date(row.computed_at).toLocaleDateString('en-GB', { month: 'short', day: 'numeric' }),
    overall_score: row.overall_score,
  }))
  const logbook = scores.metrics?.logbook ?? {}
  const monthsTotal = daysBetween(enrolment.start_date, enrolment.expected_end_date) / 30.44

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">
          Good day, {principal?.user.first_name}
        </h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          {enrolment.programme} · {titleCase(enrolment.level)} · Year {enrolment.year}
        </p>
      </header>

      {/* ---- Headline figures ------------------------------------------- */}
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Overall training score"
          value={formatNumber(scores.overall_score, 1)}
          unit="/ 100"
          tone={ragFor(scores.overall_score)}
          hint={`Weighted across ${Object.keys(scores.domains).length - scores.unassessed_domains.length} assessed domains`}
          icon={<TrendingUp className="h-4 w-4" />}
        />
        <StatTile
          label="Promotion readiness"
          value={formatNumber(data.promotion.readiness_percent, 0)}
          unit="%"
          tone={ragFor(data.promotion.readiness_percent)}
          hint={
            data.promotion.blocking_count === 0
              ? 'All mandatory gates cleared'
              : `${data.promotion.blocking_count} requirement(s) outstanding`
          }
          icon={<ClipboardCheck className="h-4 w-4" />}
        />
        <StatTile
          label="Validated logbook entries"
          value={formatNumber(logbook.validated ?? 0)}
          hint={`${formatNumber(logbook.pending ?? 0)} awaiting consultant sign-off`}
          icon={<BookOpen className="h-4 w-4" />}
        />
        <StatTile
          label="Training time served"
          value={formatNumber(enrolment.months_served)}
          unit={`of ${Math.round(monthsTotal)} months`}
          hint={`Expected completion ${formatDate(enrolment.expected_end_date)}`}
          icon={<CalendarClock className="h-4 w-4" />}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        {/* ---- Domain performance --------------------------------------- */}
        <Card className="lg:col-span-2">
          <CardHeader
            title="Performance by domain"
            description="Each bar is the weighted progress of the curriculum requirements assigned to that domain."
            action={<RagBadge rag={scores.overall_rag} />}
          />
          <CardBody>
            <DomainBars domains={scores.domains} />
            {scores.unassessed_domains.length ? (
              <p className="mt-3 text-xs" style={{ color: 'var(--text-muted)' }}>
                Not assessed by this curriculum:{' '}
                {scores.unassessed_domains.map(humanise).join(', ')}. These are excluded
                from the overall score rather than counted as zero.
              </p>
            ) : null}
          </CardBody>
        </Card>

        {/* ---- Current rotation ----------------------------------------- */}
        <Card>
          <CardHeader title="Current rotation" />
          <CardBody>
            {data.current_rotation ? (
              <div className="space-y-4">
                <div>
                  <p className="text-base font-semibold leading-tight">
                    {data.current_rotation.name}
                  </p>
                  <p className="mt-0.5 text-xs" style={{ color: 'var(--text-muted)' }}>
                    {formatDate(data.current_rotation.start_date)} –{' '}
                    {formatDate(data.current_rotation.end_date)}
                  </p>
                </div>
                <div className="flex items-center justify-center py-1">
                  <ProgressRing
                    value={data.current_rotation.completion_percent}
                    sublabel="requirements"
                  />
                </div>
                <dl className="space-y-1.5 text-xs">
                  <div className="flex justify-between gap-2">
                    <dt style={{ color: 'var(--text-muted)' }}>Supervisor</dt>
                    <dd className="text-right font-medium">
                      {data.current_rotation.supervisor ?? 'Not assigned'}
                    </dd>
                  </div>
                  <div className="flex justify-between gap-2">
                    <dt style={{ color: 'var(--text-muted)' }}>Days remaining</dt>
                    <dd className="tnum text-right font-medium">
                      {data.current_rotation.days_remaining}
                    </dd>
                  </div>
                </dl>
                <LinkButton size="sm" to="/rotations" className="w-full justify-center">
                  View full schedule
                </LinkButton>
              </div>
            ) : (
              <EmptyState
                title="No active rotation"
                description="You are not currently posted to a rotation. Your coordinator manages the schedule."
              />
            )}
          </CardBody>
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-3">
        {/* ---- Gaps ------------------------------------------------------ */}
        <Card className="lg:col-span-2">
          <CardHeader
            title="What is outstanding"
            description="Ordered by severity, then by how far short you are."
            action={
              <LinkButton size="sm" to="/logbook">
                Record activity
              </LinkButton>
            }
          />
          <CardBody className="space-y-3">
            {scores.gaps.length === 0 ? (
              <EmptyState
                title="Every requirement is met"
                description="Nothing is outstanding against your current curriculum."
              />
            ) : (
              scores.gaps.slice(0, 7).map((gap, index) => (
                <div key={index} className="space-y-1.5">
                  <div className="flex items-start justify-between gap-3">
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium">{gap.label}</p>
                      {gap.guidance ? (
                        <p className="truncate text-xs" style={{ color: 'var(--text-muted)' }}>
                          {gap.guidance}
                        </p>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 items-center gap-2">
                      <span className="tnum text-xs" style={{ color: 'var(--text-secondary)' }}>
                        {formatNumber(gap.measured, 0)} / {formatNumber(gap.target, 0)}
                      </span>
                      {gap.severity === 'mandatory' ? (
                        <Badge tone="critical">Mandatory</Badge>
                      ) : (
                        <Badge tone="neutral">Advisory</Badge>
                      )}
                    </div>
                  </div>
                  <Progress
                    value={gap.progress_percent}
                    tone={gap.severity === 'mandatory' ? ragFor(gap.progress_percent) : 'brand'}
                  />
                </div>
              ))
            )}
          </CardBody>
        </Card>

        {/* ---- Upcoming -------------------------------------------------- */}
        <Card>
          <CardHeader title="Coming up" description="Next 14 days in your department" />
          <CardBody className="space-y-3">
            {data.upcoming_activities.length === 0 ? (
              <EmptyState title="Nothing scheduled" />
            ) : (
              data.upcoming_activities.map((activity) => (
                <div key={activity.id} className="flex gap-3">
                  <div
                    className="mt-0.5 grid h-8 w-8 shrink-0 place-items-center rounded-lg"
                    style={{ background: 'var(--surface-2)' }}
                    aria-hidden
                  >
                    <Activity className="h-4 w-4" style={{ color: 'var(--brand)' }} />
                  </div>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{activity.title}</p>
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {humanise(activity.kind)} ·{' '}
                      {new Date(activity.scheduled_at).toLocaleString('en-GB', {
                        weekday: 'short',
                        day: 'numeric',
                        month: 'short',
                        hour: '2-digit',
                        minute: '2-digit',
                      })}
                    </p>
                  </div>
                </div>
              ))
            )}
          </CardBody>
        </Card>
      </div>

      {/* ---- Trend ------------------------------------------------------ */}
      <Card>
        <CardHeader
          title="Overall training score over time"
          description="Recomputed nightly and whenever a rotation closes."
        />
        <CardBody>
          <TrendChart data={trend} />
        </CardBody>
      </Card>
    </div>
  )
}

// ==========================================================================
// Supervisor
// ==========================================================================
interface SupervisorDashboard {
  supervised_count: number
  active_rotations: {
    rotation_id: string
    trainee: string | null
    enrolment_id: string
    name: string
    end_date: string
    days_remaining: number
  }[]
  pending_validations: {
    count: number
    oldest_days: number
    items: { id: string; title: string; entry_type: string; occurred_on: string }[]
  }
  draft_assessments: number
  trainees_needing_support: {
    enrolment_id: string
    trainee: string | null
    rag: string | null
    overall_score: number | null
    year: number
  }[]
}

function SupervisorView() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', 'supervisor'],
    queryFn: () => api.get<SupervisorDashboard>('/analytics/dashboard/supervisor'),
  })

  if (isLoading) return <LoadingRows rows={5} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Trainees supervised"
          value={formatNumber(data.supervised_count)}
          icon={<Users className="h-4 w-4" />}
        />
        <StatTile
          label="Entries awaiting your sign-off"
          value={formatNumber(data.pending_validations.count)}
          tone={data.pending_validations.oldest_days > 14 ? 'red' : data.pending_validations.count > 0 ? 'amber' : 'green'}
          hint={
            data.pending_validations.count
              ? `Oldest waiting ${data.pending_validations.oldest_days} days`
              : 'Queue is clear'
          }
          icon={<ClipboardCheck className="h-4 w-4" />}
        />
        <StatTile
          label="Active rotations"
          value={formatNumber(data.active_rotations.length)}
          icon={<CalendarClock className="h-4 w-4" />}
        />
        <StatTile
          label="Trainees needing support"
          value={formatNumber(data.trainees_needing_support.length)}
          tone={data.trainees_needing_support.length ? 'amber' : 'green'}
          icon={<AlertTriangle className="h-4 w-4" />}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Validation queue"
            description="Nothing counts toward a trainee's requirements until you sign it off."
            action={
              <LinkButton size="sm" variant="primary" to="/validation">
                Open queue
              </LinkButton>
            }
          />
          <CardBody className="space-y-2.5">
            {data.pending_validations.items.length === 0 ? (
              <EmptyState title="Queue is clear" description="No entries are waiting on you." />
            ) : (
              data.pending_validations.items.map((item) => (
                <div key={item.id} className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm">{item.title}</p>
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {humanise(item.entry_type)} · {formatDate(item.occurred_on)}
                    </p>
                  </div>
                  <Badge tone="warning">Pending</Badge>
                </div>
              ))
            )}
          </CardBody>
        </Card>

        <Card>
          <CardHeader
            title="Trainees who may need support"
            description="Flagged by the analytics engine, not by any single judgement."
          />
          <CardBody className="space-y-2.5">
            {data.trainees_needing_support.length === 0 ? (
              <EmptyState
                title="Everyone is on track"
                description="No supervised trainee is currently flagged."
              />
            ) : (
              data.trainees_needing_support.map((trainee) => (
                <div key={trainee.enrolment_id} className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{trainee.trainee}</p>
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      Year {trainee.year} · score {formatNumber(trainee.overall_score, 1)}
                    </p>
                  </div>
                  <RagBadge rag={trainee.rag} />
                </div>
              ))
            )}
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader title="Rotations you are supervising" />
        <CardBody className="space-y-2.5">
          {data.active_rotations.length === 0 ? (
            <EmptyState title="No active rotations assigned to you" />
          ) : (
            data.active_rotations.map((rotation) => (
              <div key={rotation.rotation_id} className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{rotation.trainee}</p>
                  <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    {rotation.name} · ends {formatDate(rotation.end_date)}
                  </p>
                </div>
                <Badge tone={rotation.days_remaining < 14 ? 'warning' : 'neutral'}>
                  {rotation.days_remaining} days left
                </Badge>
              </div>
            ))
          )}
        </CardBody>
      </Card>
    </div>
  )
}

// ==========================================================================
// Department & institution
// ==========================================================================
function DepartmentView() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', 'department'],
    queryFn: () => api.get<any>('/analytics/dashboard/department'),
  })

  if (isLoading) return <LoadingRows rows={5} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  const byYear = Object.entries(data.trainees.by_year ?? {}).map(([year, count]) => ({
    name: `Year ${year}`,
    value: Number(count),
  }))

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Active trainees"
          value={formatNumber(data.trainees.active)}
          hint={`${formatNumber(data.trainees.total)} total enrolments`}
          icon={<Users className="h-4 w-4" />}
        />
        <StatTile
          label="Mean training score"
          value={formatNumber(data.performance.mean_overall_score, 1)}
          unit="/ 100"
          tone={ragFor(data.performance.mean_overall_score)}
          icon={<TrendingUp className="h-4 w-4" />}
        />
        <StatTile
          label="Logbook validation rate"
          value={formatPercent(
            data.logbook.total ? (data.logbook.validated / data.logbook.total) * 100 : 0,
          )}
          hint={`${formatNumber(data.logbook.pending)} entries pending`}
          tone={ragFor(data.logbook.total ? (data.logbook.validated / data.logbook.total) * 100 : 0)}
          icon={<ClipboardCheck className="h-4 w-4" />}
        />
        <StatTile
          label="Research output"
          value={formatNumber(data.research.projects)}
          unit="projects"
          hint={`${formatNumber(data.research.publications)} publications`}
          icon={<FlaskConical className="h-4 w-4" />}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Cohort status"
            description="Colour-coded readiness across active trainees."
          />
          <CardBody>
            <RagDistribution counts={data.performance.rag} total={data.trainees.active} />
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Trainees by year of training" />
          <CardBody>
            <CategoryBars data={byYear} height={200} />
          </CardBody>
        </Card>
      </div>

      {data.at_risk?.length ? (
        <Card>
          <CardHeader
            title="Trainees at risk"
            description="Every entry here is actionable — open the trainee to see which requirements are unmet."
          />
          <CardBody className="space-y-2.5">
            {data.at_risk.map((trainee: any) => (
              <div key={trainee.enrolment_id} className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{trainee.trainee}</p>
                  <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                    Year {trainee.year} · score {formatNumber(trainee.score, 1)}
                  </p>
                </div>
                <RagBadge rag={trainee.rag} />
              </div>
            ))}
          </CardBody>
        </Card>
      ) : null}
    </div>
  )
}

function InstitutionView() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['dashboard', 'institution'],
    queryFn: () => api.get<any>('/analytics/dashboard/institution'),
  })

  if (isLoading) return <LoadingRows rows={5} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  const departments = (data.departments ?? []).filter((d: any) => d.active_trainees > 0)

  return (
    <div className="space-y-5">
      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Active trainees"
          value={formatNumber(data.headline.active_trainees)}
          hint={`Across ${formatNumber(data.headline.departments)} departments`}
          icon={<Users className="h-4 w-4" />}
        />
        <StatTile
          label="Mean training score"
          value={formatNumber(data.headline.mean_overall_score, 1)}
          unit="/ 100"
          tone={ragFor(data.headline.mean_overall_score)}
          icon={<TrendingUp className="h-4 w-4" />}
        />
        <StatTile
          label="Ready for promotion"
          value={formatNumber(data.headline.promotion_ready)}
          hint="All mandatory gates cleared"
          icon={<GraduationCap className="h-4 w-4" />}
        />
        <StatTile
          label="Completed training"
          value={formatNumber(data.headline.completed)}
          hint={`${formatNumber(data.headline.active_staff)} active staff`}
          icon={<Building2 className="h-4 w-4" />}
        />
      </div>

      <Card>
        <CardHeader
          title="Institution-wide readiness"
          description="Every active trainee, by colour-coded status."
        />
        <CardBody>
          <RagDistribution counts={data.rag_distribution} total={data.headline.active_trainees} />
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Departments"
          description="Mean score is across trainees with a computed scorecard."
        />
        <CardBody className="space-y-3">
          {departments.length === 0 ? (
            <EmptyState title="No department has active trainees yet" />
          ) : (
            departments.map((department: any) => (
              <div key={department.org_unit_id} className="space-y-1.5">
                <div className="flex items-center justify-between gap-3">
                  <div className="min-w-0">
                    <p className="truncate text-sm font-medium">{department.name}</p>
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {department.active_trainees} active ·{' '}
                      {department.promotion_ready} promotion-ready
                    </p>
                  </div>
                  <span className="tnum shrink-0 text-sm font-semibold">
                    {formatNumber(department.mean_score, 1)}
                  </span>
                </div>
                <Progress
                  value={department.mean_score ?? 0}
                  tone={ragFor(department.mean_score)}
                />
              </div>
            ))
          )}
        </CardBody>
      </Card>
    </div>
  )
}

// ==========================================================================
export default function Dashboard() {
  const { principal, can, isTrainee, isSupervisor } = useAuth()

  const views = [
    isTrainee && principal?.enrolment ? { id: 'trainee', label: 'My training' } : null,
    isSupervisor ? { id: 'supervisor', label: 'My trainees' } : null,
    can('analytics.department.read') ? { id: 'department', label: 'Department' } : null,
    can('analytics.institution.read') ? { id: 'institution', label: 'Institution' } : null,
  ].filter(Boolean) as { id: string; label: string }[]

  const [active, setActive] = useState(views[0]?.id ?? 'trainee')

  if (!views.length) {
    return (
      <Card>
        <EmptyState
          title="Nothing to show yet"
          description="Your account has no training enrolment and no oversight responsibilities."
        />
      </Card>
    )
  }

  return (
    <div className="space-y-5">
      {views.length > 1 ? (
        <Tabs tabs={views} active={active} onChange={setActive} />
      ) : null}
      {active === 'trainee' ? <TraineeView /> : null}
      {active === 'supervisor' ? <SupervisorView /> : null}
      {active === 'department' ? <DepartmentView /> : null}
      {active === 'institution' ? <InstitutionView /> : null}
    </div>
  )
}
