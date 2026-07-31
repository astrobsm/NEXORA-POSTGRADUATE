/**
 * The question review queue — where the AI publication gate is operated.
 *
 * The screen is built around one fact: nothing here has been seen by a
 * candidate, and nothing will be until someone on this page approves it. So the
 * provenance label is the loudest element on every row, the correct answer and
 * every distractor rationale are shown in full, and "Publish" is deliberately a
 * separate step from "Approve" rather than a single button.
 *
 * Items are ordered by the generator's own confidence, lowest first. A
 * consultant with an hour should spend it where the machine was least sure, not
 * on whichever item sorts first.
 */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, Bot, CheckCircle2, PenLine, User } from 'lucide-react'

import { api } from '@/lib/api'
import { formatNumber, humanise } from '@/lib/utils'
import {
  Badge,
  Button,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Textarea,
} from '@/components/ui'
import { StatTile } from '@/components/charts'

interface QueueEntry {
  question_id: string
  stem: string
  topic: string | null
  blueprint_category: string | null
  difficulty_band: string | null
  editorial_status: string
  authoring_source: string
  is_ai_generated: boolean
  ai_confidence: number | null
  generation_job_id: string | null
  version: number
  advisory_notes: string[]
  created_at: string
}

interface QueueSummary {
  by_status: Record<string, number>
  by_authoring_source: Record<string, number>
  awaiting_review: number
  unreviewed_ai_generated: number
  published: number
}

interface QuestionDetail {
  id: string
  stem: string
  lead_in: string | null
  options: { key: string; text: string; is_correct: boolean; rationale?: string }[]
  correct_keys: string[]
  explanation: string | null
  references: string[]
  topic: string | null
  blueprint_category: string | null
  difficulty_band: string | null
  bloom_level: string | null
  editorial_status: string
  authoring_source: string
  version: number
  ai_confidence: number | null
  is_servable: boolean
  provenance: {
    is_ai_generated: boolean
    is_reviewed: boolean
    label: string | null
    must_display_label: boolean
  }
}

/** Which decisions are offered from each state. Mirrors the server's table. */
const DECISIONS: Record<string, { value: string; label: string; primary?: boolean }[]> = {
  ai_draft: [{ value: 'submit', label: 'Take for review', primary: true }],
  draft: [{ value: 'submit', label: 'Take for review', primary: true }],
  in_review: [
    { value: 'approve', label: 'Approve', primary: true },
    { value: 'request_changes', label: 'Request changes' },
    { value: 'reject', label: 'Reject' },
  ],
  changes_requested: [{ value: 'submit', label: 'Re-submit for review', primary: true }],
  approved: [
    { value: 'publish', label: 'Publish', primary: true },
    { value: 'request_changes', label: 'Send back' },
  ],
  published: [{ value: 'retire', label: 'Retire' }],
}

/** Decisions the server refuses without a written reason. */
const REASON_REQUIRED = new Set(['request_changes', 'reject', 'retire'])

export default function QuestionReview() {
  const [selected, setSelected] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['review-queue'],
    queryFn: () =>
      api.get<{ summary: QueueSummary; items: QueueEntry[] }>('/cbt/review-queue', {
        limit: 200,
      }),
  })

  if (isLoading) return <LoadingRows rows={8} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return <EmptyState title="Nothing to review" />

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold text-[var(--text-primary)]">
          Question review
        </h1>
        <p className="mt-1 text-sm text-[var(--text-secondary)]">
          No item on this page has been served to a candidate. Nothing will be
          until it is approved and published here.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile
          label="Awaiting review"
          value={data.summary.awaiting_review}
          hint="Across all authors"
        />
        <StatTile
          label="Unreviewed AI-generated"
          value={data.summary.unreviewed_ai_generated}
          hint="Cannot be served until approved"
        />
        <StatTile label="Published" value={data.summary.published} />
      </div>

      {data.items.length === 0 ? (
        <EmptyState
          title="The queue is empty"
          description="Every item in the bank has been through review."
        />
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_1fr]">
          <Card className="max-h-[70vh] overflow-y-auto">
            <CardHeader
              title={`${data.items.length} awaiting`}
              description="Least-confident items first."
            />
            <CardBody className="space-y-2">
              {data.items.map((item) => (
                <button
                  key={item.question_id}
                  type="button"
                  onClick={() => setSelected(item.question_id)}
                  className={`w-full rounded-lg border p-3 text-left text-sm ${
                    selected === item.question_id
                      ? 'border-[var(--brand)] bg-[var(--brand-wash)]'
                      : 'border-[var(--border-hairline)]'
                  }`}
                >
                  <div className="flex flex-wrap items-center gap-2">
                    <ProvenanceBadge
                      isAi={item.is_ai_generated}
                      reviewed={item.editorial_status === 'published'}
                    />
                    <Badge tone="neutral">{humanise(item.editorial_status)}</Badge>
                    {item.ai_confidence !== null && (
                      <span
                        className={`text-xs ${
                          item.ai_confidence < 0.6
                            ? 'text-[var(--status-warning)]'
                            : 'text-[var(--text-muted)]'
                        }`}
                      >
                        confidence {formatNumber(item.ai_confidence, 2)}
                      </span>
                    )}
                  </div>
                  <p className="mt-2 line-clamp-3 text-[var(--text-secondary)]">
                    {item.stem}
                  </p>
                  {item.advisory_notes.length > 0 && (
                    <p className="mt-1 text-xs text-[var(--text-muted)]">
                      {item.advisory_notes.length} advisory note
                      {item.advisory_notes.length === 1 ? '' : 's'}
                    </p>
                  )}
                </button>
              ))}
            </CardBody>
          </Card>

          {selected ? (
            <ReviewPanel questionId={selected} onDone={() => void refetch()} />
          ) : (
            <EmptyState
              title="Select an item"
              description="Its full text, key and every rationale will be shown here."
            />
          )}
        </div>
      )}
    </div>
  )
}

function ProvenanceBadge({ isAi, reviewed }: { isAi: boolean; reviewed: boolean }) {
  if (!isAi) {
    return (
      <Badge tone="neutral">
        <User className="mr-1 h-3 w-3" aria-hidden />
        Human-written
      </Badge>
    )
  }
  // Icon and wording, never colour alone — this label has to survive being
  // printed in greyscale in a departmental paper pack.
  return (
    <Badge tone={reviewed ? 'good' : 'warning'}>
      <Bot className="mr-1 h-3 w-3" aria-hidden />
      {reviewed ? 'AI-generated, reviewed' : 'AI-generated, NOT REVIEWED'}
    </Badge>
  )
}

function ReviewPanel({
  questionId,
  onDone,
}: {
  questionId: string
  onDone: () => void
}) {
  const client = useQueryClient()
  const [comments, setComments] = useState('')
  const [failure, setFailure] = useState<string | null>(null)

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['question', questionId],
    queryFn: () => api.get<QuestionDetail>(`/cbt/questions/${questionId}`),
  })

  const decide = useMutation({
    mutationFn: (decision: string) =>
      api.post(`/cbt/questions/${questionId}/review`, { decision, comments }),
    onSuccess: () => {
      setComments('')
      setFailure(null)
      void client.invalidateQueries({ queryKey: ['question', questionId] })
      onDone()
    },
    onError: (err) =>
      setFailure(err instanceof Error ? err.message : 'That decision was refused.'),
  })

  if (isLoading) return <LoadingRows rows={6} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  const available = DECISIONS[data.editorial_status] ?? []

  return (
    <Card>
      <CardHeader
        title={`Version ${data.version}`}
        description={`${humanise(data.editorial_status)} · ${humanise(
          data.topic ?? 'unclassified',
        )}`}
      />
      <CardBody className="space-y-4 text-sm">
        {data.provenance.must_display_label && (
          <p
            className={`flex items-start gap-2 rounded-lg p-3 ${
              data.provenance.is_reviewed
                ? 'bg-[var(--status-good-wash)]'
                : 'bg-[var(--status-warning-wash)]'
            }`}
          >
            <Bot className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            <span className="text-[var(--text-primary)]">
              {data.provenance.label}
              {!data.provenance.is_reviewed && (
                <>
                  {' '}
                  Check the clinical accuracy of every option and every reference
                  before approving — the generator does not verify that a cited
                  paper exists.
                </>
              )}
            </span>
          </p>
        )}

        <p className="whitespace-pre-line text-[var(--text-primary)]">{data.stem}</p>
        {data.lead_in && <p className="font-medium">{data.lead_in}</p>}

        <ul className="space-y-2">
          {data.options.map((option) => (
            <li
              key={option.key}
              className={`rounded-lg border p-3 ${
                option.is_correct
                  ? 'border-[var(--status-good)] bg-[var(--status-good-wash)]'
                  : 'border-[var(--border-hairline)]'
              }`}
            >
              <div className="flex items-baseline gap-2">
                <strong>{option.key}.</strong>
                <span>{option.text}</span>
                {option.is_correct && (
                  <Badge tone="good">
                    <CheckCircle2 className="mr-1 h-3 w-3" aria-hidden />
                    Key
                  </Badge>
                )}
              </div>
              <p className="mt-1 text-[var(--text-secondary)]">
                {option.rationale || (
                  <em className="text-[var(--status-warning)]">
                    No rationale recorded — a candidate reviewing this will learn
                    nothing from it.
                  </em>
                )}
              </p>
            </li>
          ))}
        </ul>

        {data.explanation && (
          <div>
            <h3 className="font-medium text-[var(--text-primary)]">Explanation</h3>
            <p className="mt-1 whitespace-pre-line text-[var(--text-secondary)]">
              {data.explanation}
            </p>
          </div>
        )}

        <div>
          <h3 className="font-medium text-[var(--text-primary)]">References</h3>
          {data.references.length ? (
            <ul className="mt-1 list-disc pl-5 text-[var(--text-secondary)]">
              {data.references.map((reference) => (
                <li key={reference}>{reference}</li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-[var(--status-warning)]">
              No reference — this item cannot be verified against anything.
            </p>
          )}
        </div>

        <div className="flex flex-wrap gap-2 text-xs text-[var(--text-muted)]">
          {data.blueprint_category && (
            <Badge tone="neutral">{humanise(data.blueprint_category)}</Badge>
          )}
          {data.difficulty_band && (
            <Badge tone="neutral">{humanise(data.difficulty_band)}</Badge>
          )}
          {data.bloom_level && <Badge tone="neutral">{humanise(data.bloom_level)}</Badge>}
          <Badge tone={data.is_servable ? 'good' : 'neutral'}>
            {data.is_servable ? 'Servable to candidates' : 'Not servable'}
          </Badge>
        </div>

        <div className="space-y-2 border-t border-[var(--border-hairline)] pt-4">
          <label className="block">
            <span className="text-[var(--text-secondary)]">
              Reviewer comments
              <span className="ml-1 text-[var(--text-muted)]">
                (required when requesting changes, rejecting or retiring)
              </span>
            </span>
            <Textarea
              rows={3}
              value={comments}
              onChange={(e) => setComments(e.target.value)}
              placeholder="What needs to change, and why."
            />
          </label>

          {failure && (
            <p className="flex items-start gap-2 rounded-lg bg-[var(--status-critical-wash)] p-3 text-[var(--status-critical)]">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
              {failure}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            {available.map((decision) => (
              <Button
                key={decision.value}
                variant={decision.primary ? 'primary' : 'secondary'}
                disabled={
                  decide.isPending ||
                  (REASON_REQUIRED.has(decision.value) && !comments.trim())
                }
                onClick={() => decide.mutate(decision.value)}
              >
                {decision.value === 'publish' && (
                  <PenLine className="mr-1 h-4 w-4" aria-hidden />
                )}
                {decision.label}
              </Button>
            ))}
            {available.length === 0 && (
              <p className="text-[var(--text-muted)]">
                No decision is available from “{humanise(data.editorial_status)}”.
              </p>
            )}
          </div>

          {data.editorial_status === 'approved' && (
            <p className="text-xs text-[var(--text-muted)]">
              Publishing is a separate step from approving on purpose. Approval
              records your clinical judgement; publication is what puts the item
              in front of candidates.
            </p>
          )}
        </div>
      </CardBody>
    </Card>
  )
}
