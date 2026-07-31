/**
 * HTTP client.
 *
 * Handles token storage, transparent refresh on 401, and — importantly for a ward
 * environment — distinguishes "the server said no" from "there is no network", so
 * the UI can queue the write instead of showing an error the user cannot act on.
 */

const API_BASE = import.meta.env.VITE_API_BASE ?? '/api/v1'

const ACCESS_KEY = 'rtc.access'
const REFRESH_KEY = 'rtc.refresh'
const TENANT_KEY = 'rtc.tenant'

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public code?: string,
    public fieldErrors?: Record<string, string[]>,
  ) {
    super(message)
    this.name = 'ApiError'
  }
}

/** Raised when the request never reached the server. Callers queue rather than fail. */
export class OfflineError extends Error {
  constructor(message = 'No connection to the training server.') {
    super(message)
    this.name = 'OfflineError'
  }
}

export const tokens = {
  access: () => localStorage.getItem(ACCESS_KEY),
  refresh: () => localStorage.getItem(REFRESH_KEY),
  tenant: () => localStorage.getItem(TENANT_KEY),
  set(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access)
    localStorage.setItem(REFRESH_KEY, refresh)
  },
  setTenant(tenantId: string | null) {
    if (tenantId) localStorage.setItem(TENANT_KEY, tenantId)
    else localStorage.removeItem(TENANT_KEY)
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY)
    localStorage.removeItem(REFRESH_KEY)
    localStorage.removeItem(TENANT_KEY)
  },
}

type Options = RequestInit & {
  params?: Record<string, string | number | boolean | string[] | undefined | null>
  /** Skip the Authorization header (sign-in, reference vocabularies). */
  anonymous?: boolean
  /** Internal: prevents an infinite refresh loop. */
  _retried?: boolean
}

function buildUrl(path: string, params?: Options['params']): string {
  const url = `${API_BASE}${path}`
  if (!params) return url
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === '') continue
    if (Array.isArray(value)) value.forEach((v) => search.append(key, String(v)))
    else search.append(key, String(value))
  }
  const query = search.toString()
  return query ? `${url}?${query}` : url
}

let refreshInFlight: Promise<boolean> | null = null

async function refreshSession(): Promise<boolean> {
  // Collapse concurrent 401s into one refresh, or a dashboard firing six queries
  // at once would burn six refresh tokens and log the user out.
  if (refreshInFlight) return refreshInFlight

  refreshInFlight = (async () => {
    const refresh = tokens.refresh()
    if (!refresh) return false
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      })
      if (!response.ok) return false
      const data = await response.json()
      tokens.set(data.access_token, data.refresh_token)
      return true
    } catch {
      return false
    } finally {
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

export async function request<T = unknown>(path: string, options: Options = {}): Promise<T> {
  const { params, anonymous, _retried, headers, ...init } = options

  const finalHeaders: Record<string, string> = {
    Accept: 'application/json',
    ...(init.body ? { 'Content-Type': 'application/json' } : {}),
    ...((headers as Record<string, string>) ?? {}),
  }

  if (!anonymous) {
    const access = tokens.access()
    if (access) finalHeaders.Authorization = `Bearer ${access}`
    const tenant = tokens.tenant()
    if (tenant) finalHeaders['X-Tenant-Id'] = tenant
  }

  let response: Response
  try {
    response = await fetch(buildUrl(path, params), { ...init, headers: finalHeaders })
  } catch {
    throw new OfflineError()
  }

  if (response.status === 401 && !anonymous && !_retried) {
    if (await refreshSession()) {
      return request<T>(path, { ...options, _retried: true })
    }
    tokens.clear()
    window.dispatchEvent(new CustomEvent('rtc:session-expired'))
    throw new ApiError(401, 'Your session has expired. Please sign in again.')
  }

  if (response.status === 204) return undefined as T

  const text = await response.text()
  const data = text ? JSON.parse(text) : null

  if (!response.ok) {
    throw new ApiError(
      response.status,
      data?.detail ?? `Request failed (${response.status}).`,
      data?.code,
      data?.field_errors,
    )
  }
  return data as T
}

export const api = {
  get: <T>(path: string, params?: Options['params'], options?: Options) =>
    request<T>(path, { ...options, method: 'GET', params }),

  post: <T>(path: string, body?: unknown, options?: Options) =>
    request<T>(path, {
      ...options,
      method: 'POST',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),

  patch: <T>(path: string, body?: unknown, options?: Options) =>
    request<T>(path, {
      ...options,
      method: 'PATCH',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),

  put: <T>(path: string, body?: unknown, options?: Options) =>
    request<T>(path, {
      ...options,
      method: 'PUT',
      body: body === undefined ? undefined : JSON.stringify(body),
    }),

  delete: <T>(path: string, options?: Options) =>
    request<T>(path, { ...options, method: 'DELETE' }),
}
