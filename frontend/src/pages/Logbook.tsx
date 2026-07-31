/**
 * The digital logbook.
 *
 * The capture form is the screen most used and the one most often used with no
 * signal, so it writes to the local outbox first and only then attempts the
 * network. A registrar who logs a case in theatre gets confirmation immediately;
 * the sync engine reconciles later.
 */

import { useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  BookOpen,
  CheckCircle2,
  CloudUpload,
  Filter,
  Plus,
  Search,
  X,
} from 'lucide-react'

import { api, OfflineError } from '@/lib/api'
import { enqueue, newClientUuid, readCollection } from '@/lib/db'
import { synchronise } from '@/lib/sync'
import { useAuth } from '@/lib/auth'
import { download, formatDate, formatNumber, humanise, toCsv } from '@/lib/utils'
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
  Select,
  Td,
  Textarea,
  Th,
  TableWrap,
} from '@/components/ui'
import { CategoryBars, StatTile, VolumeChart } from '@/components/charts'

interface LogEntry {
  id: string
  entry_type: string
  occurred_on: string
  title: string
  diagnosis: string | null
  procedure_name: string | null
  procedure_grade: string | null
  participation_role: string | null
  complexity: string
  outcome: string
  quantity: number
  validation_status: string
  validator_comment: string | null
  supervisor_name: string | null
  captured_offline: boolean
}

const ENTRY_TYPES = [
  'admission',
  'discharge',
  'ward_round',
  'clinic',
  'major_procedure',
  'minor_procedure',
  'emergency_call',
  'consultation',
  'teaching',
  'simulation',
  'skill_practice',
  'death',
  'complication',
]

const ROLES = [
  'observed',
  'assisted',
  'performed_supervised',
  'performed_independent',
  'supervised_other',
]

const STATUS_TONE: Record<string, 'good' | 'warning' | 'critical' | 'neutral'> = {
  validated: 'good',
  pending: 'warning',
  queried: 'warning',
  rejected: 'critical',
  draft: 'neutral',
}

// --------------------------------------------------------------------------
function EntryForm({ onClose }: { onClose: () => void }) {
  const queryClient = useQueryClient()
  const [saved, setSaved] = useState<'online' | 'queued' | null>(null)
  const [form, setForm] = useState({
    entry_type: 'major_procedure',
    occurred_on: new Date().toISOString().slice(0, 10),
    occurred_time: new Date().toTimeString().slice(0, 5),
    title: '',
    diagnosis: '',
    procedure_id: '',
    procedure_name: '',
    procedure_grade: 'major',
    participation_role: 'assisted',
    complexity: 'routine',
    outcome: 'uneventful',
    setting: 'theatre',
    patient_age_years: '',
    patient_sex: '',
    duration_minutes: '',
    quantity: '1',
    reflection: '',
  })

  // The procedure catalogue is mirrored locally so the picker works offline.
  const { data: procedures = [] } = useQuery({
    queryKey: ['procedures'],
    queryFn: async () => {
      try {
        return await api.get<{ id: string; name: string; grade: string }[]>(
          '/curriculum/procedures',
          { limit: 400 },
        )
      } catch (error) {
        if (error instanceof OfflineError) {
          return readCollection<{ id: string; name: string; grade: string }>(
            'procedure_catalogue',
          )
        }
        throw error
      }
    },
  })

  const isProcedure =
    form.entry_type === 'major_procedure' || form.entry_type === 'minor_procedure'

  const mutation = useMutation({
    mutationFn: async () => {
      const clientUuid = newClientUuid()
      const payload: Record<string, unknown> = {
        entry_type: form.entry_type,
        occurred_at: new Date(`${form.occurred_on}T${form.occurred_time}:00`).toISOString(),
        title: form.title.trim(),
        diagnosis: form.diagnosis.trim() || null,
        setting: form.setting || null,
        complexity: form.complexity,
        outcome: form.outcome,
        quantity: Number(form.quantity) || 1,
        reflection: form.reflection.trim() || null,
        client_uuid: clientUuid,
        ...(form.patient_age_years ? { patient_age_years: Number(form.patient_age_years) } : {}),
        ...(form.patient_sex ? { patient_sex: form.patient_sex } : {}),
        ...(form.duration_minutes ? { duration_minutes: Number(form.duration_minutes) } : {}),
        ...(isProcedure
          ? {
              procedure_id: form.procedure_id || null,
              procedure_name:
                procedures.find((p) => p.id === form.procedure_id)?.name ||
                form.procedure_name.trim() ||
                form.title.trim(),
              procedure_grade: form.procedure_grade,
              participation_role: form.participation_role,
            }
          : {}),
      }

      try {
        const created = await api.post<LogEntry>('/logbook', {
          ...payload,
          captured_offline: false,
        })
        return { mode: 'online' as const, created }
      } catch (error) {
        if (!(error instanceof OfflineError)) throw error
        // No connection: queue it. The server de-duplicates on client_uuid, so a
        // later retry cannot produce a second entry.
        await enqueue({
          clientUuid,
          collection: 'log_entries',
          op: 'create',
          payload: { ...payload, captured_offline: true },
        })
        return { mode: 'queued' as const }
      }
    },
    onSuccess: (result) => {
      setSaved(result.mode)
      void queryClient.invalidateQueries({ queryKey: ['logbook'] })
      void queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    mutation.mutate()
  }

  if (saved) {
    return (
      <Card>
        <CardBody>
          <div className="flex flex-col items-center py-8 text-center">
            <CheckCircle2
              className="mb-3 h-10 w-10"
              style={{ color: saved === 'online' ? 'var(--status-good)' : 'var(--status-warning)' }}
            />
            <p className="text-sm font-medium">
              {saved === 'online' ? 'Entry recorded' : 'Saved on this device'}
            </p>
            <p className="mt-1 max-w-sm text-xs" style={{ color: 'var(--text-muted)' }}>
              {saved === 'online'
                ? 'It is now awaiting consultant validation. Nothing counts toward your requirements until it is signed off.'
                : 'You are offline. This entry is stored securely on this device and will be submitted automatically when you reconnect.'}
            </p>
            <div className="mt-5 flex gap-2">
              <Button
                variant="primary"
                onClick={() => {
                  setSaved(null)
                  setForm((prev) => ({ ...prev, title: '', diagnosis: '', reflection: '' }))
                }}
              >
                Record another
              </Button>
              <Button onClick={onClose}>Done</Button>
            </div>
          </div>
        </CardBody>
      </Card>
    )
  }

  return (
    <Card>
      <CardHeader
        title="Record an activity"
        description="Patient identifiers are never stored — record age, sex and diagnosis only."
        action={
          <Button size="icon" variant="ghost" onClick={onClose} aria-label="Close">
            <X className="h-4 w-4" />
          </Button>
        }
      />
      <CardBody>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="Activity type" required>
              <Select
                value={form.entry_type}
                onChange={(event) => setForm({ ...form, entry_type: event.target.value })}
              >
                {ENTRY_TYPES.map((type) => (
                  <option key={type} value={type}>
                    {humanise(type)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Date" required>
              <Input
                type="date"
                value={form.occurred_on}
                max={new Date().toISOString().slice(0, 10)}
                onChange={(event) => setForm({ ...form, occurred_on: event.target.value })}
                required
              />
            </Field>
            <Field label="Time">
              <Input
                type="time"
                value={form.occurred_time}
                onChange={(event) => setForm({ ...form, occurred_time: event.target.value })}
              />
            </Field>
            <Field label="Setting">
              <Select
                value={form.setting}
                onChange={(event) => setForm({ ...form, setting: event.target.value })}
              >
                {['ward', 'theatre', 'clinic', 'emergency', 'icu', 'labour_ward', 'community'].map(
                  (setting) => (
                    <option key={setting} value={setting}>
                      {humanise(setting)}
                    </option>
                  ),
                )}
              </Select>
            </Field>
          </div>

          <Field label="Title" required hint="A short description a consultant will recognise.">
            <Input
              value={form.title}
              onChange={(event) => setForm({ ...form, title: event.target.value })}
              placeholder="e.g. Emergency appendicectomy"
              required
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Diagnosis">
              <Input
                value={form.diagnosis}
                onChange={(event) => setForm({ ...form, diagnosis: event.target.value })}
                placeholder="e.g. Acute appendicitis"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Patient age (years)">
                <Input
                  type="number"
                  min={0}
                  max={120}
                  value={form.patient_age_years}
                  onChange={(event) =>
                    setForm({ ...form, patient_age_years: event.target.value })
                  }
                />
              </Field>
              <Field label="Patient sex">
                <Select
                  value={form.patient_sex}
                  onChange={(event) => setForm({ ...form, patient_sex: event.target.value })}
                >
                  <option value="">Not recorded</option>
                  <option value="male">Male</option>
                  <option value="female">Female</option>
                </Select>
              </Field>
            </div>
          </div>

          {isProcedure ? (
            <div
              className="space-y-4 rounded-[var(--radius-control)] border p-4"
              style={{ background: 'var(--surface-2)', borderColor: 'var(--border-hairline)' }}
            >
              <p className="text-xs font-medium">Procedure detail</p>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                <Field label="Procedure" className="sm:col-span-2">
                  <Select
                    value={form.procedure_id}
                    onChange={(event) => {
                      const found = procedures.find((p) => p.id === event.target.value)
                      setForm({
                        ...form,
                        procedure_id: event.target.value,
                        procedure_grade: found?.grade ?? form.procedure_grade,
                      })
                    }}
                  >
                    <option value="">Not in the catalogue — free text</option>
                    {procedures.map((procedure) => (
                      <option key={procedure.id} value={procedure.id}>
                        {procedure.name}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Your role" required>
                  <Select
                    value={form.participation_role}
                    onChange={(event) =>
                      setForm({ ...form, participation_role: event.target.value })
                    }
                  >
                    {ROLES.map((role) => (
                      <option key={role} value={role}>
                        {humanise(role)}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Grade">
                  <Select
                    value={form.procedure_grade}
                    onChange={(event) =>
                      setForm({ ...form, procedure_grade: event.target.value })
                    }
                  >
                    <option value="major">Major</option>
                    <option value="intermediate">Intermediate</option>
                    <option value="minor">Minor</option>
                  </Select>
                </Field>
              </div>
              {!form.procedure_id ? (
                <Field label="Procedure name" hint="Used when the procedure is not yet catalogued.">
                  <Input
                    value={form.procedure_name}
                    onChange={(event) => setForm({ ...form, procedure_name: event.target.value })}
                  />
                </Field>
              ) : null}
              <div className="grid gap-4 sm:grid-cols-3">
                <Field label="Complexity">
                  <Select
                    value={form.complexity}
                    onChange={(event) => setForm({ ...form, complexity: event.target.value })}
                  >
                    {['routine', 'intermediate', 'complex', 'highly_complex'].map((level) => (
                      <option key={level} value={level}>
                        {humanise(level)}
                      </option>
                    ))}
                  </Select>
                </Field>
                <Field label="Outcome">
                  <Select
                    value={form.outcome}
                    onChange={(event) => setForm({ ...form, outcome: event.target.value })}
                  >
                    {['uneventful', 'minor_complication', 'major_complication', 'mortality', 'unknown'].map(
                      (outcome) => (
                        <option key={outcome} value={outcome}>
                          {humanise(outcome)}
                        </option>
                      ),
                    )}
                  </Select>
                </Field>
                <Field label="Duration (minutes)">
                  <Input
                    type="number"
                    min={0}
                    value={form.duration_minutes}
                    onChange={(event) =>
                      setForm({ ...form, duration_minutes: event.target.value })
                    }
                  />
                </Field>
              </div>
            </div>
          ) : null}

          <Field
            label="Reflection"
            hint="Optional, but it is what turns a case list into evidence of learning."
          >
            <Textarea
              value={form.reflection}
              onChange={(event) => setForm({ ...form, reflection: event.target.value })}
              placeholder="What did you learn? What would you do differently?"
            />
          </Field>

          {mutation.isError ? <ErrorState error={mutation.error} /> : null}

          <div className="flex items-center justify-end gap-2 pt-1">
            <Button type="button" variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={mutation.isPending}>
              Save entry
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  )
}

// --------------------------------------------------------------------------
export default function Logbook() {
  const { principal } = useAuth()
  const [showForm, setShowForm] = useState(false)
  const [filters, setFilters] = useState({ status: '', type: '', search: '' })

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['logbook', filters],
    queryFn: () =>
      api.get<{ items: LogEntry[]; total: number }>('/logbook', {
        page_size: 200,
        validation_status: filters.status || undefined,
        entry_type: filters.type || undefined,
        search: filters.search || undefined,
      }),
  })

  const { data: summary } = useQuery({
    queryKey: ['logbook', 'summary'],
    queryFn: () => api.get<any>('/logbook/summary'),
    enabled: Boolean(principal?.enrolment),
  })

  const byType = useMemo(
    () =>
      Object.entries(summary?.by_type ?? {}).map(([key, value]) => ({
        name: humanise(key),
        value: Number(value),
      })),
    [summary],
  )

  const byMonth = useMemo(
    () =>
      Object.entries(summary?.by_month ?? {})
        .slice(-12)
        .map(([key, value]) => ({
          label: new Date(`${key}-01`).toLocaleDateString('en-GB', { month: 'short' }),
          value: Number(value),
        })),
    [summary],
  )

  return (
    <div className="space-y-5">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Digital logbook</h1>
          <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
            Only validated entries count toward your training requirements.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            icon={<CloudUpload className="h-4 w-4" />}
            onClick={() => void synchronise()}
          >
            Sync now
          </Button>
          {data?.items?.length ? (
            <Button
              onClick={() =>
                download(
                  `logbook-${new Date().toISOString().slice(0, 10)}.csv`,
                  toCsv(data.items as unknown as Record<string, unknown>[]),
                )
              }
            >
              Export CSV
            </Button>
          ) : null}
          <Button
            variant="primary"
            icon={<Plus className="h-4 w-4" />}
            onClick={() => setShowForm((open) => !open)}
          >
            New entry
          </Button>
        </div>
      </header>

      {showForm ? <EntryForm onClose={() => setShowForm(false)} /> : null}

      {summary ? (
        <>
          <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
            <StatTile label="Total entries" value={formatNumber(summary.total)} />
            <StatTile
              label="Validated"
              value={formatNumber(summary.validated)}
              tone="green"
              hint={`${((summary.validated / Math.max(1, summary.total)) * 100).toFixed(0)}% of all entries`}
            />
            <StatTile label="Awaiting sign-off" value={formatNumber(summary.pending)} tone="amber" />
            <StatTile
              label="Major procedures"
              value={formatNumber(summary.major_procedures)}
              hint={`${formatNumber(summary.minor_procedures)} minor`}
            />
          </div>

          <div className="grid gap-5 lg:grid-cols-2">
            <Card>
              <CardHeader title="Activity mix" description="All entries, by type." />
              <CardBody>
                <CategoryBars data={byType} />
              </CardBody>
            </Card>
            <Card>
              <CardHeader title="Entries per month" description="Last twelve months." />
              <CardBody>
                <VolumeChart data={byMonth} height={240} />
              </CardBody>
            </Card>
          </div>
        </>
      ) : null}

      <Card>
        <CardHeader
          title="Entries"
          description={data ? `${formatNumber(data.total)} records` : undefined}
        />

        {/* Filters sit in one row above the table. */}
        <div
          className="flex flex-wrap items-center gap-2 border-b px-5 py-3"
          style={{ borderColor: 'var(--border-hairline)' }}
        >
          <div className="relative min-w-[12rem] flex-1">
            <Search
              className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 opacity-50"
              aria-hidden
            />
            <Input
              className="pl-8"
              placeholder="Search title, diagnosis or procedure"
              value={filters.search}
              onChange={(event) => setFilters({ ...filters, search: event.target.value })}
            />
          </div>
          <Select
            className="w-auto"
            value={filters.status}
            onChange={(event) => setFilters({ ...filters, status: event.target.value })}
            aria-label="Filter by validation status"
          >
            <option value="">All statuses</option>
            {['pending', 'validated', 'queried', 'rejected'].map((status) => (
              <option key={status} value={status}>
                {humanise(status)}
              </option>
            ))}
          </Select>
          <Select
            className="w-auto"
            value={filters.type}
            onChange={(event) => setFilters({ ...filters, type: event.target.value })}
            aria-label="Filter by activity type"
          >
            <option value="">All types</option>
            {ENTRY_TYPES.map((type) => (
              <option key={type} value={type}>
                {humanise(type)}
              </option>
            ))}
          </Select>
          {filters.status || filters.type || filters.search ? (
            <Button
              size="sm"
              variant="ghost"
              icon={<Filter className="h-3.5 w-3.5" />}
              onClick={() => setFilters({ status: '', type: '', search: '' })}
            >
              Clear
            </Button>
          ) : null}
        </div>

        {isLoading ? (
          <LoadingRows />
        ) : error ? (
          <CardBody>
            <ErrorState error={error} retry={refetch} />
          </CardBody>
        ) : !data?.items.length ? (
          <EmptyState
            icon={<BookOpen className="h-10 w-10" />}
            title="No entries yet"
            description="Record your first activity — it takes about twenty seconds."
            action={
              <Button variant="primary" onClick={() => setShowForm(true)}>
                Record an activity
              </Button>
            }
          />
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th>Date</Th>
                <Th>Activity</Th>
                <Th>Type</Th>
                <Th>Role</Th>
                <Th>Supervisor</Th>
                <Th>Status</Th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((entry) => (
                <tr key={entry.id}>
                  <Td className="tnum whitespace-nowrap text-xs">
                    {formatDate(entry.occurred_on)}
                  </Td>
                  <Td>
                    <p className="font-medium">{entry.title}</p>
                    {entry.diagnosis ? (
                      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                        {entry.diagnosis}
                      </p>
                    ) : null}
                    {entry.validator_comment ? (
                      <p className="mt-0.5 text-xs" style={{ color: 'var(--status-critical)' }}>
                        {entry.validator_comment}
                      </p>
                    ) : null}
                  </Td>
                  <Td className="text-xs">{humanise(entry.entry_type)}</Td>
                  <Td className="text-xs">
                    {entry.participation_role ? humanise(entry.participation_role) : '—'}
                  </Td>
                  <Td className="text-xs">{entry.supervisor_name ?? '—'}</Td>
                  <Td>
                    <div className="flex items-center gap-1.5">
                      <Badge tone={STATUS_TONE[entry.validation_status] ?? 'neutral'}>
                        {humanise(entry.validation_status)}
                      </Badge>
                      {entry.captured_offline ? <Badge tone="info">Offline</Badge> : null}
                    </div>
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
