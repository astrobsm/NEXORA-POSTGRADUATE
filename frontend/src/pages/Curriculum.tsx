/**
 * The curriculum builder.
 *
 * This is where the platform's central claim becomes concrete: a department changes
 * its training policy by editing rows here, and the promotion engine, the analytics
 * and the accreditation returns all change with it. No deployment involved.
 */

import { useMemo, useState, type FormEvent } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { GraduationCap, Plus, Sliders, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { formatNumber, humanise } from '@/lib/utils'
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
  Tabs,
  Td,
  Textarea,
  Th,
  TableWrap,
} from '@/components/ui'

interface Programme {
  id: string
  code: string
  name: string
  programme_type: string
  awarding_body: string | null
  awarding_body_name: string | null
  duration_months: number
  annual_intake: number
  entry_level: string
  exit_level: string
}

interface Version {
  id: string
  version: string
  title: string
  status: string
  effective_from: string | null
  training_years: number
  competencies: number
  requirements: number
}

interface Rule {
  id: string
  code: string | null
  label: string
  kind: string
  operator: string
  target_value: number
  parameters: Record<string, unknown>
  scope: string
  severity: string
  weight: number
  score_domain: string | null
  guidance: string | null
  source_reference: string | null
  is_active: boolean
}

const OPERATOR_LABEL: Record<string, string> = {
  gte: 'at least',
  gt: 'more than',
  lte: 'at most',
  lt: 'fewer than',
  eq: 'exactly',
  neq: 'not equal to',
}

// --------------------------------------------------------------------------
function RuleBuilder({
  versionId,
  vocabulary,
  onDone,
}: {
  versionId: string
  vocabulary: any
  onDone: () => void
}) {
  const queryClient = useQueryClient()
  const [form, setForm] = useState({
    label: '',
    kind: 'procedure_count',
    operator: 'gte',
    target_value: '10',
    scope: 'programme',
    severity: 'mandatory',
    score_domain: 'clinical_competency',
    weight: '1',
    guidance: '',
    source_reference: '',
    parameters: '{}',
  })
  const [parseError, setParseError] = useState<string | null>(null)

  const hints: string[] = useMemo(() => {
    const entry = vocabulary?.kinds?.find((k: any) => k.kind === form.kind)
    return entry?.parameters ?? []
  }, [vocabulary, form.kind])

  const create = useMutation({
    mutationFn: () => {
      let parameters: Record<string, unknown> = {}
      try {
        parameters = form.parameters.trim() ? JSON.parse(form.parameters) : {}
      } catch {
        throw new Error('Parameters must be valid JSON, for example {"grade": "major"}.')
      }
      return api.post(`/curriculum/versions/${versionId}/requirements`, {
        label: form.label.trim(),
        kind: form.kind,
        operator: form.operator,
        target_value: Number(form.target_value),
        scope: form.scope,
        severity: form.severity,
        score_domain: form.score_domain || null,
        weight: Number(form.weight) || 1,
        guidance: form.guidance.trim() || null,
        source_reference: form.source_reference.trim() || null,
        parameters,
      })
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['requirements', versionId] })
      onDone()
    },
  })

  function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setParseError(null)
    try {
      if (form.parameters.trim()) JSON.parse(form.parameters)
    } catch {
      setParseError('Parameters must be valid JSON.')
      return
    }
    create.mutate()
  }

  return (
    <Card>
      <CardHeader
        title="Add a training requirement"
        description="This becomes policy the moment you save it — the promotion engine and analytics pick it up on the next evaluation."
      />
      <CardBody>
        <form onSubmit={handleSubmit} className="space-y-4">
          <Field
            label="Requirement"
            required
            hint="Write it as a trainee would read it, e.g. '40 major operations performed under supervision'."
          >
            <Input
              value={form.label}
              onChange={(event) => setForm({ ...form, label: event.target.value })}
              required
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <Field label="What is measured" required>
              <Select
                value={form.kind}
                onChange={(event) => setForm({ ...form, kind: event.target.value })}
              >
                {(vocabulary?.kinds ?? [])
                  .filter((k: any) => k.implemented)
                  .map((k: any) => (
                    <option key={k.kind} value={k.kind}>
                      {humanise(k.kind)}
                    </option>
                  ))}
              </Select>
            </Field>
            <Field label="Comparison" required>
              <Select
                value={form.operator}
                onChange={(event) => setForm({ ...form, operator: event.target.value })}
              >
                {(vocabulary?.operators ?? []).map((op: string) => (
                  <option key={op} value={op}>
                    {OPERATOR_LABEL[op] ?? op}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Target" required>
              <Input
                type="number"
                step="any"
                value={form.target_value}
                onChange={(event) => setForm({ ...form, target_value: event.target.value })}
                required
              />
            </Field>
            <Field label="Measured over">
              <Select
                value={form.scope}
                onChange={(event) => setForm({ ...form, scope: event.target.value })}
              >
                {(vocabulary?.scopes ?? []).map((scope: string) => (
                  <option key={scope} value={scope}>
                    {humanise(scope)}
                  </option>
                ))}
              </Select>
            </Field>
          </div>

          <div className="grid gap-4 sm:grid-cols-3">
            <Field label="Severity" hint="Mandatory requirements block promotion.">
              <Select
                value={form.severity}
                onChange={(event) => setForm({ ...form, severity: event.target.value })}
              >
                {(vocabulary?.severities ?? []).map((severity: string) => (
                  <option key={severity} value={severity}>
                    {humanise(severity)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Contributes to score domain">
              <Select
                value={form.score_domain}
                onChange={(event) => setForm({ ...form, score_domain: event.target.value })}
              >
                <option value="">None</option>
                {(vocabulary?.score_domains ?? []).map((domain: string) => (
                  <option key={domain} value={domain}>
                    {humanise(domain)}
                  </option>
                ))}
              </Select>
            </Field>
            <Field label="Weight" hint="Relative importance within its domain.">
              <Input
                type="number"
                step="0.5"
                min="0"
                value={form.weight}
                onChange={(event) => setForm({ ...form, weight: event.target.value })}
              />
            </Field>
          </div>

          <Field
            label="Parameters (JSON)"
            error={parseError ?? undefined}
            hint={
              hints.length
                ? `Recognised for this measurement: ${hints.join(', ')}`
                : 'This measurement takes no parameters.'
            }
          >
            <Textarea
              value={form.parameters}
              onChange={(event) => setForm({ ...form, parameters: event.target.value })}
              className="font-mono text-xs"
              rows={3}
              placeholder='{"grade": "major", "role": "performed_supervised"}'
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Guidance for trainees">
              <Input
                value={form.guidance}
                onChange={(event) => setForm({ ...form, guidance: event.target.value })}
                placeholder="Only entries validated by a consultant are counted."
              />
            </Field>
            <Field
              label="Source reference"
              hint="Which college regulation this implements — kept for audit."
            >
              <Input
                value={form.source_reference}
                onChange={(event) => setForm({ ...form, source_reference: event.target.value })}
                placeholder="NPMCN residency training guidelines §4.2"
              />
            </Field>
          </div>

          {create.isError ? <ErrorState error={create.error} /> : null}

          <div className="flex justify-end gap-2">
            <Button type="button" variant="ghost" onClick={onDone}>
              Cancel
            </Button>
            <Button type="submit" variant="primary" loading={create.isPending}>
              Save requirement
            </Button>
          </div>
        </form>
      </CardBody>
    </Card>
  )
}

// --------------------------------------------------------------------------
export default function Curriculum() {
  const { can } = useAuth()
  const queryClient = useQueryClient()
  const [programmeId, setProgrammeId] = useState('')
  const [versionId, setVersionId] = useState('')
  const [tab, setTab] = useState('requirements')
  const [building, setBuilding] = useState(false)

  const { data: programmes, isLoading } = useQuery({
    queryKey: ['programmes'],
    queryFn: () => api.get<Programme[]>('/curriculum/programmes'),
  })

  const { data: versions } = useQuery({
    queryKey: ['versions', programmeId],
    queryFn: () => api.get<Version[]>(`/curriculum/programmes/${programmeId}/versions`),
    enabled: Boolean(programmeId),
  })

  const { data: version } = useQuery({
    queryKey: ['version', versionId],
    queryFn: () => api.get<any>(`/curriculum/versions/${versionId}`),
    enabled: Boolean(versionId),
  })

  const { data: vocabulary } = useQuery({
    queryKey: ['requirement-vocabulary'],
    queryFn: () => api.get<any>('/curriculum/requirement-kinds'),
  })

  const remove = useMutation({
    mutationFn: (ruleId: string) => api.delete(`/curriculum/requirements/${ruleId}`),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['version', versionId] })
    },
  })

  const selectedProgramme = programmes?.find((p) => p.id === programmeId)
  const rules: Rule[] = version?.requirements ?? []
  const byScope = rules.reduce<Record<string, Rule[]>>((acc, rule) => {
    ;(acc[rule.scope] ??= []).push(rule)
    return acc
  }, {})

  if (isLoading) return <LoadingRows rows={6} />

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Curriculum builder</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Training policy is data. Requirements defined here are what the promotion
          engine evaluates — changing them never requires a software change.
        </p>
      </header>

      <Card>
        <CardBody className="grid gap-4 sm:grid-cols-2">
          <Field label="Programme">
            <Select
              value={programmeId}
              onChange={(event) => {
                setProgrammeId(event.target.value)
                setVersionId('')
              }}
            >
              <option value="">Choose a programme…</option>
              {(programmes ?? []).map((programme) => (
                <option key={programme.id} value={programme.id}>
                  {programme.name} ({programme.code})
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Curriculum version">
            <Select
              value={versionId}
              onChange={(event) => setVersionId(event.target.value)}
              disabled={!programmeId}
            >
              <option value="">Choose a version…</option>
              {(versions ?? []).map((entry) => (
                <option key={entry.id} value={entry.id}>
                  v{entry.version} — {entry.title} ({entry.status})
                </option>
              ))}
            </Select>
          </Field>
        </CardBody>
      </Card>

      {selectedProgramme && !versionId ? (
        <Card>
          <CardHeader title={selectedProgramme.name} />
          <CardBody className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <div>
              <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Awarding body
              </p>
              <p className="text-sm font-medium">
                {selectedProgramme.awarding_body_name ?? 'Institution-awarded'}
              </p>
            </div>
            <div>
              <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Duration
              </p>
              <p className="tnum text-sm font-medium">{selectedProgramme.duration_months} months</p>
            </div>
            <div>
              <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Progression
              </p>
              <p className="text-sm font-medium">
                {humanise(selectedProgramme.entry_level)} → {humanise(selectedProgramme.exit_level)}
              </p>
            </div>
            <div>
              <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Annual intake
              </p>
              <p className="tnum text-sm font-medium">{selectedProgramme.annual_intake}</p>
            </div>
          </CardBody>
        </Card>
      ) : null}

      {!versionId ? (
        <Card>
          <EmptyState
            icon={<GraduationCap className="h-10 w-10" />}
            title="Choose a programme and version"
            description="Select above to inspect or edit the training years, competencies and requirement rules."
          />
        </Card>
      ) : (
        <>
          <Tabs
            tabs={[
              { id: 'requirements', label: 'Requirements', count: rules.length },
              { id: 'structure', label: 'Years & rotations', count: version?.training_years?.length },
              { id: 'competencies', label: 'Competencies', count: version?.competencies?.length },
            ]}
            active={tab}
            onChange={setTab}
          />

          {tab === 'requirements' ? (
            <>
              {building && can('curriculum.manage') ? (
                <RuleBuilder
                  versionId={versionId}
                  vocabulary={vocabulary}
                  onDone={() => setBuilding(false)}
                />
              ) : null}

              {can('curriculum.manage') && !building ? (
                <div className="flex justify-end">
                  <Button
                    variant="primary"
                    icon={<Plus className="h-4 w-4" />}
                    onClick={() => setBuilding(true)}
                  >
                    Add requirement
                  </Button>
                </div>
              ) : null}

              {Object.entries(byScope).map(([scope, scopeRules]) => (
                <Card key={scope}>
                  <CardHeader
                    title={humanise(scope)}
                    description={`${scopeRules.length} requirement(s) evaluated at this scope`}
                  />
                  <TableWrap>
                    <thead>
                      <tr>
                        <Th>Requirement</Th>
                        <Th>Measurement</Th>
                        <Th>Parameters</Th>
                        <Th>Domain</Th>
                        <Th>Severity</Th>
                        {can('curriculum.manage') ? <Th /> : null}
                      </tr>
                    </thead>
                    <tbody>
                      {scopeRules.map((rule) => (
                        <tr key={rule.id}>
                          <Td>
                            <p className="font-medium">{rule.label}</p>
                            {rule.source_reference ? (
                              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                                {rule.source_reference}
                              </p>
                            ) : null}
                          </Td>
                          <Td className="text-xs">
                            {humanise(rule.kind)}{' '}
                            <span style={{ color: 'var(--text-muted)' }}>
                              {OPERATOR_LABEL[rule.operator] ?? rule.operator}
                            </span>{' '}
                            <span className="tnum font-medium">
                              {formatNumber(rule.target_value, 1)}
                            </span>
                          </Td>
                          <Td>
                            {Object.keys(rule.parameters ?? {}).length ? (
                              <code
                                className="rounded px-1.5 py-0.5 text-[11px]"
                                style={{ background: 'var(--surface-2)' }}
                              >
                                {JSON.stringify(rule.parameters)}
                              </code>
                            ) : (
                              <span className="text-xs" style={{ color: 'var(--text-muted)' }}>
                                —
                              </span>
                            )}
                          </Td>
                          <Td className="text-xs">
                            {rule.score_domain ? humanise(rule.score_domain) : '—'}
                          </Td>
                          <Td>
                            <Badge
                              tone={
                                rule.severity === 'mandatory'
                                  ? 'critical'
                                  : rule.severity === 'recommended'
                                    ? 'warning'
                                    : 'neutral'
                              }
                            >
                              {humanise(rule.severity)}
                            </Badge>
                          </Td>
                          {can('curriculum.manage') ? (
                            <Td className="text-right">
                              <Button
                                size="icon"
                                variant="ghost"
                                aria-label={`Remove ${rule.label}`}
                                loading={remove.isPending && remove.variables === rule.id}
                                onClick={() => remove.mutate(rule.id)}
                              >
                                <Trash2 className="h-3.5 w-3.5" />
                              </Button>
                            </Td>
                          ) : null}
                        </tr>
                      ))}
                    </tbody>
                  </TableWrap>
                </Card>
              ))}

              {!rules.length ? (
                <Card>
                  <EmptyState
                    icon={<Sliders className="h-10 w-10" />}
                    title="No requirements defined"
                    description="Without requirements this curriculum cannot measure progress or gate promotion."
                  />
                </Card>
              ) : null}
            </>
          ) : null}

          {tab === 'structure' ? (
            <div className="space-y-4">
              {(version?.training_years ?? []).map((year: any) => (
                <Card key={year.id}>
                  <CardHeader
                    title={`${year.name} — ${humanise(year.level)}`}
                    description={`${year.duration_months} months · ${year.rotations.length} postings`}
                  />
                  <TableWrap>
                    <thead>
                      <tr>
                        <Th>Posting</Th>
                        <Th>Duration</Th>
                        <Th>Capacity</Th>
                        <Th>Type</Th>
                        <Th>Required assessments</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {year.rotations.map((rotation: any) => (
                        <tr key={rotation.id}>
                          <Td className="font-medium">{rotation.name}</Td>
                          <Td className="tnum text-xs">{rotation.duration_weeks} weeks</Td>
                          <Td className="tnum text-xs">{rotation.max_trainees ?? '—'}</Td>
                          <Td>
                            <Badge tone={rotation.is_elective ? 'neutral' : 'info'}>
                              {rotation.is_elective ? 'Elective' : 'Core'}
                            </Badge>
                          </Td>
                          <Td className="text-xs">
                            {rotation.required_assessments?.join(', ') || '—'}
                          </Td>
                        </tr>
                      ))}
                    </tbody>
                  </TableWrap>
                </Card>
              ))}
            </div>
          ) : null}

          {tab === 'competencies' ? (
            <Card>
              <CardHeader
                title="Competencies & EPAs"
                description="Target entrustment levels by training year."
              />
              <TableWrap>
                <thead>
                  <tr>
                    <Th>Code</Th>
                    <Th>Competency</Th>
                    <Th>Domain</Th>
                    <Th>Targets by year</Th>
                    <Th>Exit target</Th>
                  </tr>
                </thead>
                <tbody>
                  {(version?.competencies ?? []).map((competency: any) => (
                    <tr key={competency.id}>
                      <Td className="tnum text-xs font-medium">{competency.code}</Td>
                      <Td>
                        <div className="flex items-center gap-1.5">
                          <span className="text-sm">{competency.title}</span>
                          {competency.is_epa ? <Badge tone="brand">EPA</Badge> : null}
                        </div>
                      </Td>
                      <Td className="text-xs">{humanise(competency.domain)}</Td>
                      <Td className="text-xs">
                        {Object.entries(competency.target_by_year ?? {})
                          .map(([year, level]) => `Y${year}: ${humanise(String(level).slice(2))}`)
                          .join(' · ') || '—'}
                      </Td>
                      <Td className="text-xs">{humanise(competency.exit_target.slice(2))}</Td>
                    </tr>
                  ))}
                </tbody>
              </TableWrap>
            </Card>
          ) : null}
        </>
      )}
    </div>
  )
}
