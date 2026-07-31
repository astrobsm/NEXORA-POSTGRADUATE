/**
 * Vendor identity.
 *
 * Kept in one place and read everywhere, so the attribution cannot drift between
 * the sign-in screen, the sidebar and the about panel — and so a white-labelled
 * deployment changes it once.
 *
 * This is distinct from *institution* branding: a hospital sets its own logo and
 * colours (see `lib/branding.tsx`), but the platform vendor stays constant.
 */

export const VENDOR = {
  name: 'NEXORA Technologies',
  statement: 'Created and managed by NEXORA Technologies',
  url: 'https://github.com/astrobsm/NEXORA-POSTGRADUATE',
  productName: 'Postgraduate Medical Training Console',
  shortName: 'RTC',
} as const

export const APP_VERSION =
  typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '1.0.0'

/** © line for footers and printed exports. */
export function copyright(): string {
  return `© ${new Date().getFullYear()} ${VENDOR.name}`
}
