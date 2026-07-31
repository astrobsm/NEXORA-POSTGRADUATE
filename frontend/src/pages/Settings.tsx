/**
 * Account and offline-data management.
 *
 * The offline section matters more than it looks: on a shared ward device, a user
 * needs to see exactly what is held locally and be able to clear it.
 */

import { useEffect, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { Database, HardDriveDownload, KeyRound, Smartphone, Trash2 } from 'lucide-react'

import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { clearLocalData, db, getMeta, openConflictCount, pendingCount } from '@/lib/db'
import { synchronise } from '@/lib/sync'
import { formatDateTime, formatNumber, formatRelative } from '@/lib/utils'
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
  Td,
  Th,
  TableWrap,
} from '@/components/ui'
import { StatTile } from '@/components/charts'
import { AttributionBlock } from '@/components/Attribution'

export default function Settings() {
  const { principal, refresh } = useAuth()
  const [storage, setStorage] = useState({ records: 0, queued: 0, conflicts: 0, lastSync: '' })
  const [passwords, setPasswords] = useState({ current: '', next: '', confirm: '' })
  const [passwordMessage, setPasswordMessage] = useState<string | null>(null)

  const loadStorage = async () => {
    setStorage({
      records: await db.records.count(),
      queued: await pendingCount(),
      conflicts: await openConflictCount(),
      lastSync: await getMeta<string>('lastPulledAt', ''),
    })
  }

  useEffect(() => {
    void loadStorage()
  }, [])

  const { data: sessions } = useQuery({
    queryKey: ['sessions'],
    queryFn: () => api.get<any[]>('/auth/sessions'),
  })

  const changePassword = useMutation({
    mutationFn: () =>
      api.post('/auth/password', {
        current_password: passwords.current,
        new_password: passwords.next,
      }),
    onSuccess: () => {
      setPasswords({ current: '', next: '', confirm: '' })
      setPasswordMessage(
        'Password updated. You have been signed out of your other devices.',
      )
    },
  })

  const revoke = useMutation({
    mutationFn: (sessionId: string) => api.delete(`/auth/sessions/${sessionId}`),
    onSuccess: () => void refresh(),
  })

  const passwordsMatch = passwords.next.length > 0 && passwords.next === passwords.confirm

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Account &amp; offline data</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          What this device holds, and who can sign in as you.
        </p>
      </header>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <StatTile
          label="Records cached on this device"
          value={formatNumber(storage.records)}
          icon={<Database className="h-4 w-4" />}
        />
        <StatTile
          label="Waiting to sync"
          value={formatNumber(storage.queued)}
          tone={storage.queued > 0 ? 'amber' : 'green'}
          hint={storage.queued > 0 ? 'Will upload when you reconnect' : 'Everything is uploaded'}
        />
        <StatTile
          label="Unresolved conflicts"
          value={formatNumber(storage.conflicts)}
          tone={storage.conflicts > 0 ? 'red' : 'green'}
        />
        <StatTile
          label="Last synchronised"
          value={storage.lastSync ? formatRelative(storage.lastSync) : 'Never'}
          icon={<HardDriveDownload className="h-4 w-4" />}
        />
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card>
          <CardHeader
            title="Offline data"
            description="Training records held locally so the app works without a connection."
          />
          <CardBody className="space-y-3">
            <p className="text-xs" style={{ color: 'var(--text-secondary)' }}>
              Clearing local data removes the cached copy from this device. Anything still
              waiting to sync would be lost, so upload first. Signing out clears it
              automatically — important on a shared ward computer.
            </p>
            <div className="flex flex-wrap gap-2">
              <Button
                icon={<HardDriveDownload className="h-4 w-4" />}
                onClick={async () => {
                  await synchronise()
                  await loadStorage()
                }}
              >
                Sync now
              </Button>
              <Button
                variant="danger"
                icon={<Trash2 className="h-4 w-4" />}
                disabled={storage.queued > 0}
                onClick={async () => {
                  await clearLocalData()
                  await loadStorage()
                }}
              >
                Clear local data
              </Button>
            </div>
            {storage.queued > 0 ? (
              <p className="text-xs" style={{ color: 'var(--status-warning)' }}>
                {storage.queued} entr{storage.queued === 1 ? 'y is' : 'ies are'} still waiting
                to upload — sync before clearing.
              </p>
            ) : null}
          </CardBody>
        </Card>

        <Card>
          <CardHeader title="Change password" />
          <CardBody className="space-y-3">
            <Field label="Current password" required>
              <Input
                type="password"
                autoComplete="current-password"
                value={passwords.current}
                onChange={(event) =>
                  setPasswords({ ...passwords, current: event.target.value })
                }
              />
            </Field>
            <Field
              label="New password"
              required
              hint="At least 12 characters, with upper and lower case, a digit and a symbol."
            >
              <Input
                type="password"
                autoComplete="new-password"
                value={passwords.next}
                onChange={(event) => setPasswords({ ...passwords, next: event.target.value })}
              />
            </Field>
            <Field
              label="Confirm new password"
              required
              error={
                passwords.confirm && !passwordsMatch ? 'The passwords do not match.' : undefined
              }
            >
              <Input
                type="password"
                autoComplete="new-password"
                value={passwords.confirm}
                onChange={(event) =>
                  setPasswords({ ...passwords, confirm: event.target.value })
                }
              />
            </Field>
            {changePassword.isError ? <ErrorState error={changePassword.error} /> : null}
            {passwordMessage ? (
              <p className="text-xs" style={{ color: 'var(--text-success)' }}>
                {passwordMessage}
              </p>
            ) : null}
            <Button
              variant="primary"
              icon={<KeyRound className="h-4 w-4" />}
              disabled={!passwords.current || !passwordsMatch}
              loading={changePassword.isPending}
              onClick={() => changePassword.mutate()}
            >
              Update password
            </Button>
          </CardBody>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Active sessions"
          description="Every device currently signed in as you. Revoke anything you do not recognise."
        />
        {!sessions?.length ? (
          <EmptyState title="No other active sessions" />
        ) : (
          <TableWrap>
            <thead>
              <tr>
                <Th>Device</Th>
                <Th>Address</Th>
                <Th>Signed in</Th>
                <Th>Expires</Th>
                <Th />
              </tr>
            </thead>
            <tbody>
              {sessions.map((session) => (
                <tr key={session.id}>
                  <Td>
                    <div className="flex items-center gap-2">
                      <Smartphone className="h-4 w-4 opacity-50" aria-hidden />
                      <span className="max-w-xs truncate text-xs">
                        {session.device_label ?? session.user_agent ?? 'Unknown device'}
                      </span>
                    </div>
                  </Td>
                  <Td className="tnum text-xs">{session.ip_address ?? '—'}</Td>
                  <Td className="text-xs">{formatDateTime(session.created_at)}</Td>
                  <Td className="text-xs">{formatRelative(session.expires_at)}</Td>
                  <Td className="text-right">
                    <Button
                      size="sm"
                      variant="ghost"
                      loading={revoke.isPending && revoke.variables === session.id}
                      onClick={() => revoke.mutate(session.id)}
                    >
                      Revoke
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </TableWrap>
        )}
      </Card>

      <Card>
        <CardHeader title="About this platform" />
        <CardBody>
          <AttributionBlock />
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Your access" description="Roles and the scope each applies to." />
        <CardBody className="space-y-2">
          {principal?.roles.map((role) => (
            <div key={role.id} className="flex items-center justify-between gap-3">
              <div>
                <p className="text-sm font-medium">{role.role_name}</p>
                <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                  {role.org_unit_name ?? 'Platform-wide'}
                </p>
              </div>
              {role.is_primary ? <Badge tone="brand">Primary</Badge> : null}
            </div>
          ))}
          <p className="pt-2 text-xs" style={{ color: 'var(--text-muted)' }}>
            {principal?.is_superuser
              ? 'Platform administrator — unrestricted access.'
              : `${principal?.permissions.length ?? 0} permissions across ${principal?.roles.length ?? 0} role assignment(s).`}
          </p>
        </CardBody>
      </Card>
    </div>
  )
}
