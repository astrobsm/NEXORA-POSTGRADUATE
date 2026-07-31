/**
 * Institution branding.
 *
 * Where a hospital makes the platform its own: crest, app icon, and the two
 * colours everything else derives from. The preview is live because the whole UI
 * reads `var(--brand)` — what you see here is what every screen will do.
 */

import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Building2,
  CheckCircle2,
  Image as ImageIcon,
  Palette,
  Trash2,
  Upload,
} from 'lucide-react'

import { api, ApiError, tokens } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import { applyBrandColours, inkFor, useBranding } from '@/lib/branding'
import { formatNumber } from '@/lib/utils'
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
  RagBadge,
} from '@/components/ui'

interface AssetInfo {
  url: string
  content_type: string
  filename: string | null
  size_bytes: number
  width: number | null
  height: number | null
  checksum: string
  updated_at: string
}

interface BrandingSummary {
  tenant_id: string
  name: string
  code: string
  colours: Record<string, string>
  max_bytes: number
  accepted_types: string[]
  assets: Partial<Record<string, AssetInfo>>
}

const KINDS = [
  {
    kind: 'logo',
    label: 'Institution logo',
    hint: 'Shown in the sidebar and on the sign-in screen. A wide wordmark or crest works best.',
    frame: 'h-16 w-full max-w-[16rem]',
  },
  {
    kind: 'icon',
    label: 'App icon',
    hint: 'Used when the app is installed to a phone or desktop. Must be square — 512×512 or larger.',
    frame: 'h-20 w-20',
  },
  {
    kind: 'favicon',
    label: 'Browser tab icon',
    hint: 'Optional. Falls back to the app icon when not set.',
    frame: 'h-10 w-10',
  },
  {
    kind: 'login_backdrop',
    label: 'Sign-in backdrop',
    hint: 'Optional image behind the sign-in panel. A photograph of the hospital works well.',
    frame: 'h-24 w-full max-w-[20rem]',
  },
] as const

// --------------------------------------------------------------------------
function AssetSlot({
  spec,
  asset,
  onChanged,
}: {
  spec: (typeof KINDS)[number]
  asset: AssetInfo | undefined
  onChanged: () => void
}) {
  const inputRef = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  // Cache-busted so a replaced asset is visibly replaced, not served from cache.
  const [version, setVersion] = useState(0)

  async function upload(file: File) {
    setBusy(true)
    setError(null)
    try {
      const body = new FormData()
      body.append('file', file)

      // FormData must not carry an explicit Content-Type — the browser has to
      // set the multipart boundary itself.
      const response = await fetch(`/api/v1/tenancy/tenants/current/branding/${spec.kind}`, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${tokens.access()}`,
          ...(tokens.tenant() ? { 'X-Tenant-Id': tokens.tenant()! } : {}),
        },
        body,
      })
      const payload = await response.json().catch(() => null)
      if (!response.ok) {
        throw new ApiError(response.status, payload?.detail ?? 'Upload failed.')
      }
      setVersion((v) => v + 1)
      onChanged()
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : 'Upload failed.')
    } finally {
      setBusy(false)
    }
  }

  const remove = useMutation({
    mutationFn: () => api.delete(`/tenancy/tenants/current/branding/${spec.kind}`),
    onSuccess: () => {
      setVersion((v) => v + 1)
      onChanged()
    },
  })

  return (
    <div
      className="rounded-[var(--radius-control)] border p-4"
      style={{ borderColor: 'var(--border-hairline)' }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-sm font-medium">{spec.label}</p>
          <p className="mt-0.5 text-xs" style={{ color: 'var(--text-muted)' }}>
            {spec.hint}
          </p>
        </div>
        {asset ? <Badge tone="good">Set</Badge> : <Badge tone="neutral">Not set</Badge>}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-4">
        <div
          className={`grid shrink-0 place-items-center overflow-hidden rounded-lg border ${spec.frame}`}
          style={{
            borderColor: 'var(--border-hairline)',
            background: 'var(--surface-2)',
          }}
        >
          {asset ? (
            /* Rendered through <img>, where an uploaded SVG cannot execute. */
            <img
              src={`${asset.url}?v=${version}`}
              alt={`${spec.label} preview`}
              className="max-h-full max-w-full object-contain"
            />
          ) : (
            <ImageIcon className="h-5 w-5 opacity-30" aria-hidden />
          )}
        </div>

        <div className="min-w-0 flex-1">
          {asset ? (
            <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
              {asset.filename ?? asset.content_type} ·{' '}
              {formatNumber(asset.size_bytes / 1024, 0)} KiB
              {asset.width && asset.height ? ` · ${asset.width}×${asset.height}` : ''}
            </p>
          ) : null}

          <div className="mt-2 flex flex-wrap gap-2">
            <input
              ref={inputRef}
              type="file"
              accept="image/png,image/jpeg,image/webp,image/svg+xml,image/gif"
              className="hidden"
              onChange={(event) => {
                const file = event.target.files?.[0]
                if (file) void upload(file)
                event.target.value = ''
              }}
            />
            <Button
              size="sm"
              icon={<Upload className="h-3.5 w-3.5" />}
              loading={busy}
              onClick={() => inputRef.current?.click()}
            >
              {asset ? 'Replace' : 'Upload'}
            </Button>
            {asset ? (
              <Button
                size="sm"
                variant="ghost"
                icon={<Trash2 className="h-3.5 w-3.5" />}
                loading={remove.isPending}
                onClick={() => remove.mutate()}
              >
                Remove
              </Button>
            ) : null}
          </div>

          {error ? (
            <p className="mt-2 text-xs" style={{ color: 'var(--status-critical)' }}>
              {error}
            </p>
          ) : null}
        </div>
      </div>
    </div>
  )
}

// --------------------------------------------------------------------------
function ColourField({
  label,
  hint,
  value,
  onChange,
}: {
  label: string
  hint: string
  value: string
  onChange: (next: string) => void
}) {
  return (
    <Field label={label} hint={hint}>
      <div className="flex items-center gap-2">
        <input
          type="color"
          aria-label={`${label} colour picker`}
          value={/^#[0-9a-f]{6}$/i.test(value) ? value : '#166534'}
          onChange={(event) => onChange(event.target.value)}
          className="h-9 w-12 shrink-0 cursor-pointer rounded-[var(--radius-control)] border bg-transparent p-1"
          style={{ borderColor: 'var(--border-hairline)' }}
        />
        <Input
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder="#166534"
          className="tnum font-mono"
          spellCheck={false}
        />
      </div>
    </Field>
  )
}

// --------------------------------------------------------------------------
export default function Branding() {
  const { can } = useAuth()
  const { reload } = useBranding()
  const queryClient = useQueryClient()

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: ['branding-summary'],
    queryFn: () => api.get<BrandingSummary>('/tenancy/tenants/current/branding'),
  })

  const [primary, setPrimary] = useState('')
  const [accent, setAccent] = useState('')
  const [logoText, setLogoText] = useState('')
  const [motto, setMotto] = useState('')
  const [saved, setSaved] = useState(false)

  useEffect(() => {
    if (!data) return
    setPrimary(data.colours.primary ?? '#166534')
    setAccent(data.colours.accent ?? '#b45309')
    setLogoText(data.colours.logo_text ?? '')
    setMotto(data.colours.motto ?? '')
  }, [data])

  // Live preview: the whole interface reads these variables, so the page recolours
  // as the picker moves.
  useEffect(() => {
    if (!primary && !accent) return
    applyBrandColours({ primary, accent })
  }, [primary, accent])

  const save = useMutation({
    mutationFn: () =>
      api.patch('/tenancy/tenants/current', {
        branding: {
          ...(data?.colours ?? {}),
          primary,
          accent,
          logo_text: logoText.trim() || undefined,
          motto: motto.trim() || undefined,
        },
      }),
    onSuccess: async () => {
      setSaved(true)
      await reload()
      void queryClient.invalidateQueries({ queryKey: ['branding-summary'] })
      window.setTimeout(() => setSaved(false), 3000)
    },
  })

  const refresh = () => {
    void refetch()
    void reload()
  }

  if (!can('tenancy.settings.manage')) {
    return (
      <Card>
        <EmptyState
          icon={<Building2 className="h-10 w-10" />}
          title="Branding is managed by your institution's administrators"
          description="Contact your Director of Residency Training or Chief Medical Director to change the logo or colours."
        />
      </Card>
    )
  }

  if (isLoading) return <LoadingRows rows={6} />
  if (error) return <ErrorState error={error} retry={refetch} />
  if (!data) return null

  return (
    <div className="space-y-5">
      <header>
        <h1 className="text-xl font-semibold tracking-tight">Institution branding</h1>
        <p className="mt-0.5 text-sm" style={{ color: 'var(--text-muted)' }}>
          {data.name} · {data.code}. Changes apply to everyone on the next page load.
        </p>
      </header>

      <div className="grid gap-5 lg:grid-cols-[1.4fr_1fr]">
        {/* ---- Assets --------------------------------------------------- */}
        <Card>
          <CardHeader
            title="Logo and icons"
            description={`PNG, JPEG, WebP, GIF or SVG, up to ${Math.round(data.max_bytes / 1024)} KiB.`}
          />
          <CardBody className="space-y-3">
            {KINDS.map((spec) => (
              <AssetSlot
                key={spec.kind}
                spec={spec}
                asset={data.assets[spec.kind]}
                onChanged={refresh}
              />
            ))}

            <div
              className="flex items-start gap-2 rounded-[var(--radius-control)] p-3 text-xs"
              style={{ background: 'var(--surface-2)', color: 'var(--text-secondary)' }}
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <p>
                Uploads are checked against their actual file contents, not the
                extension, and an SVG carrying scripting is rejected. Branding images
                are public — they are served without a session so the sign-in screen
                can show them.
              </p>
            </div>
          </CardBody>
        </Card>

        {/* ---- Colours -------------------------------------------------- */}
        <div className="space-y-5">
          <Card>
            <CardHeader
              title="Colours"
              description="The interface derives every shade from these two."
            />
            <CardBody className="space-y-4">
              <ColourField
                label="Primary"
                hint="Navigation, primary buttons, single-series charts."
                value={primary}
                onChange={setPrimary}
              />
              <ColourField
                label="Accent"
                hint="Highlights and secondary emphasis."
                value={accent}
                onChange={setAccent}
              />
              <Field label="Short name" hint="Two to four letters, used where the full logo will not fit.">
                <Input
                  value={logoText}
                  onChange={(event) => setLogoText(event.target.value.slice(0, 6))}
                  placeholder="UTH"
                  maxLength={6}
                />
              </Field>
              <Field label="Motto" hint="Optional, shown on the sign-in screen.">
                <Input
                  value={motto}
                  onChange={(event) => setMotto(event.target.value)}
                  placeholder="Learn. Serve. Advance."
                />
              </Field>

              {save.isError ? <ErrorState error={save.error} /> : null}

              <div className="flex items-center gap-2">
                <Button variant="primary" loading={save.isPending} onClick={() => save.mutate()}>
                  Save colours
                </Button>
                <Button
                  variant="ghost"
                  onClick={() => {
                    setPrimary('#166534')
                    setAccent('#b45309')
                  }}
                >
                  Reset to default
                </Button>
                {saved ? (
                  <span
                    className="inline-flex items-center gap-1 text-xs"
                    style={{ color: 'var(--text-success)' }}
                  >
                    <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                    Saved
                  </span>
                ) : null}
              </div>
            </CardBody>
          </Card>

          {/* ---- Live preview ------------------------------------------ */}
          <Card>
            <CardHeader
              title="Preview"
              description="This page is already using your colours."
              action={<Palette className="h-4 w-4 opacity-40" aria-hidden />}
            />
            <CardBody className="space-y-3">
              <div className="flex flex-wrap items-center gap-2">
                <Button variant="primary" size="sm">
                  Primary action
                </Button>
                <Button size="sm">Secondary</Button>
                <span
                  className="inline-flex items-center rounded-full px-2.5 py-1 text-xs font-medium"
                  style={{ background: 'var(--accent)', color: 'var(--accent-ink)' }}
                >
                  Accent
                </span>
              </div>

              <div className="flex flex-wrap items-center gap-2">
                <RagBadge rag="green" />
                <RagBadge rag="amber" />
                <RagBadge rag="red" />
              </div>
              <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
                Status colours are fixed and never follow the brand — a clinical
                signal must mean the same thing in every institution.
              </p>

              {/* Contrast is measured, not assumed: a pale brand gets dark ink. */}
              <div
                className="rounded-[var(--radius-control)] p-3 text-sm font-medium"
                style={{ background: primary || 'var(--brand)', color: inkFor(primary) }}
              >
                Text on your primary colour is chosen for readability.
              </div>
            </CardBody>
          </Card>
        </div>
      </div>
    </div>
  )
}
