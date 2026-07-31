/**
 * Accreditation returns.
 *
 * A department head opens this before a college visit. It has to answer one
 * question honestly: what would the inspectors find, and what is missing?
 */

import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Building2, Download, FileCheck2 } from 'lucide-react'

import { api } from '@/lib/api'
import { download, formatNumber, humanise } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  Input,
  LoadingRows,
  Progress,
  RagBadge,
  Select,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { ProgressRing, StatTile } from '@/components/charts'

interface Review {
  review_id: string | null
  org_unit: { id: string; name: string; code: string }
  profile: { id: string; body: string; name: string; version: string }
  period: { start: string; end: string }
  compliance_percent: number
  essential_met: number
  essential_total: number
  readiness_rag: string
  criteria: {
    code: string
    section: string
    title: string
    metric: string
    operator: string
    measured: number
    target: number
    unit: string | null
    weighting: string
    met: boolean
    detail: Record<string, unknown>
  }[]
  gaps: { code: string; title: string; measured: number; target: number; unit: string | null; weighting: string; shortfall: number }[]
  narrative: string
}

export default function Accreditation() {
  const today = new Date().toISOString().slice(0, 10)
  const yearAgo = new Date(Date.now() - 365 * 86_400_000).toISOString().slice(0, 10)

  const [orgUnitId, setOrgUnitId] = useState('')
  const [profileId, setProfileId] = useState('')
  const [periodStart, setPeriodStart] = useState(yearAgo)
  const [periodEnd, setPeriodEnd] = useState(today)
  const [review, setReview] = useState<Review | null>(null)

  const { data: units } = useQuery({
    queryKey: ['org-units', 'department'],
    queryFn: () => api.get<any[]>('/tenancy/org-units', { kind: 'department' }),
  })

  const { data: profiles } = useQuery({
    queryKey: ['accreditation-profiles'],
    queryFn: () => api.get<any[]>('/accreditation/profiles'),
  })

  const generate = useMutation({
    mutationFn: () =>
      api.post<Review>(
        `/accreditation/reviews?org_unit_id=${orgUnitId}&profile_id=${profileId}` +
          `&period_start=${periodStart}&period_end=${periodEnd}&persist=true`,
      ),
    onSuccess: setReview,
  })

  const bySection = (review?.criteria ?? []).reduce<Record<string, Review['criteria']>>(
    (acc, criterion) => {
      ;(acc[criterion.section] ??= []).push(criterion)
      return acc
    },
    {},
  )

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Accreditation</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Every figure is drawn from validated records — the same numbers a college
          inspector would arrive at.
        </p>
      </header>

      <Card>
        <CardHeader
          title="Generate a return"
          description="Choose a department, the standard it is assessed against, and the review period."
        />
        <CardBody>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
            <Field label="Department" required className="lg:col-span-2">
              <Select value={orgUnitId} onChange={(event) => setOrgUnitId(event.target.value)}>
                <option value="">Choose…</option>
                {(units ?? []).map((unit) => (
                  <option key={unit.id} value={unit.id}>
                    {unit.name}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Standard" required className="lg:col-span-2">
              <Select value={profileId} onChange={(event) => setProfileId(event.target.value)}>
                <option value="">Choose…</option>
                {(profiles ?? []).map((profile) => (
                  <option key={profile.id} value={profile.id}>
                    {profile.body_name} — {profile.name} (v{profile.version})
                  </option>
                ))}
              </Select>
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="From">
                <Input
                  type="date"
                  value={periodStart}
                  onChange={(event) => setPeriodStart(event.target.value)}
                />
              </Field>
              <Field label="To">
                <Input
                  type="date"
                  value={periodEnd}
                  onChange={(event) => setPeriodEnd(event.target.value)}
                />
              </Field>
            </div>
          </div>
          {generate.isError ? (
            <div className="mt-3">
              <ErrorState error={generate.error} />
            </div>
          ) : null}
          <div className="mt-4 flex justify-end">
            <Button
              variant="primary"
              icon={<FileCheck2 className="h-4 w-4" />}
              disabled={!orgUnitId || !profileId}
              loading={generate.isPending}
              onClick={() => generate.mutate()}
            >
              Generate return
            </Button>
          </div>
        </CardBody>
      </Card>

      {generate.isPending ? (
        <Card>
          <LoadingRows rows={6} />
        </Card>
      ) : null}

      {review ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile
              label="Essential criteria met"
              value={`${review.essential_met} / ${review.essential_total}`}
              tone={review.readiness_rag as 'green' | 'amber' | 'red'}
            />
            <StatTile
              label="Compliance"
              value={formatNumber(review.compliance_percent, 1)}
              unit="%"
              tone={review.readiness_rag as 'green' | 'amber' | 'red'}
            />
            <StatTile label="Gaps" value={formatNumber(review.gaps.length)} />
            <Card className="flex items-center justify-center p-4">
              <ProgressRing
                value={review.compliance_percent}
                sublabel="compliance"
                tone={review.readiness_rag as 'green' | 'amber' | 'red'}
              />
            </Card>
          </div>

          <Card>
            <CardHeader
              title={`${review.profile.name} — ${review.org_unit.name}`}
              description={`${review.period.start} to ${review.period.end} · version ${review.profile.version}`}
              action={
                <div className="flex items-center gap-2">
                  <RagBadge rag={review.readiness_rag} />
                  <Button
                    size="sm"
                    icon={<Download className="h-3.5 w-3.5" />}
                    onClick={() =>
                      download(
                        `accreditation-${review.org_unit.code}-${review.profile.body}.csv`,
                        toCsvCriteria(review),
                      )
                    }
                  >
                    Export
                  </Button>
                </div>
              }
            />
            <CardBody>
              <pre
                className="whitespace-pre-wrap rounded-[var(--radius-control)] border p-4 text-xs leading-relaxed"
                style={{
                  background: 'var(--surface-2)',
                  borderColor: 'var(--border-hairline)',
                  fontFamily: 'var(--font-sans)',
                }}
              >
                {review.narrative}
              </pre>
            </CardBody>
          </Card>

          {Object.entries(bySection).map(([section, criteria]) => (
            <Card key={section}>
              <CardHeader
                title={section}
                description={`${criteria.filter((c) => c.met).length} of ${criteria.length} criteria met`}
              />
              <TableWrap>
                <thead>
                  <tr>
                    <Th>Code</Th>
                    <Th>Criterion</Th>
                    <Th className="text-right">Recorded</Th>
                    <Th className="text-right">Required</Th>
                    <Th>Weighting</Th>
                    <Th>Result</Th>
                  </tr>
                </thead>
                <tbody>
                  {criteria.map((criterion) => (
                    <tr key={criterion.code}>
                      <Td className="tnum text-xs font-medium">{criterion.code}</Td>
                      <Td>
                        <p className="text-sm">{criterion.title}</p>
                        <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                          {humanise(criterion.metric)}
                          {criterion.detail?.error ? (
                            <span style={{ color: 'var(--status-critical)' }}>
                              {' '}
                              · {String(criterion.detail.error)}
                            </span>
                          ) : null}
                        </p>
                      </Td>
                      <Td className="tnum text-right text-sm">
                        {formatNumber(criterion.measured, 1)}
                        {criterion.unit ? (
                          <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                            {' '}
                            {criterion.unit}
                          </span>
                        ) : null}
                      </Td>
                      <Td className="tnum text-right text-sm">
                        {criterion.operator === 'lte' ? '≤ ' : '≥ '}
                        {formatNumber(criterion.target, 1)}
                      </Td>
                      <Td>
                        <Badge tone={criterion.weighting === 'essential' ? 'info' : 'neutral'}>
                          {humanise(criterion.weighting)}
                        </Badge>
                      </Td>
                      <Td>
                        <Badge tone={criterion.met ? 'good' : criterion.weighting === 'essential' ? 'critical' : 'warning'}>
                          {criterion.met ? 'Met' : 'Not met'}
                        </Badge>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </TableWrap>
            </Card>
          ))}

          {review.gaps.length ? (
            <Card>
              <CardHeader
                title="Gaps, ranked"
                description="Essential criteria first, then by how far short the department is."
              />
              <CardBody className="space-y-3">
                {review.gaps.map((gap) => (
                  <div key={gap.code} className="space-y-1.5">
                    <div className="flex items-baseline justify-between gap-3">
                      <p className="text-sm font-medium">
                        <span className="tnum" style={{ color: 'var(--text-muted)' }}>
                          {gap.code}
                        </span>{' '}
                        {gap.title}
                      </p>
                      <span className="tnum shrink-0 text-xs" style={{ color: 'var(--text-secondary)' }}>
                        {formatNumber(gap.measured, 1)} / {formatNumber(gap.target, 1)}{' '}
                        {gap.unit ?? ''}
                      </span>
                    </div>
                    <Progress
                      value={gap.target ? (gap.measured / gap.target) * 100 : 0}
                      tone={gap.weighting === 'essential' ? 'red' : 'amber'}
                    />
                  </div>
                ))}
              </CardBody>
            </Card>
          ) : null}
        </>
      ) : !generate.isPending ? (
        <Card>
          <EmptyState
            icon={<Building2 className="h-10 w-10" />}
            title="No return generated yet"
            description="Choose a department and a standard above. Returns are computed live from validated records."
          />
        </Card>
      ) : null}
    </div>
  )
}

function toCsvCriteria(review: Review): string {
  const header = 'code,section,criterion,metric,operator,measured,target,unit,weighting,met'
  const rows = review.criteria.map((c) =>
    [
      c.code,
      `"${c.section}"`,
      `"${c.title.replace(/"/g, '""')}"`,
      c.metric,
      c.operator,
      c.measured,
      c.target,
      c.unit ?? '',
      c.weighting,
      c.met ? 'yes' : 'no',
    ].join(','),
  )
  return [header, ...rows].join('\n')
}
