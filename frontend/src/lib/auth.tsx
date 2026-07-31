/**
 * Session context.
 *
 * Holds the signed-in principal and exposes `can(permission)`, which every screen
 * uses to decide what to render. The client-side check is a usability affordance
 * only — the server enforces the same permission on every request, and the UI
 * never assumes its own answer is authoritative.
 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { api, tokens } from './api'
import { clearLocalData } from './db'

export interface Principal {
  user: {
    id: string
    email: string
    full_name: string
    display_name: string
    initials: string
    title?: string | null
    first_name: string
    last_name: string
    discipline: string
    status: string
    photo_key?: string | null
    registration_number?: string | null
    mfa_enabled: boolean
    preferences: Record<string, unknown>
  }
  tenant: {
    id: string
    name: string
    code: string
    branding: Record<string, string>
    accrediting_bodies: string[]
    settings: Record<string, unknown>
  } | null
  roles: {
    id: string
    role_code: string | null
    role_name: string | null
    org_unit_id: string | null
    org_unit_name: string | null
    is_primary: boolean
  }[]
  permissions: string[]
  is_superuser: boolean
  enrolment: {
    id: string
    programme_id: string
    programme_name: string | null
    curriculum_version_id: string
    org_unit_id: string
    current_year: number
    current_level: string
    status: string
    start_date: string
    expected_end_date: string
    latest_overall_score: number | null
    latest_rag: string | null
    promotion_ready: boolean
  } | null
}

interface AuthContextValue {
  principal: Principal | null
  loading: boolean
  error: string | null
  signIn: (email: string, password: string) => Promise<'ok' | 'mfa'>
  verifyMfa: (code: string) => Promise<void>
  signOut: () => Promise<void>
  refresh: () => Promise<void>
  can: (permission: string, ...others: string[]) => boolean
  hasRole: (...codes: string[]) => boolean
  isTrainee: boolean
  isSupervisor: boolean
  isLeadership: boolean
}

const AuthContext = createContext<AuthContextValue | null>(null)

const TRAINEE_ROLES = [
  'house_officer',
  'intern',
  'registrar',
  'senior_registrar',
  'resident',
  'medical_officer',
  'dental_officer',
]

const SUPERVISOR_ROLES = [
  'consultant',
  'intern_supervisor',
  'research_supervisor',
  'training_coordinator',
]

const LEADERSHIP_ROLES = [
  'head_of_department',
  'residency_coordinator',
  'director_residency',
  'deputy_director_residency',
  'chief_medical_director',
  'medical_director',
  'cmac',
  'dean',
  'college_admin',
  'national_residency_admin',
  'national_super_admin',
  'quality_assurance',
]

export function AuthProvider({ children }: { children: ReactNode }) {
  const [principal, setPrincipal] = useState<Principal | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [mfaChallenge, setMfaChallenge] = useState<string | null>(null)

  const loadPrincipal = useCallback(async () => {
    if (!tokens.access()) {
      setPrincipal(null)
      setLoading(false)
      return
    }
    try {
      const me = await api.get<Principal>('/auth/me')
      setPrincipal(me)
      tokens.setTenant(me.tenant?.id ?? null)
      setError(null)
    } catch {
      setPrincipal(null)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadPrincipal()
  }, [loadPrincipal])

  // The API client raises this when a refresh token has been revoked server-side.
  useEffect(() => {
    const onExpired = () => {
      setPrincipal(null)
      setError('Your session has expired. Please sign in again.')
    }
    window.addEventListener('rtc:session-expired', onExpired)
    return () => window.removeEventListener('rtc:session-expired', onExpired)
  }, [])

  const signIn = useCallback(
    async (email: string, password: string): Promise<'ok' | 'mfa'> => {
      setError(null)
      const response = await api.post<
        | { access_token: string; refresh_token: string }
        | { mfa_required: true; challenge_token: string }
      >('/auth/login', { email, password }, { anonymous: true })

      if ('mfa_required' in response) {
        setMfaChallenge(response.challenge_token)
        return 'mfa'
      }
      tokens.set(response.access_token, response.refresh_token)
      await loadPrincipal()
      return 'ok'
    },
    [loadPrincipal],
  )

  const verifyMfa = useCallback(
    async (code: string) => {
      if (!mfaChallenge) throw new Error('No multi-factor challenge is in progress.')
      const response = await api.post<{ access_token: string; refresh_token: string }>(
        '/auth/mfa/verify',
        { challenge_token: mfaChallenge, code },
        { anonymous: true },
      )
      tokens.set(response.access_token, response.refresh_token)
      setMfaChallenge(null)
      await loadPrincipal()
    },
    [mfaChallenge, loadPrincipal],
  )

  const signOut = useCallback(async () => {
    const refresh = tokens.refresh()
    if (refresh) {
      // Best effort: a failed sign-out must still clear the device.
      try {
        await api.post('/auth/logout', { refresh_token: refresh })
      } catch {
        /* offline — the local session is cleared regardless */
      }
    }
    tokens.clear()
    // Clinical data must not survive sign-out on a shared ward device.
    await clearLocalData()
    setPrincipal(null)
  }, [])

  const can = useCallback(
    (permission: string, ...others: string[]) => {
      if (!principal) return false
      if (principal.is_superuser || principal.permissions.includes('*')) return true
      return [permission, ...others].some((code) => principal.permissions.includes(code))
    },
    [principal],
  )

  const hasRole = useCallback(
    (...codes: string[]) => {
      if (!principal) return false
      const held = new Set(principal.roles.map((role) => role.role_code))
      return codes.some((code) => held.has(code))
    },
    [principal],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      principal,
      loading,
      error,
      signIn,
      verifyMfa,
      signOut,
      refresh: loadPrincipal,
      can,
      hasRole,
      isTrainee: Boolean(principal?.enrolment) || hasRole(...TRAINEE_ROLES),
      isSupervisor: hasRole(...SUPERVISOR_ROLES, ...LEADERSHIP_ROLES),
      isLeadership: hasRole(...LEADERSHIP_ROLES),
    }),
    [principal, loading, error, signIn, verifyMfa, signOut, loadPrincipal, can, hasRole],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside an AuthProvider.')
  return context
}
