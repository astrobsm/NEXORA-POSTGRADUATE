import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Download, Gauge } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { download, formatNumber, humanise, ragFor, toCsv } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Progress,
  RagBadge,
  Select,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { DomainBars, RagDistribution, StatTile } from '@/components/charts'

interface Enrolment {
  id: string
  trainee_name: string | null
  programme_name: string | null
  org_unit_name: string | null
  current_year: number
  current_level: string
  status: string
  latest_overall_score: number | null
  latest_rag: string | null
  promotion_ready: boolean
}

export default function Analytics() {
  const { can } = useAuth()
  const [year, setYear] = useState('')
  const [rag, setRag] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['enrolments', year, rag],
    queryFn: () =>
      api.get<{ items: Enrolment[]; total: number }>('/training/enrolments', {
        page_size: 300,
        training_year: year || undefined,
        rag: rag || undefined,
      }),
  })

  const { data: scorecard } = useQuery({
    queryKey: ['scorecard', selected],
    queryFn: () => api.get<any>(`/analytics/enrolments/${selected}/score`, { include_rules: true }),
    enabled: Boolean(selected),
  })

  const rows = data?.items ?? []
  const scored = rows.filter((r) => r.latest_overall_score !== null)
  const mean = scored.length
    ? scored.reduce((sum, r) => sum + (r.latest_overall_score ?? 0), 0) / scored.length
    : null

  const ragCounts = rows.reduce<Record<string, number>>((acc, row) => {
    const key = row.latest_rag ?? 'unknown'
    acc[key] = (acc[key] ?? 0) + 1
    return acc
  }, {})

  if (!can('analytics.department.read', 'analytics.institution.read', 'analytics.supervised.read')) {
    return (
      <Card>
        <EmptyState
          icon={<Gauge className="h-10 w-10" />}
          title="Analytics not available"
          description="You do not have permission to view cohort analytics."
        />
      </Card>
    )
  }

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Cohort analytics</h1>
          <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
            Every score traces back to specific curriculum requirements — select a trainee
            to see exactly which.
          </p>
        </div>
        {rows.length ? (
          <Button
            icon={<Download className="h-4 w-4" />}
            onClick={() =>
              download(
                `cohort-${new Date().toISOString().slice(0, 10)}.csv`,
                toCsv(rows as unknown as Record<string, unknown>[]),
              )
            }
          >
            Export
          </Button>
        ) : null}
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Trainees" value={formatNumber(rows.length)} />
        <StatTile
          label="Mean training score"
          value={formatNumber(mean, 1)}
          unit="/ 100"
          tone={ragFor(mean)}
        />
        <StatTile
          label="Promotion ready"
          value={formatNumber(rows.filter((r) => r.promotion_ready).length)}
        />
        <StatTile
          label="At risk"
          value={formatNumber(ragCounts.red ?? 0)}
          tone={(ragCounts.red ?? 0) > 0 ? 'red' : 'green'}
        />
      </div>

      <Card>
        <CardHeader title="Cohort status" />
        <CardBody>
          <RagDistribution counts={ragCounts} total={rows.length} />
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Trainees" />
        <div
          className="flex flex-wrap gap-2 border-b px-5 py-3"
          style={{ borderColor: 'var(--border-hairline)' }}
        >
          <Select
            className="w-auto"
            value={year}
            onChange={(event) => setYear(event.target.value)}
            aria-label="Filter by training year"
          >
            <option value="">All years</option>
            {[1, 2, 3, 4, 5, 6].map((value) => (
              <option key={value} value={value}>
                Year {value}
              </option>
            ))}
          </Select>
          <Select
            className="w-auto"
            value={rag}
            onChange={(event) => setRag(event.target.value)}
            aria-label="Filter by status"
          >
            <option value="">All statuses</option>
            <option value="green">On track</option>
            <option value="amber">Needs attention</option>
            <option value="red">At risk</option>
          </Select>
        </div>

        {isLoading ? (
          <LoadingRows />
        ) : error ? (
          <CardBody>
            <ErrorState error={error} retry={refetch} />
          </CardBody>
        ) : !rows.length ? (
          <EmptyState title="No trainees match these filters" />
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th>Trainee</Th>
                <Th>Programme</Th>
                <Th>Year</Th>
                <Th>Score</Th>
                <Th>Status</Th>
                <Th className="text-right">Detail</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <Td>
                    <p className="font-medium">{row.trainee_name}</p>
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {humanise(row.current_level)}
                      {row.org_unit_name ? ` · ${row.org_unit_name}` : ''}
                    </p>
                  </Td>
                  <Td className="text-xs">{row.programme_name ?? '—'}</Td>
                  <Td className="tnum text-xs">{row.current_year}</Td>
                  <Td className="w-40">
                    <div className="flex items-center gap-2">
                      <span className="tnum w-10 text-sm font-medium">
                        {formatNumber(row.latest_overall_score, 1)}
                      </span>
                      <div className="flex-1">
                        <Progress
                          value={row.latest_overall_score ?? 0}
                          tone={ragFor(row.latest_overall_score)}
                        />
                      </div>
                    </div>
                  </Td>
                  <Td>
                    <div className="flex items-center gap-1.5">
                      <RagBadge rag={row.latest_rag} showLabel={false} />
                      {row.promotion_ready ? <Badge tone="good">Promotion ready</Badge> : null}
                    </div>
                  </Td>
                  <Td className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() => setSelected(selected === row.id ? null : row.id)}
                    >
                      {selected === row.id ? 'Hide' : 'View'}
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>

      {selected && scorecard ? (
        <Card>
          <CardHeader
            title="Scorecard detail"
            description="Every requirement evaluated, with what was measured and what was required."
            action={<RagBadge rag={scorecard.overall_rag} />}
          />
          <CardBody className="space-y-5">
            <DomainBars domains={scorecard.domains} />

            <div>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide" style={{ color: 'var(--text-muted)' }}>
                Requirements
              </h3>
              <TableWrap>
                <thead>
                  <tr>
                    <Th>Requirement</Th>
                    <Th>Scope</Th>
                    <Th className="text-right">Measured</Th>
                    <Th className="text-right">Target</Th>
                    <Th>Progress</Th>
                    <Th>Result</Th>
                  </tr>
                </thead>
                <tbody>
                  {scorecard.requirement_results.map((result: any) => (
                    <tr key={result.rule_id + result.label}>
                      <Td>
                        <p className="text-sm">{result.label}</p>
                        {result.guidance ? (
                          <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                            {result.guidance}
                          </p>
                        ) : null}
                      </Td>
                      <Td className="text-xs">{humanise(result.scope)}</Td>
                      <Td className="tnum text-right text-sm">{formatNumber(result.measured, 1)}</Td>
                      <Td className="tnum text-right text-sm">{formatNumber(result.target, 1)}</Td>
                      <Td className="w-28">
                        <Progress
                          value={result.progress_percent}
                          tone={result.met ? 'green' : ragFor(result.progress_percent)}
                        />
                      </Td>
                      <Td>
                        <Badge
                          tone={
                            result.met
                              ? 'good'
                              : result.severity === 'mandatory'
                                ? 'critical'
                                : 'warning'
                          }
                        >
                          {result.met ? 'Met' : humanise(result.severity)}
                        </Badge>
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </TableWrap>
            </div>
          </CardBody>
        </Card>
      ) : null}
    </div>
  )
}
