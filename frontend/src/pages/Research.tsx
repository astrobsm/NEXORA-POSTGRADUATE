/**
 * Research and the dissertation workflow.
 *
 * The stage tracker is the point of the screen: a dissertation fails far more often
 * from drift than from bad science, so where a project actually stands has to be
 * unmissable.
 */

import { useQuery } from '@tanstack/react-query'
import { FlaskConical, GraduationCap } from 'lucide-react'

import { api } from '@/lib/api'
import { formatDate, formatNumber, humanise } from '@/lib/utils'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  LoadingRows,
  Progress,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { StatTile } from '@/components/charts'

interface Project {
  id: string
  title: string
  research_type: string
  submitting_body: string | null
  current_stage: string
  stage_index: number | null
  total_stages: number
  status: string
  progress_percent: number
  ethics_status: string
  ethics_reference: string | null
  started_on: string | null
  target_completion_on: string | null
  keywords: string[]
  supervisors: { user_id: string; name: string | null; is_primary: boolean; allocation_method: string }[]
  milestones_total: number
  milestones_completed: number
  principal_investigator: string | null
}

const STAGES = [
  'concept',
  'supervisor_assignment',
  'topic_approval',
  'proposal_writing',
  'proposal_defence',
  'ethics_approval',
  'data_collection',
  'analysis',
  'draft_submission',
  'corrections',
  'final_defence',
  'college_submission',
  'publication',
  'completed',
]

function StageTracker({ current }: { current: string }) {
  const index = STAGES.indexOf(current)
  return (
    <div>
      <div className="flex gap-[2px]" role="img" aria-label={`Stage: ${humanise(current)}`}>
        {STAGES.map((stage, position) => (
          <span
            key={stage}
            title={humanise(stage)}
            className="h-1.5 flex-1 rounded-full first:rounded-l-full last:rounded-r-full"
            style={{
              background:
                position < index
                  ? 'var(--status-good)'
                  : position === index
                    ? 'var(--brand)'
                    : 'var(--surface-3)',
            }}
          />
        ))}
      </div>
      <p className="mt-1.5 text-xs">
        <span className="font-medium">{humanise(current)}</span>
        <span style={{ color: 'var(--text-muted)' }}>
          {' '}
          · stage {index + 1} of {STAGES.length}
        </span>
      </p>
    </div>
  )
}

export default function Research() {
  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['research-projects'],
    queryFn: () => api.get<{ items: Project[]; total: number }>('/research/projects'),
  })

  const { data: publications } = useQuery({
    queryKey: ['publications'],
    queryFn: () => api.get<any[]>('/research/publications'),
  })

  const projects = data?.items ?? []
  const active = projects.filter((p) => p.current_stage !== 'completed')

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Research &amp; dissertation</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Supervisors are allocated on expertise, availability, workload and declared
          conflicts of interest — and the reasoning is recorded.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile label="Projects" value={formatNumber(projects.length)} icon={<FlaskConical className="h-4 w-4" />} />
        <StatTile label="In progress" value={formatNumber(active.length)} />
        <StatTile
          label="Ethics approved"
          value={formatNumber(projects.filter((p) => p.ethics_status === 'approved').length)}
          tone={active.length && !projects.some((p) => p.ethics_status === 'approved') ? 'amber' : 'green'}
        />
        <StatTile
          label="Publications"
          value={formatNumber(publications?.length ?? 0)}
          hint={`${formatNumber((publications ?? []).filter((p: any) => p.verification_status === 'approved').length)} verified`}
        />
      </div>

      {isLoading ? (
        <Card>
          <LoadingRows />
        </Card>
      ) : error ? (
        <ErrorState error={error} retry={refetch} />
      ) : !projects.length ? (
        <Card>
          <EmptyState
            icon={<GraduationCap className="h-10 w-10" />}
            title="No research projects registered"
            description="A dissertation is a college requirement for most residency programmes. Register the project to have a supervisor allocated."
          />
        </Card>
      ) : (
        <div className="space-y-4">
          {projects.map((project) => (
            <Card key={project.id}>
              <CardHeader
                title={project.title}
                description={`${humanise(project.research_type)}${
                  project.submitting_body ? ` · ${project.submitting_body.toUpperCase()}` : ''
                }${project.principal_investigator ? ` · ${project.principal_investigator}` : ''}`}
                action={
                  <Badge
                    tone={
                      project.current_stage === 'completed'
                        ? 'good'
                        : project.ethics_status === 'approved'
                          ? 'info'
                          : 'warning'
                    }
                  >
                    {humanise(project.current_stage)}
                  </Badge>
                }
              />
              <CardBody className="space-y-4">
                <StageTracker current={project.current_stage} />

                <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                  <div>
                    <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      Milestones
                    </p>
                    <p className="tnum text-sm font-medium">
                      {project.milestones_completed} / {project.milestones_total}
                    </p>
                    <Progress value={project.progress_percent} />
                  </div>
                  <div>
                    <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      Ethics
                    </p>
                    <p className="text-sm font-medium">{humanise(project.ethics_status)}</p>
                    {project.ethics_reference ? (
                      <p className="tnum text-xs" style={{ color: 'var(--text-muted)' }}>
                        {project.ethics_reference}
                      </p>
                    ) : null}
                  </div>
                  <div>
                    <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      Supervisor
                    </p>
                    {project.supervisors.length ? (
                      project.supervisors.map((supervisor) => (
                        <p key={supervisor.user_id} className="text-sm font-medium">
                          {supervisor.name}
                          <span
                            className="ml-1 text-[11px] font-normal"
                            style={{ color: 'var(--text-muted)' }}
                          >
                            ({supervisor.allocation_method})
                          </span>
                        </p>
                      ))
                    ) : (
                      <p className="text-sm" style={{ color: 'var(--status-critical)' }}>
                        Not yet allocated
                      </p>
                    )}
                  </div>
                  <div>
                    <p className="text-[11px]" style={{ color: 'var(--text-muted)' }}>
                      Target completion
                    </p>
                    <p className="text-sm font-medium">
                      {formatDate(project.target_completion_on)}
                    </p>
                  </div>
                </div>

                {project.keywords.length ? (
                  <div className="flex flex-wrap gap-1.5">
                    {project.keywords.map((keyword) => (
                      <Badge key={keyword} tone="neutral">
                        {keyword}
                      </Badge>
                    ))}
                  </div>
                ) : null}
              </CardBody>
            </Card>
          ))}
        </div>
      )}

      {publications?.length ? (
        <Card>
          <CardHeader
            title="Publications"
            description="Only verified publications count toward the research score."
          />
          <TableWrap>
            <thead>
              <tr>
                <Th>Title</Th>
                <Th>Venue</Th>
                <Th>Year</Th>
                <Th>Position</Th>
                <Th>Status</Th>
              </tr>
            </thead>
            <tbody>
              {publications.map((publication: any) => (
                <tr key={publication.id}>
                  <Td>
                    <p className="font-medium">{publication.title}</p>
                    <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                      {publication.authors}
                    </p>
                  </Td>
                  <Td className="text-xs">{publication.venue ?? '—'}</Td>
                  <Td className="tnum text-xs">{publication.year}</Td>
                  <Td className="text-xs">
                    {publication.author_position === 1 ? 'First author' : `#${publication.author_position}`}
                  </Td>
                  <Td>
                    <Badge tone={publication.verification_status === 'approved' ? 'good' : 'warning'}>
                      {humanise(publication.verification_status)}
                    </Badge>
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        </Card>
      ) : null}
    </div>
  )
}
