import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Activity, CalendarCheck, QrCode } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatDateTime, formatNumber, formatPercent, humanise } from '@/lib/utils'
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
  Tabs,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { CategoryBars, StatTile } from '@/components/charts'

interface ActivityRow {
  id: string
  kind: string
  title: string
  scheduled_at: string
  scheduled_on: string
  venue: string | null
  presenter_name: string | null
  is_mandatory: boolean
  cme_credits: number
  attendee_count: number
}

function CheckInCard() {
  const queryClient = useQueryClient()
  const [code, setCode] = useState('')
  const [activityId, setActivityId] = useState('')

  const { data: upcoming } = useQuery({
    queryKey: ['activities', 'today'],
    queryFn: () =>
      api.get<{ items: ActivityRow[] }>('/academic/activities', {
        date_from: new Date().toISOString().slice(0, 10),
        page_size: 20,
      }),
  })

  const checkIn = useMutation({
    mutationFn: () =>
      api.post(`/academic/activities/${activityId}/attendance`, {
        checkin_code: code.trim().toUpperCase(),
        role: 'attendee',
      }),
    onSuccess: () => {
      setCode('')
      void queryClient.invalidateQueries({ queryKey: ['attendance-summary'] })
      void queryClient.invalidateQueries({ queryKey: ['cme-ledger'] })
    },
  })

  const options = upcoming?.items ?? []

  return (
    <Card>
      <CardHeader
        title="Check in to a session"
        description="Enter the code shown at the start of the session. Credits post immediately."
      />
      <CardBody className="space-y-3">
        <Field label="Session">
          <select
            className="w-full rounded-[var(--radius-control)] border px-3 py-2 text-sm"
            style={{ background: 'var(--surface-1)', borderColor: 'var(--border-hairline)' }}
            value={activityId}
            onChange={(event) => setActivityId(event.target.value)}
          >
            <option value="">Choose a session…</option>
            {options.map((activity) => (
              <option key={activity.id} value={activity.id}>
                {activity.title} — {formatDateTime(activity.scheduled_at)}
              </option>
            ))}
          </select>
        </Field>
        <Field label="Check-in code">
          <Input
            value={code}
            onChange={(event) => setCode(event.target.value.toUpperCase())}
            placeholder="ABC123"
            maxLength={8}
            className="tnum tracking-[0.2em]"
          />
        </Field>
        {checkIn.isError ? <ErrorState error={checkIn.error} /> : null}
        {checkIn.isSuccess ? (
          <p className="text-xs" style={{ color: 'var(--text-success)' }}>
            Attendance recorded and credits posted.
          </p>
        ) : null}
        <Button
          variant="primary"
          className="w-full justify-center"
          icon={<QrCode className="h-4 w-4" />}
          disabled={!code.trim() || !activityId}
          loading={checkIn.isPending}
          onClick={() => checkIn.mutate()}
        >
          Check in
        </Button>
      </CardBody>
    </Card>
  )
}

export default function Academic() {
  const { principal, can } = useAuth()
  const [tab, setTab] = useState('calendar')

  const { data: activities, isLoading, error, refetch } = useQuery({
    queryKey: ['activities'],
    queryFn: () =>
      api.get<{ items: ActivityRow[]; total: number }>('/academic/activities', {
        page_size: 100,
      }),
  })

  const { data: attendance } = useQuery({
    queryKey: ['attendance-summary'],
    queryFn: () => api.get<any>('/academic/attendance/summary'),
  })

  const { data: ledger } = useQuery({
    queryKey: ['cme-ledger'],
    queryFn: () => api.get<any>('/academic/cme/ledger'),
  })

  const byKind = useMemo(
    () =>
      Object.entries(attendance?.by_kind ?? {}).map(([kind, counts]: [string, any]) => ({
        name: humanise(kind),
        value: counts.percent,
      })),
    [attendance],
  )

  const items = activities?.items ?? []

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Academic activities &amp; CME</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Attendance is measured against the mandatory sessions your department held while
          you were in post.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Attendance"
          value={formatPercent(attendance?.percent ?? 0, 0)}
          tone={
            (attendance?.percent ?? 0) >= 75
              ? 'green'
              : (attendance?.percent ?? 0) >= 60
                ? 'amber'
                : 'red'
          }
          hint={`${formatNumber(attendance?.attended ?? 0)} of ${formatNumber(attendance?.expected ?? 0)} mandatory sessions`}
          icon={<CalendarCheck className="h-4 w-4" />}
        />
        <StatTile
          label="CME credits"
          value={formatNumber(ledger?.total_credits ?? 0, 1)}
          hint="Across all recognised sources"
        />
        <StatTile
          label="Sessions this year"
          value={formatNumber(
            ledger?.by_year?.[new Date().getFullYear()] !== undefined
              ? ledger.by_year[new Date().getFullYear()]
              : 0,
            1,
          )}
          unit="credits"
        />
        <StatTile label="Scheduled activities" value={formatNumber(activities?.total ?? 0)} />
      </div>

      <Tabs
        tabs={[
          { id: 'calendar', label: 'Calendar', count: items.length },
          { id: 'attendance', label: 'My attendance' },
          { id: 'cme', label: 'CME ledger' },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'calendar' ? (
        <div className="grid gap-5 lg:grid-cols-[2fr_1fr]">
          <Card>
            <CardHeader title="Academic calendar" />
            {isLoading ? (
              <LoadingRows />
            ) : error ? (
              <CardBody>
                <ErrorState error={error} retry={refetch} />
              </CardBody>
            ) : !items.length ? (
              <EmptyState
                icon={<Activity className="h-10 w-10" />}
                title="No activities scheduled"
              />
            ) : (
              <TableWrap>
                <thead>
                  <tr>
                    <Th>When</Th>
                    <Th>Session</Th>
                    <Th>Presenter</Th>
                    <Th>Credits</Th>
                    <Th>Attended</Th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((activity) => (
                    <tr key={activity.id}>
                      <Td className="tnum whitespace-nowrap text-xs">
                        {formatDateTime(activity.scheduled_at)}
                      </Td>
                      <Td>
                        <p className="font-medium">{activity.title}</p>
                        <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                          {humanise(activity.kind)}
                          {activity.venue ? ` · ${activity.venue}` : ''}
                          {activity.is_mandatory ? '' : ' · optional'}
                        </p>
                      </Td>
                      <Td className="text-xs">{activity.presenter_name ?? '—'}</Td>
                      <Td className="tnum text-xs">{activity.cme_credits}</Td>
                      <Td className="tnum text-xs">{activity.attendee_count}</Td>
                    </tr>
                  ))}
                </tbody>
              </TableWrap>
            )}
          </Card>

          {principal?.enrolment || can('academic.attendance.record') ? <CheckInCard /> : null}
        </div>
      ) : null}

      {tab === 'attendance' ? (
        <div className="grid gap-5 lg:grid-cols-2">
          <Card>
            <CardHeader
              title="Attendance by session type"
              description="Percentage of the mandatory sessions of each type that you attended."
            />
            <CardBody>
              <CategoryBars data={byKind} unit="%" />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="Detail" />
            <CardBody className="space-y-3">
              {Object.entries(attendance?.by_kind ?? {}).map(([kind, counts]: [string, any]) => (
                <div key={kind}>
                  <div className="mb-1 flex items-baseline justify-between text-xs">
                    <span className="font-medium">{humanise(kind)}</span>
                    <span className="tnum" style={{ color: 'var(--text-muted)' }}>
                      {counts.attended} / {counts.expected}
                    </span>
                  </div>
                  <Progress
                    value={counts.percent}
                    tone={counts.percent >= 75 ? 'green' : counts.percent >= 60 ? 'amber' : 'red'}
                  />
                </div>
              ))}
            </CardBody>
          </Card>
        </div>
      ) : null}

      {tab === 'cme' ? (
        <Card>
          <CardHeader
            title="CME credit ledger"
            description="Append-only. Every credit traces back to a recorded activity."
          />
          {!ledger?.entries?.length ? (
            <EmptyState title="No credits recorded yet" />
          ) : (
            <TableWrap>
              <thead>
                <tr>
                  <Th>Date</Th>
                  <Th>Description</Th>
                  <Th>Source</Th>
                  <Th>Recognised by</Th>
                  <Th className="text-right">Credits</Th>
                </tr>
              </thead>
              <tbody>
                {ledger.entries.map((entry: any) => (
                  <tr key={entry.id}>
                    <Td className="tnum whitespace-nowrap text-xs">{entry.awarded_on}</Td>
                    <Td className="text-sm">{entry.description}</Td>
                    <Td className="text-xs">{humanise(entry.source_kind)}</Td>
                    <Td>
                      <Badge tone="neutral">{entry.recognised_by.toUpperCase()}</Badge>
                    </Td>
                    <Td className="tnum text-right font-medium">{entry.credits}</Td>
                  </tr>
                ))}
              </tbody>
            </TableWrap>
          )}
        </Card>
      ) : null}
    </div>
  )
}
