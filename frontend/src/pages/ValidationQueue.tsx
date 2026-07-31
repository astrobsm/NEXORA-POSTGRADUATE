/**
 * The consultant's validation queue.
 *
 * Built around the reality that a consultant clears this between clinics: bulk
 * sign-off for the routine majority, and a per-entry path for anything needing a
 * query. A query always demands a comment, because "returned" without a reason
 * wastes the trainee's next attempt.
 */

import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckCheck, CheckCircle2, MessageSquareWarning, ShieldCheck, XCircle } from 'lucide-react'

import { api } from '@/lib/api'
import { daysBetween, formatDate, formatNumber, humanise } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Field,
  LoadingRows,
  Td,
  Textarea,
  Th,
  TableWrap,
} from '@/components/ui'
import { StatTile } from '@/components/charts'

interface PendingEntry {
  id: string
  title: string
  entry_type: string
  occurred_on: string
  diagnosis: string | null
  procedure_name: string | null
  participation_role: string | null
  complexity: string
  outcome: string
  reflection: string | null
  enrolment_id: string
}

export default function ValidationQueue() {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [queryTarget, setQueryTarget] = useState<PendingEntry | null>(null)
  const [comment, setComment] = useState('')

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['validation-queue'],
    queryFn: () =>
      api.get<{ items: PendingEntry[]; total: number }>('/logbook/pending-validation', {
        page_size: 200,
      }),
  })

  const invalidate = () => {
    void queryClient.invalidateQueries({ queryKey: ['validation-queue'] })
    void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    void queryClient.invalidateQueries({ queryKey: ['logbook'] })
  }

  const decide = useMutation({
    mutationFn: ({ id, decision, note }: { id: string; decision: string; note?: string }) =>
      api.post(`/logbook/${id}/validation`, { decision, comment: note }),
    onSuccess: () => {
      setQueryTarget(null)
      setComment('')
      invalidate()
    },
  })

  const bulkValidate = useMutation({
    mutationFn: (ids: string[]) => api.post('/logbook/validation/bulk', ids),
    onSuccess: () => {
      setSelected(new Set())
      invalidate()
    },
  })

  const items = data?.items ?? []
  const oldest = items.length ? daysBetween(items[0].occurred_on) : 0

  function toggle(id: string) {
    setSelected((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Validation queue</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Nothing a trainee records counts toward their requirements until you sign it off.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Awaiting your sign-off"
          value={formatNumber(items.length)}
          tone={items.length === 0 ? 'green' : items.length > 30 ? 'red' : 'amber'}
        />
        <StatTile
          label="Oldest entry"
          value={oldest ? `${oldest}` : '—'}
          unit={oldest ? 'days' : undefined}
          tone={oldest > 14 ? 'red' : oldest > 7 ? 'amber' : 'green'}
          hint="Institutional service level is 7 days"
        />
        <StatTile label="Selected" value={formatNumber(selected.size)} />
      </div>

      {queryTarget ? (
        <Card>
          <CardHeader
            title={`Return "${queryTarget.title}" for correction`}
            description="The trainee sees this comment and can amend and resubmit."
          />
          <CardBody className="space-y-3">
            <Field label="What needs correcting?" required>
              <Textarea
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                placeholder="e.g. Please confirm which side, and record your actual role in the procedure."
                autoFocus
              />
            </Field>
            {decide.isError ? <ErrorState error={decide.error} /> : null}
            <div className="flex justify-end gap-2">
              <Button
                variant="ghost"
                onClick={() => {
                  setQueryTarget(null)
                  setComment('')
                }}
              >
                Cancel
              </Button>
              <Button
                variant="danger"
                loading={decide.isPending}
                disabled={!comment.trim()}
                onClick={() =>
                  decide.mutate({ id: queryTarget.id, decision: 'queried', note: comment.trim() })
                }
              >
                Return to trainee
              </Button>
            </div>
          </CardBody>
        </Card>
      ) : null}

      <Card>
        <CardHeader
          title="Pending entries"
          action={
            selected.size > 0 ? (
              <Button
                variant="success"
                size="sm"
                icon={<CheckCheck className="h-4 w-4" />}
                loading={bulkValidate.isPending}
                onClick={() => bulkValidate.mutate([...selected])}
              >
                Validate {selected.size} selected
              </Button>
            ) : undefined
          }
        />

        {isLoading ? (
          <LoadingRows />
        ) : error ? (
          <CardBody>
            <ErrorState error={error} retry={refetch} />
          </CardBody>
        ) : !items.length ? (
          <EmptyState
            icon={<ShieldCheck className="h-10 w-10" />}
            title="Queue is clear"
            description="No logbook entries are waiting on you. Your trainees can see that their records are up to date."
          />
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th className="w-10">
                  <input
                    type="checkbox"
                    aria-label="Select all pending entries"
                    checked={selected.size === items.length && items.length > 0}
                    onChange={(event) =>
                      setSelected(event.target.checked ? new Set(items.map((i) => i.id)) : new Set())
                    }
                  />
                </Th>
                <Th>Date</Th>
                <Th>Activity</Th>
                <Th>Role</Th>
                <Th>Outcome</Th>
                <Th className="text-right">Decision</Th>
              </tr>
            </thead>
            <tbody>
              {items.map((entry) => {
                const waiting = daysBetween(entry.occurred_on)
                return (
                  <tr key={entry.id}>
                    <Td>
                      <input
                        type="checkbox"
                        aria-label={`Select ${entry.title}`}
                        checked={selected.has(entry.id)}
                        onChange={() => toggle(entry.id)}
                      />
                    </Td>
                    <Td className="whitespace-nowrap">
                      <span className="tnum text-xs">{formatDate(entry.occurred_on)}</span>
                      {waiting > 7 ? (
                        <Badge tone="warning" className="ml-1.5">
                          {waiting}d
                        </Badge>
                      ) : null}
                    </Td>
                    <Td>
                      <p className="font-medium">{entry.title}</p>
                      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                        {humanise(entry.entry_type)}
                        {entry.diagnosis ? ` · ${entry.diagnosis}` : ''}
                      </p>
                      {entry.reflection ? (
                        <p
                          className="mt-1 max-w-md text-xs italic"
                          style={{ color: 'var(--text-secondary)' }}
                        >
                          &ldquo;{entry.reflection}&rdquo;
                        </p>
                      ) : null}
                    </Td>
                    <Td className="text-xs">
                      {entry.participation_role ? humanise(entry.participation_role) : '—'}
                    </Td>
                    <Td>
                      <Badge
                        tone={
                          entry.outcome === 'uneventful'
                            ? 'good'
                            : entry.outcome === 'mortality'
                              ? 'critical'
                              : 'warning'
                        }
                      >
                        {humanise(entry.outcome)}
                      </Badge>
                    </Td>
                    <Td>
                      <div className="flex justify-end gap-1.5">
                        <Button
                          size="sm"
                          variant="success"
                          icon={<CheckCircle2 className="h-3.5 w-3.5" />}
                          loading={decide.isPending && decide.variables?.id === entry.id}
                          onClick={() => decide.mutate({ id: entry.id, decision: 'validated' })}
                        >
                          Validate
                        </Button>
                        <Button
                          size="sm"
                          icon={<MessageSquareWarning className="h-3.5 w-3.5" />}
                          onClick={() => {
                            setQueryTarget(entry)
                            setComment('')
                          }}
                        >
                          Query
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          aria-label="Reject entry"
                          icon={<XCircle className="h-3.5 w-3.5" />}
                          onClick={() => {
                            setQueryTarget(entry)
                            setComment('')
                          }}
                        />
                      </div>
                    </Td>
                  </tr>
                )
              })}
            </tbody>
          </TableWrap>
        )}
      </Card>
    </div>
  )
}
