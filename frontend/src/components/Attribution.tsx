/**
 * Vendor attribution.
 *
 * Deliberately quiet. An institution's own crest is the identity a clinician
 * should see; the platform vendor belongs in the margin, not competing with it.
 */

import { cn } from '@/lib/utils'
import { APP_VERSION, copyright, VENDOR } from '@/lib/vendor'

export function Attribution({
  className,
  variant = 'muted',
  showVersion = false,
}: {
  className?: string
  /** `muted` for light surfaces; `inverse` for the coloured sign-in panel. */
  variant?: 'muted' | 'inverse'
  showVersion?: boolean
}) {
  const inverse = variant === 'inverse'
  return (
    <p
      className={cn('text-[11px] leading-relaxed', inverse && 'opacity-70', className)}
      style={inverse ? undefined : { color: 'var(--text-muted)' }}
    >
      Created and managed by{' '}
      <a
        href={VENDOR.url}
        target="_blank"
        rel="noreferrer noopener"
        className="font-medium underline-offset-2 hover:underline"
        style={inverse ? undefined : { color: 'var(--text-secondary)' }}
      >
        {VENDOR.name}
      </a>
      {showVersion ? (
        <>
          {' · '}
          <span className="tnum">v{APP_VERSION}</span>
        </>
      ) : null}
    </p>
  )
}

/** Fuller block for the account screen and printed exports. */
export function AttributionBlock({ className }: { className?: string }) {
  return (
    <div className={cn('space-y-1', className)}>
      <p className="text-sm font-medium">{VENDOR.productName}</p>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
        Version {APP_VERSION} · {copyright()}
      </p>
      <p className="text-xs" style={{ color: 'var(--text-muted)' }}>
        Created and managed by{' '}
        <a
          href={VENDOR.url}
          target="_blank"
          rel="noreferrer noopener"
          className="font-medium underline-offset-2 hover:underline"
          style={{ color: 'var(--brand)' }}
        >
          {VENDOR.name}
        </a>
        .
      </p>
    </div>
  )
}
