/**
 * Institution branding.
 *
 * Applies the institution's own colours and assets at runtime by writing CSS
 * custom properties onto the document root. Every component already reads
 * `var(--brand)` / `var(--accent)` rather than a hard-coded value, so a hospital's
 * palette propagates without touching a single component.
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
import { api } from './api'

export interface BrandingColours {
  primary?: string
  accent?: string
  logo_text?: string
  motto?: string
  background?: string
}

export interface PublicBranding {
  tenant_id: string | null
  name: string
  code: string | null
  colours: BrandingColours
  manifest_url?: string
  assets: Partial<Record<'logo' | 'icon' | 'favicon' | 'login_backdrop', string>>
}

const FALLBACK: PublicBranding = {
  tenant_id: null,
  name: 'Residency Training Console',
  code: null,
  colours: {},
  assets: {},
}

interface BrandingContextValue {
  branding: PublicBranding
  loading: boolean
  reload: () => Promise<void>
}

const BrandingContext = createContext<BrandingContextValue>({
  branding: FALLBACK,
  loading: true,
  reload: async () => {},
})

// --------------------------------------------------------------------------
// Colour handling
// --------------------------------------------------------------------------
const HEX = /^#(?:[0-9a-f]{3}|[0-9a-f]{6})$/i

function normaliseHex(value: string | undefined): string | null {
  if (!value || !HEX.test(value.trim())) return null
  let hex = value.trim().toLowerCase()
  if (hex.length === 4) {
    hex = `#${hex[1]}${hex[1]}${hex[2]}${hex[2]}${hex[3]}${hex[3]}`
  }
  return hex
}

function channels(hex: string): [number, number, number] {
  return [
    parseInt(hex.slice(1, 3), 16),
    parseInt(hex.slice(3, 5), 16),
    parseInt(hex.slice(5, 7), 16),
  ]
}

function relativeLuminance(hex: string): number {
  const linear = channels(hex).map((value) => {
    const channel = value / 255
    return channel <= 0.03928 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]
}

function contrastRatio(a: string, b: string): number {
  const [light, dark] = [relativeLuminance(a), relativeLuminance(b)].sort((x, y) => y - x)
  return (light + 0.05) / (dark + 0.05)
}

/**
 * Pick readable ink for text sitting on a brand colour.
 *
 * An institution will upload the colour from its letterhead without checking
 * contrast; if we assumed white text, a pale gold or lime brand would render an
 * unreadable button. Choosing the ink from measured contrast means a bad brand
 * colour degrades to an ugly button rather than an illegible one.
 */
export function inkFor(background: string): string {
  const hex = normaliseHex(background)
  if (!hex) return '#ffffff'
  return contrastRatio(hex, '#ffffff') >= contrastRatio(hex, '#0c1310') ? '#ffffff' : '#0c1310'
}

/** A very light tint of the brand, for badge and wash backgrounds. */
function washFor(hex: string, dark: boolean): string {
  const [r, g, b] = channels(hex)
  return dark
    ? `rgb(${Math.round(r * 0.22)} ${Math.round(g * 0.22)} ${Math.round(b * 0.22)})`
    : `rgb(${Math.round(255 - (255 - r) * 0.14)} ${Math.round(255 - (255 - g) * 0.14)} ${Math.round(255 - (255 - b) * 0.14)})`
}

/**
 * Write the institution's colours onto the document root.
 *
 * Only overrides what the institution actually set — an unset accent keeps the
 * platform default rather than falling back to something arbitrary.
 */
export function applyBrandColours(colours: BrandingColours): void {
  const root = document.documentElement
  const dark =
    root.getAttribute('data-theme') === 'dark' ||
    (!root.hasAttribute('data-theme') &&
      window.matchMedia('(prefers-color-scheme: dark)').matches)

  const primary = normaliseHex(colours.primary)
  if (primary) {
    root.style.setProperty('--brand', primary)
    root.style.setProperty('--brand-ink', inkFor(primary))
    root.style.setProperty('--brand-wash', washFor(primary, dark))
    root.style.setProperty('--focus-ring', primary)
    // The theme-color meta drives the browser and OS chrome on mobile.
    document
      .querySelectorAll('meta[name="theme-color"]')
      .forEach((tag) => tag.setAttribute('content', primary))
  } else {
    ;['--brand', '--brand-ink', '--brand-wash', '--focus-ring'].forEach((token) =>
      root.style.removeProperty(token),
    )
  }

  const accent = normaliseHex(colours.accent)
  if (accent) {
    root.style.setProperty('--accent', accent)
    root.style.setProperty('--accent-ink', inkFor(accent))
    root.style.setProperty('--accent-wash', washFor(accent, dark))
  } else {
    ;['--accent', '--accent-ink', '--accent-wash'].forEach((token) =>
      root.style.removeProperty(token),
    )
  }
}

/** Point the browser tab icon at the institution's own mark. */
export function applyFavicon(url: string | undefined): void {
  const href = url ?? '/icon.svg'
  let link = document.querySelector<HTMLLinkElement>('link[rel="icon"]')
  if (!link) {
    link = document.createElement('link')
    link.rel = 'icon'
    document.head.appendChild(link)
  }
  // Only set the type for the platform default; an uploaded asset may be PNG.
  if (href === '/icon.svg') link.type = 'image/svg+xml'
  else link.removeAttribute('type')
  link.href = href
}

// --------------------------------------------------------------------------
export function BrandingProvider({ children }: { children: ReactNode }) {
  const [branding, setBranding] = useState<PublicBranding>(FALLBACK)
  const [loading, setLoading] = useState(true)

  const load = useCallback(async () => {
    try {
      const result = await api.get<PublicBranding>('/tenancy/public/branding', undefined, {
        anonymous: true,
      })
      setBranding(result)
    } catch {
      // Branding is cosmetic. Offline or a failed lookup falls back to the
      // platform identity rather than blocking the app from starting.
      setBranding(FALLBACK)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  useEffect(() => {
    applyBrandColours(branding.colours)
    applyFavicon(branding.assets.favicon ?? branding.assets.icon)

    // A branded manifest makes an installed app appear as the hospital's.
    if (branding.manifest_url) {
      const manifest = document.querySelector<HTMLLinkElement>('link[rel="manifest"]')
      if (manifest) manifest.href = branding.manifest_url
    }
  }, [branding])

  // The wash tints depend on the active theme, so recompute when it changes.
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)')
    const onChange = () => applyBrandColours(branding.colours)
    media.addEventListener('change', onChange)

    const observer = new MutationObserver(onChange)
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => {
      media.removeEventListener('change', onChange)
      observer.disconnect()
    }
  }, [branding.colours])

  const value = useMemo(
    () => ({ branding, loading, reload: load }),
    [branding, loading, load],
  )

  return <BrandingContext.Provider value={value}>{children}</BrandingContext.Provider>
}

export function useBranding(): BrandingContextValue {
  return useContext(BrandingContext)
}
