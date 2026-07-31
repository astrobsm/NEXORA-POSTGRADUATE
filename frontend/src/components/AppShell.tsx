/**
 * The application chrome: navigation, connection state and the sync indicator.
 *
 * Navigation is permission-driven — a consultant and a house officer see different
 * menus because the server told the client what they may do, not because the client
 * guessed from a role name.
 */

import { useEffect, useState } from 'react'
import { NavLink, Outlet, useNavigate } from 'react-router-dom'
import {
  Activity,
  Award,
  BookOpen,
  Building2,
  CalendarDays,
  ChevronDown,
  CloudOff,
  FlaskConical,
  Gauge,
  GraduationCap,
  LogOut,
  Menu,
  Moon,
  Palette,
  RefreshCw,
  ShieldCheck,
  Stethoscope,
  Sun,
  Users,
  X,
  ClipboardCheck,
  FileCheck2,
  Target,
} from 'lucide-react'
import { useAuth } from '@/lib/auth'
import { useBranding } from '@/lib/branding'
import { onSyncChange, startAutoSync, synchronise, type SyncState } from '@/lib/sync'
import { openConflictCount, pendingCount } from '@/lib/db'
import { cn, formatRelative, initials, titleCase } from '@/lib/utils'
import { Badge, Button } from './ui'
import { Attribution } from './Attribution'

interface NavItem {
  to: string
  label: string
  icon: typeof Gauge
  /** Any one of these permissions grants access. Empty means always visible. */
  permissions?: string[]
  requiresEnrolment?: boolean
}

const NAV: { section: string; items: NavItem[] }[] = [
  {
    section: 'Training',
    items: [
      { to: '/', label: 'Dashboard', icon: Gauge },
      { to: '/logbook', label: 'Logbook', icon: BookOpen, permissions: ['logbook.entry.read.own', 'logbook.entry.read.any'] },
      { to: '/validation', label: 'Validation queue', icon: ShieldCheck, permissions: ['logbook.entry.validate'] },
      { to: '/rotations', label: 'Rotations', icon: CalendarDays, permissions: ['training.rotation.read'] },
      { to: '/competencies', label: 'Competencies', icon: Stethoscope, requiresEnrolment: true },
    ],
  },
  {
    section: 'Academic',
    items: [
      { to: '/academic', label: 'Activities & CME', icon: Activity, permissions: ['academic.activity.read'] },
      { to: '/cbt', label: 'Computer-based tests', icon: ClipboardCheck, permissions: ['exam.attempt.take'] },
      { to: '/readiness', label: 'Examination readiness', icon: Target, requiresEnrolment: true },
      { to: '/research', label: 'Research', icon: FlaskConical, permissions: ['research.project.create', 'research.project.read.any'] },
    ],
  },
  {
    section: 'Oversight',
    items: [
      { to: '/analytics', label: 'Analytics', icon: Gauge, permissions: ['analytics.department.read', 'analytics.institution.read'] },
      { to: '/promotion', label: 'Promotion', icon: Award, permissions: ['promotion.readiness.read'] },
      { to: '/accreditation', label: 'Accreditation', icon: Building2, permissions: ['accreditation.report.generate'] },
      { to: '/question-review', label: 'Question review', icon: FileCheck2, permissions: ['exam.question.review'] },
    ],
  },
  {
    section: 'Administration',
    items: [
      { to: '/curriculum', label: 'Curriculum builder', icon: GraduationCap, permissions: ['curriculum.read'] },
      { to: '/people', label: 'People & roles', icon: Users, permissions: ['identity.user.read'] },
      { to: '/branding', label: 'Branding', icon: Palette, permissions: ['tenancy.settings.manage'] },
    ],
  },
]

// --------------------------------------------------------------------------
function useTheme() {
  const [theme, setTheme] = useState<'light' | 'dark' | 'system'>(() => {
    const stored = localStorage.getItem('rtc.theme')
    return stored === 'dark' || stored === 'light' ? stored : 'system'
  })

  useEffect(() => {
    if (theme === 'system') {
      document.documentElement.removeAttribute('data-theme')
      localStorage.removeItem('rtc.theme')
    } else {
      document.documentElement.setAttribute('data-theme', theme)
      localStorage.setItem('rtc.theme', theme)
    }
  }, [theme])

  const isDark =
    theme === 'dark' ||
    (theme === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches)

  return { theme, isDark, toggle: () => setTheme(isDark ? 'light' : 'dark') }
}

function useConnection() {
  const [online, setOnline] = useState(navigator.onLine)
  useEffect(() => {
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
    }
  }, [])
  return online
}

// --------------------------------------------------------------------------
function SyncIndicator() {
  const [state, setState] = useState<SyncState>('idle')
  const [lastAt, setLastAt] = useState<string | null>(null)
  const [queued, setQueued] = useState(0)
  const [conflicts, setConflicts] = useState(0)

  useEffect(() => {
    const off = onSyncChange((next, result) => {
      setState(next)
      if (result?.at) setLastAt(result.at)
      void pendingCount().then(setQueued)
      void openConflictCount().then(setConflicts)
    })
    const stop = startAutoSync()
    const poll = window.setInterval(() => {
      void pendingCount().then(setQueued)
    }, 8000)
    return () => {
      off()
      stop()
      window.clearInterval(poll)
    }
  }, [])

  const busy = state === 'syncing'

  return (
    <button
      onClick={() => void synchronise()}
      disabled={busy}
      className="inline-flex items-center gap-2 rounded-[var(--radius-control)] border px-2.5 py-1.5 text-xs transition-opacity hover:opacity-80 disabled:opacity-60"
      style={{ borderColor: 'var(--border-hairline)', background: 'var(--surface-1)' }}
      title={
        lastAt
          ? `Last synchronised ${formatRelative(lastAt)}`
          : 'Synchronise with the training server'
      }
    >
      <RefreshCw className={cn('h-3.5 w-3.5', busy && 'animate-spin')} aria-hidden />
      <span className="hidden sm:inline">
        {busy ? 'Syncing…' : queued > 0 ? `${queued} queued` : 'Synced'}
      </span>
      {conflicts > 0 ? <Badge tone="warning">{conflicts} conflict</Badge> : null}
    </button>
  )
}

// --------------------------------------------------------------------------
export function AppShell() {
  const { principal, signOut, can } = useAuth()
  const { branding } = useBranding()
  const navigate = useNavigate()
  const { isDark, toggle } = useTheme()
  const online = useConnection()
  const [menuOpen, setMenuOpen] = useState(false)
  const [profileOpen, setProfileOpen] = useState(false)

  const visible = (item: NavItem) => {
    if (item.requiresEnrolment && !principal?.enrolment) return false
    if (!item.permissions?.length) return true
    return can(item.permissions[0], ...item.permissions.slice(1))
  }

  const sections = NAV.map((section) => ({
    ...section,
    items: section.items.filter(visible),
  })).filter((section) => section.items.length > 0)

  const primaryRole = principal?.roles.find((role) => role.is_primary) ?? principal?.roles[0]

  return (
    <div className="flex min-h-dvh flex-col">
      {/* ---- Offline banner --------------------------------------------- */}
      {!online ? (
        <div
          className="no-print flex items-center justify-center gap-2 px-4 py-1.5 text-xs font-medium"
          style={{ background: 'var(--status-warning)', color: '#0b0b0b' }}
          role="status"
        >
          <CloudOff className="h-3.5 w-3.5" aria-hidden />
          Working offline — entries are saved on this device and will sync when you reconnect.
        </div>
      ) : null}

      <div className="flex flex-1">
        {/* ---- Sidebar --------------------------------------------------- */}
        <aside
          className={cn(
            'no-print fixed inset-y-0 left-0 z-40 flex w-64 shrink-0 flex-col overflow-y-auto border-r transition-transform lg:static lg:translate-x-0',
            menuOpen ? 'translate-x-0' : '-translate-x-full',
          )}
          style={{ background: 'var(--surface-1)', borderColor: 'var(--border-hairline)' }}
        >
          <div className="flex h-14 items-center gap-2.5 px-4">
            {branding.assets.logo ? (
              /* Rendered via <img>: an uploaded SVG cannot execute here. */
              <img
                src={branding.assets.logo}
                alt={`${branding.name} logo`}
                className="h-9 max-w-[10.5rem] shrink-0 object-contain"
              />
            ) : (
              <>
                <div
                  className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-[10px] font-bold"
                  style={{ background: 'var(--brand)', color: 'var(--brand-ink)' }}
                  aria-hidden
                >
                  {branding.colours.logo_text ?? 'RTC'}
                </div>
                <div className="min-w-0">
                  <p className="truncate text-sm font-semibold leading-tight">
                    Residency Console
                  </p>
                  <p className="truncate text-[11px]" style={{ color: 'var(--text-muted)' }}>
                    {principal?.tenant?.name ?? branding.name}
                  </p>
                </div>
              </>
            )}
            <button
              className="ml-auto lg:hidden"
              onClick={() => setMenuOpen(false)}
              aria-label="Close navigation"
            >
              <X className="h-4 w-4" />
            </button>
          </div>

          <nav className="px-2.5 pb-4" aria-label="Main">
            {sections.map((section) => (
              <div key={section.section} className="mt-4">
                <p
                  className="px-2.5 pb-1.5 text-[10px] font-semibold uppercase tracking-wider"
                  style={{ color: 'var(--text-muted)' }}
                >
                  {section.section}
                </p>
                <ul className="space-y-0.5">
                  {section.items.map((item) => (
                    <li key={item.to}>
                      <NavLink
                        to={item.to}
                        end={item.to === '/'}
                        onClick={() => setMenuOpen(false)}
                        className={({ isActive }) =>
                          cn(
                            'flex items-center gap-2.5 rounded-[var(--radius-control)] px-2.5 py-2 text-sm transition-colors',
                            isActive ? 'font-medium' : 'hover:opacity-80',
                          )
                        }
                        style={({ isActive }) =>
                          isActive
                            ? { background: 'var(--surface-2)', color: 'var(--text-primary)' }
                            : { color: 'var(--text-secondary)' }
                        }
                      >
                        <item.icon className="h-4 w-4 shrink-0" aria-hidden />
                        {item.label}
                      </NavLink>
                    </li>
                  ))}
                </ul>
              </div>
            ))}
          </nav>

          <div
            className="mt-auto border-t px-4 py-3"
            style={{ borderColor: 'var(--border-hairline)' }}
          >
            <Attribution showVersion />
          </div>
        </aside>

        {menuOpen ? (
          <div
            className="fixed inset-0 z-30 bg-black/40 lg:hidden"
            onClick={() => setMenuOpen(false)}
            aria-hidden
          />
        ) : null}

        {/* ---- Main ------------------------------------------------------ */}
        <div className="flex min-w-0 flex-1 flex-col">
          <header
            className="no-print sticky top-0 z-20 flex h-14 items-center gap-3 border-b px-4 backdrop-blur"
            style={{
              background: 'color-mix(in srgb, var(--surface-page) 88%, transparent)',
              borderColor: 'var(--border-hairline)',
            }}
          >
            <button
              className="lg:hidden"
              onClick={() => setMenuOpen(true)}
              aria-label="Open navigation"
            >
              <Menu className="h-5 w-5" />
            </button>

            <div className="ml-auto flex items-center gap-2">
              <SyncIndicator />

              <Button
                size="icon"
                variant="ghost"
                onClick={toggle}
                aria-label={isDark ? 'Switch to light mode' : 'Switch to dark mode'}
              >
                {isDark ? <Sun className="h-4 w-4" /> : <Moon className="h-4 w-4" />}
              </Button>

              <div className="relative">
                <button
                  onClick={() => setProfileOpen((open) => !open)}
                  className="flex items-center gap-2 rounded-[var(--radius-control)] px-1.5 py-1 hover:opacity-80"
                  aria-haspopup="menu"
                  aria-expanded={profileOpen}
                >
                  <span
                    className="grid h-7 w-7 place-items-center rounded-full text-[11px] font-semibold"
                    style={{ background: 'var(--surface-3)' }}
                  >
                    {initials(principal?.user.display_name)}
                  </span>
                  <span className="hidden text-left sm:block">
                    <span className="block text-xs font-medium leading-tight">
                      {principal?.user.display_name}
                    </span>
                    <span
                      className="block text-[10px] leading-tight"
                      style={{ color: 'var(--text-muted)' }}
                    >
                      {titleCase(primaryRole?.role_name ?? primaryRole?.role_code ?? '')}
                    </span>
                  </span>
                  <ChevronDown className="h-3.5 w-3.5 opacity-60" aria-hidden />
                </button>

                {profileOpen ? (
                  <>
                    <div className="fixed inset-0 z-10" onClick={() => setProfileOpen(false)} />
                    <div
                      className="absolute right-0 z-20 mt-1.5 w-60 rounded-[var(--radius-card)] border p-1.5 shadow-[var(--shadow-pop)]"
                      style={{ background: 'var(--surface-1)', borderColor: 'var(--border-hairline)' }}
                      role="menu"
                    >
                      <div className="border-b px-2.5 pb-2 pt-1" style={{ borderColor: 'var(--border-hairline)' }}>
                        <p className="text-sm font-medium">{principal?.user.full_name}</p>
                        <p className="truncate text-xs" style={{ color: 'var(--text-muted)' }}>
                          {principal?.user.email}
                        </p>
                        {principal?.user.registration_number ? (
                          <p className="mt-1 text-[10px]" style={{ color: 'var(--text-muted)' }}>
                            Reg. {principal.user.registration_number}
                          </p>
                        ) : null}
                      </div>
                      <button
                        role="menuitem"
                        onClick={() => {
                          setProfileOpen(false)
                          navigate('/settings')
                        }}
                        className="flex w-full items-center gap-2 rounded-[var(--radius-control)] px-2.5 py-2 text-sm hover:opacity-80"
                        style={{ color: 'var(--text-secondary)' }}
                      >
                        <ShieldCheck className="h-4 w-4" aria-hidden />
                        Account & offline data
                      </button>
                      <button
                        role="menuitem"
                        onClick={async () => {
                          setProfileOpen(false)
                          await signOut()
                          navigate('/sign-in')
                        }}
                        className="flex w-full items-center gap-2 rounded-[var(--radius-control)] px-2.5 py-2 text-sm hover:opacity-80"
                        style={{ color: 'var(--status-critical)' }}
                      >
                        <LogOut className="h-4 w-4" aria-hidden />
                        Sign out
                      </button>
                    </div>
                  </>
                ) : null}
              </div>
            </div>
          </header>

          <main className="min-w-0 flex-1 p-4 sm:p-6">
            <Outlet />
          </main>
        </div>
      </div>
    </div>
  )
}
