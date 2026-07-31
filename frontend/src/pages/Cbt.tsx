/**
 * Sitting a computer-based test.
 *
 * The client enforces whatever the server's conduct directives ask for, and
 * reports what it observed. It does not decide anything: the timer shown here
 * is a display of the server's clock, re-synchronised on every answer, and a
 * candidate whose browser clock is wrong is not disadvantaged.
 *
 * Every restriction is applied only when the institution's policy asks for it.
 * A formative practice quiz with no policy runs with nothing blocked and
 * nothing logged, which is the correct default and requires no configuration.
 *
 * The candidate is told plainly, before starting, exactly what will be recorded.
 * Silent monitoring would be a poor foundation for a decision anyone has to
 * defend later.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, CheckCircle2, Clock, Flag, ShieldCheck } from 'lucide-react'

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
  Progress,
} from '@/components/ui'

interface Directives {
  require_fullscreen: boolean
  block_copy_paste: boolean
  block_printing: boolean
  block_context_menu: boolean
  log_focus_changes: boolean
  log_clipboard_attempts: boolean
  idle_timeout_seconds: number
  auto_submit_on_expiry: boolean
  shuffle_questions: boolean
  shuffle_options: boolean
  proctoring_mode: string
  consent_statement: string | null
  consent_required: boolean
}

interface StartResponse {
  attempt_id: string
  session_token: string
  attempt_number: number
  question_count: number
  total_marks: number
  remaining_seconds: number
  directives: Directives
  policy_id: string | null
}

interface ServedQuestion {
  question_id: string
  sequence: number
  question_type: string
  stem: string
  lead_in: string | null
  options: { key: string; text: string }[]
  marks: number
  suggested_seconds: number
  selected_keys: string[]
  flagged_for_review: boolean
  seconds_spent: number
}

interface Feedback {
  question_id: string
  sequence: number
  stem: string
  lead_in: string | null
  selected_keys: string[]
  correct_keys: string[]
  is_correct: boolean | null
  marks_awarded: number
  marks_available: number
  options: {
    key: string
    text: string
    is_correct: boolean
    was_selected: boolean
    rationale: string
  }[]
  explanation: string | null
  references: string[]
  topic: string | null
  difficulty_band: string | null
  cohort_facility: number | null
  cme_resource_id: string | null
  authoring_source: string
}

/** One observation the client buffers until it can be posted. */
interface PendingEvent {
  kind: string
  occurred_at: string
  duration_seconds?: number
}

export default function Cbt() {
  const [paperId, setPaperId] = useState('')
  const [session, setSession] = useState<StartResponse | null>(null)
  const [submitted, setSubmitted] = useState<Record<string, unknown> | null>(null)

  if (submitted && session) {
    return (
      <Results
        attemptId={session.attempt_id}
        summary={submitted}
        onDone={() => {
          setSession(null)
          setSubmitted(null)
        }}
      />
    )
  }

  if (session) {
    return (
      <Sitting
        session={session}
        onSubmitted={setSubmitted}
        onAbandon={() => setSession(null)}
      />
    )
  }

  return <PaperPicker paperId={paperId} setPaperId={setPaperId} onStarted={setSession} />
}

// ==========================================================================
// Choosing and starting
// ==========================================================================
function PaperPicker({
  paperId,
  setPaperId,
  onStarted,
}: {
  paperId: string
  setPaperId: (v: string) => void
  onStarted: (s: StartResponse) => void
}) {
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<StartResponse | null>(null)

  const start = async () => {
    setStarting(true)
    setError(null)
    try {
      const response = await api.post<StartResponse>(
        `/cbt/papers/${paperId}/attempts`,
        {
          // A stable but non-identifying browser signature. The server salts
          // and hashes it; the raw value is never stored.
          device_fingerprint: deviceSignature(),
        },
      )
      // Show the candidate what will be recorded before anything is.
      setPending(response)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not start this paper.')
    } finally {
      setStarting(false)
    }
  }

  if (pending) {
    return <ConductNotice session={pending} onAccept={() => onStarted(pending)} />
  }

  return (
    <Card>
      <CardHeader
        title="Sit a paper"
        description="Enter the paper reference your department issued."
      />
      <CardBody className="space-y-4">
        <label className="block text-sm">
          <span className="text-[var(--text-secondary)]">Paper reference</span>
          <input
            className="mt-1 w-full rounded-lg border border-[var(--border-hairline)] bg-[var(--surface-1)] px-3 py-2"
            value={paperId}
            onChange={(e) => setPaperId(e.target.value.trim())}
            placeholder="e.g. 7f3c9a…"
          />
        </label>
        {error && (
          <p className="flex items-start gap-2 rounded-lg bg-[var(--status-critical-wash)] p-3 text-sm text-[var(--status-critical)]">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
            {error}
          </p>
        )}
        <Button onClick={start} disabled={!paperId || starting}>
          {starting ? 'Starting…' : 'Continue'}
        </Button>
      </CardBody>
    </Card>
  )
}

/**
 * What the candidate is told before the paper opens.
 *
 * Everything the client will record is listed, in plain words, with the
 * measures that are switched off omitted rather than shown as unticked — a
 * list of things that are not happening is noise.
 */
function ConductNotice({
  session,
  onAccept,
}: {
  session: StartResponse
  onAccept: () => void
}) {
  const d = session.directives
  const [consented, setConsented] = useState(false)

  const recorded: string[] = []
  if (d.log_focus_changes)
    recorded.push('When the examination window loses focus, and for how long')
  if (d.log_clipboard_attempts)
    recorded.push('Attempts to copy, cut, paste or print')
  if (d.require_fullscreen) recorded.push('Leaving full-screen mode')
  recorded.push('A one-way hash of your device and network — never the values themselves')

  const wantsMedia = d.consent_required && d.consent_statement

  return (
    <Card>
      <CardHeader
        title="Before you begin"
        description={`${session.question_count} questions · ${Math.round(
          session.remaining_seconds / 60,
        )} minutes · attempt ${session.attempt_number}`}
      />
      <CardBody className="space-y-4 text-sm">
        <div>
          <h2 className="flex items-center gap-2 font-medium text-[var(--text-primary)]">
            <ShieldCheck className="h-4 w-4" aria-hidden />
            What this examination records
          </h2>
          <ul className="mt-2 list-disc space-y-1 pl-5 text-[var(--text-secondary)]">
            {recorded.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
          <p className="mt-2 text-[var(--text-muted)]">
            These observations are advisory. They are summarised for your
            department after the examination and are never, on their own, the
            basis for any finding against you.
          </p>
        </div>

        {wantsMedia && (
          <div className="rounded-lg border border-[var(--border-hairline)] p-3">
            <h2 className="font-medium text-[var(--text-primary)]">
              Optional camera monitoring
            </h2>
            <p className="mt-1 whitespace-pre-line text-[var(--text-secondary)]">
              {d.consent_statement}
            </p>
            <label className="mt-3 flex items-start gap-2">
              <input
                type="checkbox"
                className="mt-1"
                checked={consented}
                onChange={async (e) => {
                  setConsented(e.target.checked)
                  await api.post(`/cbt/attempts/${session.attempt_id}/consent`, {
                    camera: e.target.checked,
                    microphone: false,
                  })
                }}
              />
              <span className="text-[var(--text-secondary)]">
                I consent to camera monitoring for this sitting. You may withdraw
                at any time, and the paper continues either way.
              </span>
            </label>
          </div>
        )}

        <Button onClick={onAccept}>Begin the paper</Button>
      </CardBody>
    </Card>
  )
}

// ==========================================================================
// The sitting
// ==========================================================================
function Sitting({
  session,
  onSubmitted,
  onAbandon,
}: {
  session: StartResponse
  onSubmitted: (summary: Record<string, unknown>) => void
  onAbandon: () => void
}) {
  const d = session.directives
  const [index, setIndex] = useState(0)
  const [answers, setAnswers] = useState<Record<string, string[]>>({})
  const [flags, setFlags] = useState<Record<string, boolean>>({})
  const [remaining, setRemaining] = useState(session.remaining_seconds)
  const [busy, setBusy] = useState(false)
  const buffer = useRef<PendingEvent[]>([])
  const questionShownAt = useRef(Date.now())

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['cbt-questions', session.attempt_id],
    queryFn: () =>
      api.get<{ questions: ServedQuestion[]; remaining_seconds: number }>(
        `/cbt/attempts/${session.attempt_id}/questions`,
        { session_token: session.session_token },
      ),
  })

  const questions = useMemo(() => data?.questions ?? [], [data])
  const current = questions[index]

  const note = useCallback((kind: string, durationSeconds = 0) => {
    buffer.current.push({
      kind,
      occurred_at: new Date().toISOString(),
      duration_seconds: durationSeconds,
    })
  }, [])

  const flush = useCallback(async () => {
    if (!buffer.current.length) return
    const batch = buffer.current.splice(0, buffer.current.length)
    try {
      await api.post(`/cbt/attempts/${session.attempt_id}/integrity-events`, batch)
    } catch {
      // Put them back and try again on the next flush. Losing observations
      // silently would leave a candidate's record incomplete through no fault
      // of their own, and an incomplete record is worse than a late one.
      buffer.current.unshift(...batch)
    }
  }, [session.attempt_id])

  const submit = useCallback(
    async (auto = false) => {
      setBusy(true)
      if (auto) note('auto_submitted')
      await flush()
      try {
        const summary = await api.post<Record<string, unknown>>(
          `/cbt/attempts/${session.attempt_id}/submit`,
          {},
          { params: { session_token: session.session_token } },
        )
        onSubmitted(summary)
      } finally {
        setBusy(false)
      }
    },
    [flush, note, onSubmitted, session.attempt_id, session.session_token],
  )

  // ---- The clock -------------------------------------------------------
  // Counts down locally for display only. The authoritative value comes back
  // from the server with every saved answer, and the server enforces expiry
  // regardless of what this says.
  useEffect(() => {
    const timer = setInterval(() => {
      setRemaining((seconds) => {
        if (seconds <= 1 && d.auto_submit_on_expiry) {
          void submit(true)
          return 0
        }
        return Math.max(0, seconds - 1)
      })
    }, 1000)
    return () => clearInterval(timer)
  }, [d.auto_submit_on_expiry, submit])

  // ---- Conduct: only what the policy asks for --------------------------
  useEffect(() => {
    if (!d.log_focus_changes) return
    let hiddenAt = 0
    const onBlur = () => {
      hiddenAt = Date.now()
      note('window_blurred')
    }
    const onFocus = () => {
      note('window_focused', hiddenAt ? Math.round((Date.now() - hiddenAt) / 1000) : 0)
      void flush()
    }
    const onVisibility = () =>
      note(document.hidden ? 'tab_hidden' : 'tab_visible')

    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [d.log_focus_changes, flush, note])

  useEffect(() => {
    if (!d.block_copy_paste && !d.block_printing && !d.block_context_menu) return
    const block = (kind: string, enabled: boolean) => (event: Event) => {
      if (!enabled) return
      event.preventDefault()
      if (d.log_clipboard_attempts) note(kind)
    }
    const onCopy = block('copy_blocked', d.block_copy_paste)
    const onPaste = block('paste_blocked', d.block_copy_paste)
    const onCut = block('cut_blocked', d.block_copy_paste)
    const onPrint = block('print_blocked', d.block_printing)
    const onContext = block('context_menu_blocked', d.block_context_menu)

    document.addEventListener('copy', onCopy)
    document.addEventListener('paste', onPaste)
    document.addEventListener('cut', onCut)
    window.addEventListener('beforeprint', onPrint)
    document.addEventListener('contextmenu', onContext)
    return () => {
      document.removeEventListener('copy', onCopy)
      document.removeEventListener('paste', onPaste)
      document.removeEventListener('cut', onCut)
      window.removeEventListener('beforeprint', onPrint)
      document.removeEventListener('contextmenu', onContext)
    }
  }, [d, note])

  useEffect(() => {
    if (!d.require_fullscreen) return
    const onChange = () =>
      note(document.fullscreenElement ? 'fullscreen_entered' : 'fullscreen_exited')
    document.addEventListener('fullscreenchange', onChange)
    void document.documentElement.requestFullscreen?.().catch(() => {
      // A browser may refuse without a user gesture. Recorded, not enforced:
      // refusing to run the paper would punish the candidate for their
      // browser's settings.
      note('fullscreen_exited')
    })
    return () => document.removeEventListener('fullscreenchange', onChange)
  }, [d.require_fullscreen, note])

  useEffect(() => {
    const timer = setInterval(() => void flush(), 20_000)
    return () => clearInterval(timer)
  }, [flush])

  if (isLoading) return <LoadingRows rows={6} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!current) return <EmptyState title="This paper has no questions." />

  const save = async (keys: string[]) => {
    setAnswers((prev) => ({ ...prev, [current.question_id]: keys }))
    const seconds = Math.round((Date.now() - questionShownAt.current) / 1000)
    const response = await api.post<{ remaining_seconds: number }>(
      `/cbt/attempts/${session.attempt_id}/answers`,
      {
        question_id: current.question_id,
        selected_keys: keys,
        seconds_spent: seconds,
        flagged_for_review: flags[current.question_id] ?? false,
      },
      { params: { session_token: session.session_token } },
    )
    // Re-synchronise with the server's clock rather than trusting the local one.
    setRemaining(response.remaining_seconds)
  }

  const go = (next: number) => {
    questionShownAt.current = Date.now()
    setIndex(Math.max(0, Math.min(questions.length - 1, next)))
  }

  const answered = Object.keys(answers).length
  const minutes = Math.floor(remaining / 60)
  const seconds = remaining % 60

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <Badge tone={remaining < 300 ? 'critical' : 'neutral'}>
            <Clock className="mr-1 h-3 w-3" aria-hidden />
            {minutes}:{String(seconds).padStart(2, '0')}
          </Badge>
          <span className="text-sm text-[var(--text-secondary)]">
            Question {index + 1} of {questions.length} · {answered} answered
          </span>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" onClick={onAbandon}>
            Leave (saved)
          </Button>
          <Button onClick={() => void submit(false)} disabled={busy}>
            {busy ? 'Submitting…' : 'Submit paper'}
          </Button>
        </div>
      </div>
      <Progress value={(answered / questions.length) * 100} />

      <Card>
        <CardBody className="space-y-4">
          <p className="whitespace-pre-line text-[var(--text-primary)]">{current.stem}</p>
          {current.lead_in && (
            <p className="font-medium text-[var(--text-primary)]">{current.lead_in}</p>
          )}
          <fieldset className="space-y-2">
            <legend className="sr-only">Select the single best answer</legend>
            {current.options.map((option) => {
              const selected = (answers[current.question_id] ?? current.selected_keys)
                .includes(option.key)
              return (
                <label
                  key={option.key}
                  className={`flex cursor-pointer items-start gap-3 rounded-lg border p-3 ${
                    selected
                      ? 'border-[var(--brand)] bg-[var(--brand-wash)]'
                      : 'border-[var(--border-hairline)]'
                  }`}
                >
                  <input
                    type="radio"
                    name={current.question_id}
                    className="mt-1"
                    checked={selected}
                    onChange={() => void save([option.key])}
                  />
                  <span>
                    <strong className="mr-2">{option.key}.</strong>
                    {option.text}
                  </span>
                </label>
              )
            })}
          </fieldset>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <Button
              variant="ghost"
              onClick={() => {
                setFlags((f) => ({
                  ...f,
                  [current.question_id]: !f[current.question_id],
                }))
              }}
            >
              <Flag className="mr-1 h-4 w-4" aria-hidden />
              {flags[current.question_id] ? 'Unflag' : 'Flag for review'}
            </Button>
            <div className="flex gap-2">
              <Button variant="ghost" onClick={() => go(index - 1)} disabled={index === 0}>
                Previous
              </Button>
              <Button
                onClick={() => go(index + 1)}
                disabled={index === questions.length - 1}
              >
                Next
              </Button>
            </div>
          </div>
        </CardBody>
      </Card>
    </div>
  )
}

// ==========================================================================
// Results and per-question review
// ==========================================================================
function Results({
  attemptId,
  summary,
  onDone,
}: {
  attemptId: string
  summary: Record<string, unknown>
  onDone: () => void
}) {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['cbt-feedback', attemptId],
    queryFn: () =>
      api.get<{ questions: Feedback[]; percent_score: number; is_pass: boolean }>(
        `/cbt/attempts/${attemptId}/feedback`,
      ),
  })

  const percent = Number(summary.percent_score ?? 0)
  const integrity = summary.integrity as
    | { requires_human_review: boolean; observations: { summary: string }[] }
    | undefined

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader title="Result" />
        <CardBody className="space-y-3">
          <div className="flex flex-wrap items-baseline gap-4">
            <span className="text-4xl font-semibold tabular-nums">
              {formatNumber(percent, 1)}%
            </span>
            <Badge tone={summary.is_pass ? 'good' : 'warning'}>
              {summary.is_pass ? 'Pass' : 'Below the pass mark'}
            </Badge>
            {typeof summary.cohort_percentile === 'number' && (
              <span className="text-sm text-[var(--text-secondary)]">
                {formatNumber(summary.cohort_percentile, 0)}th percentile in your cohort
              </span>
            )}
          </div>

          {integrity?.requires_human_review && (
            <div className="rounded-lg bg-[var(--status-warning-wash)] p-3 text-sm">
              <p className="flex items-center gap-2 font-medium text-[var(--text-primary)]">
                <AlertTriangle className="h-4 w-4" aria-hidden />
                This sitting has been referred for routine review
              </p>
              <p className="mt-1 text-[var(--text-secondary)]">
                Your department will look at the conduct record. This is not a
                finding against you and does not affect the result above. You will
                be invited to comment before any conclusion is drawn.
              </p>
            </div>
          )}

          <Button variant="ghost" onClick={onDone}>
            Back to papers
          </Button>
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="Review"
          description="Why the correct answer is correct, and why each other option is not."
        />
        <CardBody className="space-y-6">
          {isLoading && <LoadingRows rows={4} />}
          {error && <ErrorState error={error} retry={refetch} />}
          {data?.questions.map((q) => (
            <article key={q.question_id} className="space-y-3 border-t pt-4 first:border-0 first:pt-0">
              <header className="flex flex-wrap items-center gap-2">
                <Badge tone={q.is_correct ? 'good' : 'critical'}>
                  {q.is_correct ? (
                    <CheckCircle2 className="mr-1 h-3 w-3" aria-hidden />
                  ) : (
                    <AlertTriangle className="mr-1 h-3 w-3" aria-hidden />
                  )}
                  {q.is_correct ? 'Correct' : 'Incorrect'}
                </Badge>
                {q.topic && <Badge tone="neutral">{humanise(q.topic)}</Badge>}
                {q.difficulty_band && (
                  <Badge tone="neutral">{humanise(q.difficulty_band)}</Badge>
                )}
                {q.cohort_facility !== null && (
                  <span className="text-xs text-[var(--text-muted)]">
                    {formatNumber(q.cohort_facility * 100, 0)}% of candidates answered
                    this correctly
                  </span>
                )}
              </header>

              <p className="whitespace-pre-line text-sm text-[var(--text-primary)]">
                {q.stem}
              </p>

              <ul className="space-y-2">
                {q.options.map((option) => (
                  <li
                    key={option.key}
                    className={`rounded-lg border p-3 text-sm ${
                      option.is_correct
                        ? 'border-[var(--status-good)] bg-[var(--status-good-wash)]'
                        : option.was_selected
                          ? 'border-[var(--status-critical)] bg-[var(--status-critical-wash)]'
                          : 'border-[var(--border-hairline)]'
                    }`}
                  >
                    <div className="flex items-baseline gap-2">
                      <strong>{option.key}.</strong>
                      <span>{option.text}</span>
                      {/* Icon plus wording, never colour alone. */}
                      {option.is_correct && (
                        <Badge tone="good">
                          <CheckCircle2 className="mr-1 h-3 w-3" aria-hidden />
                          Correct answer
                        </Badge>
                      )}
                      {option.was_selected && !option.is_correct && (
                        <Badge tone="critical">Your answer</Badge>
                      )}
                    </div>
                    <p className="mt-1 text-[var(--text-secondary)]">{option.rationale}</p>
                  </li>
                ))}
              </ul>

              {q.explanation && (
                <p className="rounded-lg bg-[var(--surface-2)] p-3 text-sm text-[var(--text-secondary)]">
                  {q.explanation}
                </p>
              )}

              {q.references.length > 0 && (
                <p className="text-xs text-[var(--text-muted)]">
                  {q.references.join(' · ')}
                </p>
              )}

              {q.authoring_source !== 'human' && (
                <p className="text-xs text-[var(--text-muted)]">
                  This item was written with AI assistance and reviewed by a
                  clinician before publication.
                </p>
              )}
            </article>
          ))}
        </CardBody>
      </Card>
    </div>
  )
}

/**
 * A stable, non-identifying browser signature.
 *
 * Deliberately coarse — screen shape, platform, timezone. It answers "is this
 * the same browser as five minutes ago?" and nothing else. The server salts and
 * hashes it before storage, so even this never lands in the database.
 */
function deviceSignature(): string {
  const parts = [
    navigator.userAgent,
    navigator.language,
    `${screen.width}x${screen.height}`,
    String(new Date().getTimezoneOffset()),
  ]
  return parts.join('|')
}
