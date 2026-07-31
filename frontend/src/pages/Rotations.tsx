import { useQuery } from '@tanstack/react-query'
import { CalendarDays } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { daysBetween, formatDate, formatNumber, humanise } from '@/lib/utils'
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Progress,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'

interface Rotation {
  id: string
  name: string
  org_unit_name: string | null
  supervisor_name: string | null
  training_year: number
  start_date: string
  end_date: string
  status: string
  is_elective: boolean
  is_remedial: boolean
  completion_percent: number
  supervisor_comment: string | null
}

const STATUS_TONE: Record<string, 'good' | 'warning' | 'critical' | 'info' | 'neutral'> = {
  completed: 'good',
  active: 'info',
  extended: 'warning',
  remedial: 'warning',
  interrupted: 'warning',
  failed: 'critical',
  cancelled: 'neutral',
  planned: 'neutral',
}

export default function Rotations() {
  const { principal } = useAuth()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['rotations', principal?.enrolment?.id],
    queryFn: () =>
      api.get<{ items: Rotation[]; total: number }>('/training/rotations', {
        enrolment_id: principal?.enrolment?.id,
        page_size: 200,
      }),
  })

  const rotations = data?.items ?? []
  const byYear = rotations.reduce<Record<number, Rotation[]>>((acc, rotation) => {
    ;(acc[rotation.training_year] ??= []).push(rotation)
    return acc
  }, {})

  const today = new Date().toISOString().slice(0, 10)

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Rotation schedule</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Generated automatically from the curriculum, then adjusted for leave, extension
          and remedial postings.
        </p>
      </header>

      {isLoading ? (
        <Card>
          <LoadingRows />
        </Card>
      ) : error ? (
        <ErrorState error={error} retry={refetch} />
      ) : !rotations.length ? (
        <Card>
          <EmptyState
            icon={<CalendarDays className="h-10 w-10" />}
            title="No rotations scheduled"
            description="Your training coordinator generates the posting schedule from the curriculum."
          />
        </Card>
      ) : (
        Object.entries(byYear)
          .sort(([a], [b]) => Number(a) - Number(b))
          .map(([year, items]) => {
            const completed = items.filter((r) => r.status === 'completed').length
            return (
              <Card key={year}>
                <CardHeader
                  title={`Year ${year}`}
                  description={`${completed} of ${items.length} postings closed as completed`}
                  action={
                    <div className="w-32">
                      <Progress value={(completed / items.length) * 100} />
                    </div>
                  }
                />
                <TableWrap>
                  <thead>
                    <tr>
                      <Th>Posting</Th>
                      <Th>Unit</Th>
                      <Th>Dates</Th>
                      <Th>Supervisor</Th>
                      <Th>Requirements</Th>
                      <Th>Status</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {items
                      .sort((a, b) => a.start_date.localeCompare(b.start_date))
                      .map((rotation) => {
                        const current =
                          rotation.start_date <= today && rotation.end_date >= today
                        return (
                          <tr
                            key={rotation.id}
                            style={current ? { background: 'var(--surface-2)' } : undefined}
                          >
                            <Td>
                              <div className="flex items-center gap-1.5">
                                <span className="font-medium">{rotation.name}</span>
                                {rotation.is_elective ? (
                                  <Badge tone="neutral">Elective</Badge>
                                ) : null}
                                {rotation.is_remedial ? (
                                  <Badge tone="warning">Remedial</Badge>
                                ) : null}
                                {current ? <Badge tone="brand">Current</Badge> : null}
                              </div>
                              {rotation.supervisor_comment ? (
                                <p
                                  className="mt-0.5 max-w-md text-xs"
                                  style={{ color: 'var(--text-muted)' }}
                                >
                                  {rotation.supervisor_comment}
                                </p>
                              ) : null}
                            </Td>
                            <Td className="text-xs">{rotation.org_unit_name ?? '—'}</Td>
                            <Td className="tnum whitespace-nowrap text-xs">
                              {formatDate(rotation.start_date)} – {formatDate(rotation.end_date)}
                              {current ? (
                                <span className="block" style={{ color: 'var(--text-muted)' }}>
                                  {daysBetween(today, rotation.end_date)} days remaining
                                </span>
                              ) : null}
                            </Td>
                            <Td className="text-xs">{rotation.supervisor_name ?? '—'}</Td>
                            <Td className="w-32">
                              <Progress value={rotation.completion_percent} />
                              <span
                                className="tnum mt-0.5 block text-[11px]"
                                style={{ color: 'var(--text-muted)' }}
                              >
                                {formatNumber(rotation.completion_percent, 0)}% met
                              </span>
                            </Td>
                            <Td>
                              <Badge tone={STATUS_TONE[rotation.status] ?? 'neutral'}>
                                {humanise(rotation.status)}
                              </Badge>
                            </Td>
                          </tr>
                        )
                      })}
                  </tbody>
                </TableWrap>
              </Card>
            )
          })
      )}
    </div>
  )
}
