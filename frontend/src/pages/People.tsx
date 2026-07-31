import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Search, Users } from 'lucide-react'

import { api } from '@/lib/api'
import { formatDate, formatNumber, humanise, initials } from '@/lib/utils'
import {
  Badge,
  Card,
  CardBody,
  CardHeader,
  EmptyState,
  ErrorState,
  Input,
  LoadingRows,
  Select,
  Tabs,
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { StatTile } from '@/components/charts'

interface User {
  id: string
  email: string
  full_name: string
  display_name: string
  discipline: string
  status: string
  registration_number: string | null
  staff_number: string | null
  last_login_at: string | null
}

export default function People() {
  const [tab, setTab] = useState('directory')
  const [search, setSearch] = useState('')
  const [role, setRole] = useState('')

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['users', search, role],
    queryFn: () =>
      api.get<{ items: User[]; total: number }>('/users', {
        page_size: 200,
        search: search || undefined,
        role_code: role || undefined,
      }),
  })

  const { data: roles } = useQuery({
    queryKey: ['roles'],
    queryFn: () => api.get<any[]>('/users/roles/catalogue'),
  })

  const { data: permissions } = useQuery({
    queryKey: ['permission-catalogue'],
    queryFn: () => api.get<Record<string, { code: string; name: string }[]>>('/users/roles/permissions'),
    enabled: tab === 'roles',
  })

  const users = data?.items ?? []

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">People &amp; roles</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          Roles are scoped: a permission held in one department does not apply in another.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-3">
        <StatTile label="People" value={formatNumber(data?.total ?? 0)} icon={<Users className="h-4 w-4" />} />
        <StatTile label="Roles defined" value={formatNumber(roles?.length ?? 0)} />
        <StatTile
          label="Active accounts"
          value={formatNumber(users.filter((u) => u.status === 'active').length)}
        />
      </div>

      <Tabs
        tabs={[
          { id: 'directory', label: 'Directory', count: users.length },
          { id: 'roles', label: 'Roles', count: roles?.length },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === 'directory' ? (
        <Card>
          <div
            className="flex flex-wrap gap-2 border-b px-5 py-3"
            style={{ borderColor: 'var(--border-hairline)' }}
          >
            <div className="relative min-w-[14rem] flex-1">
              <Search
                className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 opacity-50"
                aria-hidden
              />
              <Input
                className="pl-8"
                placeholder="Search by name, email or registration number"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
            <Select
              className="w-auto"
              value={role}
              onChange={(event) => setRole(event.target.value)}
              aria-label="Filter by role"
            >
              <option value="">All roles</option>
              {(roles ?? []).map((entry) => (
                <option key={entry.id} value={entry.code}>
                  {entry.name}
                </option>
              ))}
            </Select>
          </div>

          {isLoading ? (
            <LoadingRows />
          ) : error ? (
            <CardBody>
              <ErrorState error={error} retry={refetch} />
            </CardBody>
          ) : !users.length ? (
            <EmptyState title="No people match this search" />
          ) : (
            <TableWrap>
              <thead>
                <tr>
                  <Th>Name</Th>
                  <Th>Registration</Th>
                  <Th>Discipline</Th>
                  <Th>Status</Th>
                  <Th>Last signed in</Th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id}>
                    <Td>
                      <div className="flex items-center gap-2.5">
                        <span
                          className="grid h-7 w-7 shrink-0 place-items-center rounded-full text-[10px] font-semibold"
                          style={{ background: 'var(--surface-3)' }}
                          aria-hidden
                        >
                          {initials(user.display_name)}
                        </span>
                        <div className="min-w-0">
                          <p className="truncate font-medium">{user.full_name}</p>
                          <p className="truncate text-xs" style={{ color: 'var(--text-muted)' }}>
                            {user.email}
                          </p>
                        </div>
                      </div>
                    </Td>
                    <Td className="tnum text-xs">{user.registration_number ?? '—'}</Td>
                    <Td className="text-xs">{humanise(user.discipline)}</Td>
                    <Td>
                      <Badge tone={user.status === 'active' ? 'good' : 'neutral'}>
                        {humanise(user.status)}
                      </Badge>
                    </Td>
                    <Td className="text-xs">
                      {user.last_login_at ? formatDate(user.last_login_at) : 'Never'}
                    </Td>
                  </tr>
                ))}
              </tbody>
            </TableWrap>
          )}
        </Card>
      ) : null}

      {tab === 'roles' ? (
        <div className="space-y-5">
          <Card>
            <CardHeader
              title="Role catalogue"
              description="Lower rank is more senior. Nobody may grant a role more senior than their own."
            />
            <TableWrap>
              <thead>
                <tr>
                  <Th>Role</Th>
                  <Th>Rank</Th>
                  <Th>Usual scope</Th>
                  <Th>Permissions</Th>
                  <Th>Origin</Th>
                </tr>
              </thead>
              <tbody>
                {(roles ?? []).map((entry) => (
                  <tr key={entry.id}>
                    <Td className="font-medium">{entry.name}</Td>
                    <Td className="tnum text-xs">{entry.rank}</Td>
                    <Td className="text-xs">{humanise(entry.scope_kind)}</Td>
                    <Td className="tnum text-xs">
                      {entry.permission_codes.includes('*')
                        ? 'All (platform administrator)'
                        : entry.permission_codes.length}
                    </Td>
                    <Td>
                      <Badge tone={entry.is_system ? 'neutral' : 'brand'}>
                        {entry.is_system ? 'Platform' : 'Institution'}
                      </Badge>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </TableWrap>
          </Card>

          {permissions ? (
            <Card>
              <CardHeader
                title="Permission vocabulary"
                description="Every capability the platform understands, grouped by area."
              />
              <CardBody className="grid gap-5 sm:grid-cols-2 lg:grid-cols-3">
                {Object.entries(permissions).map(([category, entries]) => (
                  <div key={category}>
                    <p
                      className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider"
                      style={{ color: 'var(--text-muted)' }}
                    >
                      {humanise(category)}
                    </p>
                    <ul className="space-y-1">
                      {entries.map((permission) => (
                        <li key={permission.code} className="text-xs">
                          <span className="font-medium">{permission.name}</span>
                          <code
                            className="ml-1 rounded px-1 py-0.5 text-[10px]"
                            style={{ background: 'var(--surface-2)', color: 'var(--text-muted)' }}
                          >
                            {permission.code}
                          </code>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))}
              </CardBody>
            </Card>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
