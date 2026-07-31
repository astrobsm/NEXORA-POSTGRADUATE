import { useState, type FormEvent } from 'react'
import { Loader2, Lock, ShieldCheck, Stethoscope } from 'lucide-react'
import { useAuth } from '@/lib/auth'
import { useBranding } from '@/lib/branding'
import { Attribution } from '@/components/Attribution'
import { ApiError, OfflineError } from '@/lib/api'
import { Button, Field, Input } from '@/components/ui'

const DEMO_ACCOUNTS = [
  { label: 'House Officer', email: 'houseofficer1@uthdemo.health' },
  { label: 'Registrar', email: 'registrar1@uthdemo.health' },
  { label: 'Senior Registrar', email: 'snr.registrar1@uthdemo.health' },
  { label: 'Consultant', email: 'consultant1@uthdemo.health' },
  { label: 'Head of Department', email: 'hod.surgery@uthdemo.health' },
  { label: 'Director of Residency', email: 'drt@uthdemo.health' },
  { label: 'Chief Medical Director', email: 'cmd@uthdemo.health' },
]

export default function SignIn() {
  const { signIn, verifyMfa } = useAuth()
  const { branding } = useBranding()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [stage, setStage] = useState<'credentials' | 'mfa'>('credentials')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const showDemo = import.meta.env.VITE_SHOW_DEMO_ACCOUNTS !== 'false'

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (stage === 'credentials') {
        const result = await signIn(email.trim().toLowerCase(), password)
        if (result === 'mfa') setStage('mfa')
      } else {
        await verifyMfa(code.trim())
      }
    } catch (caught) {
      if (caught instanceof OfflineError) {
        setError(
          'Cannot reach the training server. Signing in for the first time on this device needs a connection.',
        )
      } else if (caught instanceof ApiError) {
        setError(caught.message)
      } else {
        setError('Something went wrong. Please try again.')
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grid min-h-dvh lg:grid-cols-[1.1fr_1fr]">
      {/* ---- Brand panel ------------------------------------------------- */}
      <div
        className="relative hidden flex-col justify-between p-10 lg:flex"
        style={{
          background: branding.assets.login_backdrop
            ? `linear-gradient(rgba(0,0,0,0.55), rgba(0,0,0,0.72)), url(${branding.assets.login_backdrop}) center/cover`
            : 'var(--brand)',
          color: branding.assets.login_backdrop ? '#ffffff' : 'var(--brand-ink)',
        }}
      >
        <div className="flex items-center gap-3">
          {branding.assets.logo ? (
            <span className="grid place-items-center rounded-xl bg-white/95 px-3 py-2">
              <img
                src={branding.assets.logo}
                alt={`${branding.name} logo`}
                className="h-10 max-w-[13rem] object-contain"
              />
            </span>
          ) : (
            <div className="grid h-10 w-10 place-items-center rounded-xl bg-white/15 text-xs font-bold">
              {branding.colours.logo_text ?? 'RTC'}
            </div>
          )}
          <div>
            <p className="text-base font-semibold leading-tight">{branding.name}</p>
            <p className="text-xs opacity-80">
              {branding.colours.motto ?? 'Postgraduate Medical Training Platform'}
            </p>
          </div>
        </div>

        <div className="max-w-md">
          <h1 className="text-3xl font-semibold leading-tight tracking-tight">
            Every trainee&rsquo;s progress, measured against the curriculum they were
            actually enrolled on.
          </h1>
          <p className="mt-4 text-sm leading-relaxed opacity-90">
            Competency-based training, a validated digital logbook, automatic rotation
            scheduling, and promotion decisions that show their working — built for
            NPMCN, WACS, WACP, MDCN and NUC requirements, and configurable for any
            postgraduate college.
          </p>
          <ul className="mt-6 space-y-2 text-sm opacity-90">
            {[
              'Works offline — record cases in theatre, sync later',
              'Consultant validation before anything counts',
              'Policy is configuration, never a code change',
            ].map((item) => (
              <li key={item} className="flex items-start gap-2">
                <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
                {item}
              </li>
            ))}
          </ul>
        </div>

        <div className="space-y-1.5">
          <p className="text-xs opacity-70">
            Handles de-identified training records only. No patient-identifiable data is
            stored.
          </p>
          <Attribution variant="inverse" />
        </div>
      </div>

      {/* ---- Form -------------------------------------------------------- */}
      <div className="flex items-center justify-center p-6 sm:p-10">
        <div className="w-full max-w-sm">
          <div className="mb-8 lg:hidden">
            {branding.assets.logo ? (
              <img
                src={branding.assets.logo}
                alt={`${branding.name} logo`}
                className="mb-3 h-11 max-w-[13rem] object-contain"
              />
            ) : (
              <div
                className="mb-3 grid h-11 w-11 place-items-center rounded-xl text-xs font-bold"
                style={{ background: 'var(--brand)', color: 'var(--brand-ink)' }}
              >
                {branding.colours.logo_text ?? 'RTC'}
              </div>
            )}
            <h1 className="text-xl font-semibold tracking-tight">{branding.name}</h1>
          </div>

          <h2 className="text-lg font-semibold tracking-tight">
            {stage === 'credentials' ? 'Sign in' : 'Two-factor verification'}
          </h2>
          <p className="mt-1 text-sm" style={{ color: 'var(--text-muted)' }}>
            {stage === 'credentials'
              ? 'Use your institutional training account.'
              : 'Enter the 6-digit code from your authenticator app.'}
          </p>

          <form onSubmit={handleSubmit} className="mt-6 space-y-4">
            {stage === 'credentials' ? (
              <>
                <Field label="Email address" required>
                  <Input
                    type="email"
                    value={email}
                    onChange={(event) => setEmail(event.target.value)}
                    autoComplete="username"
                    placeholder="you@hospital.health"
                    required
                    autoFocus
                  />
                </Field>
                <Field label="Password" required>
                  <Input
                    type="password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    autoComplete="current-password"
                    required
                  />
                </Field>
              </>
            ) : (
              <Field label="Authentication code" required>
                <Input
                  inputMode="numeric"
                  pattern="[0-9]*"
                  maxLength={8}
                  value={code}
                  onChange={(event) => setCode(event.target.value)}
                  autoComplete="one-time-code"
                  className="tnum text-center text-lg tracking-[0.3em]"
                  required
                  autoFocus
                />
              </Field>
            )}

            {error ? (
              <div
                className="rounded-[var(--radius-control)] border px-3 py-2 text-xs"
                style={{
                  background: 'var(--status-critical-wash)',
                  borderColor: 'var(--status-critical)',
                  color: 'var(--status-critical)',
                }}
                role="alert"
              >
                {error}
              </div>
            ) : null}

            <Button type="submit" variant="primary" size="lg" className="w-full justify-center" loading={busy}>
              {busy ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Lock className="h-4 w-4" aria-hidden />
              )}
              {stage === 'credentials' ? 'Sign in' : 'Verify'}
            </Button>

            {stage === 'mfa' ? (
              <button
                type="button"
                onClick={() => {
                  setStage('credentials')
                  setCode('')
                  setError(null)
                }}
                className="w-full text-center text-xs underline-offset-2 hover:underline"
                style={{ color: 'var(--text-muted)' }}
              >
                Use a different account
              </button>
            ) : null}
          </form>

          <Attribution className="mt-8 lg:hidden" showVersion />

          {showDemo && stage === 'credentials' ? (
            <div
              className="mt-8 rounded-[var(--radius-card)] border p-3.5"
              style={{ borderColor: 'var(--border-hairline)', background: 'var(--surface-2)' }}
            >
              <p className="flex items-center gap-1.5 text-xs font-medium">
                <Stethoscope className="h-3.5 w-3.5" aria-hidden />
                Demo institution
              </p>
              <p className="mt-1 text-[11px]" style={{ color: 'var(--text-muted)' }}>
                Each role sees a different console. Password for all demo accounts is{' '}
                <code className="tnum font-medium">RtcDemo!2026</code>.
              </p>
              <div className="mt-2.5 flex flex-wrap gap-1.5">
                {DEMO_ACCOUNTS.map((account) => (
                  <button
                    key={account.email}
                    type="button"
                    onClick={() => {
                      setEmail(account.email)
                      setPassword('RtcDemo!2026')
                      setError(null)
                    }}
                    className="rounded-full border px-2.5 py-1 text-[11px] transition-opacity hover:opacity-75"
                    style={{
                      borderColor: 'var(--border-hairline)',
                      background: 'var(--surface-1)',
                    }}
                  >
                    {account.label}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </div>
  )
}
