/**
 * Interface primitives.
 *
 * Deliberately small and unstyled-by-default: hospital deployments white-label the
 * platform, so components read colour from CSS custom properties rather than
 * hard-coded classes.
 */

import {
  forwardRef,
  type ButtonHTMLAttributes,
  type HTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, CheckCircle2, CircleHelp, Loader2, XCircle } from 'lucide-react'
import { cn, RAG_LABEL, RAG_WASH, type Rag } from '@/lib/utils'

// --------------------------------------------------------------------------
// Card
// --------------------------------------------------------------------------
export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('surface', className)} {...props} />
}

export function CardHeader({
  title,
  description,
  action,
  className,
}: {
  title: ReactNode
  description?: ReactNode
  action?: ReactNode
  className?: string
}) {
  return (
    <div
      className={cn(
        'flex items-start justify-between gap-4 border-b px-5 py-4',
        className,
      )}
      style={{ borderColor: 'var(--border-hairline)' }}
    >
      <div className="min-w-0">
        <h2 className="truncate text-sm font-semibold tracking-tight">{title}</h2>
        {description ? (
          <p className="mt-0.5 text-xs" style={{ color: 'var(--text-muted)' }}>
            {description}
          </p>
        ) : null}
      </div>
      {action ? <div className="shrink-0">{action}</div> : null}
    </div>
  )
}

export function CardBody({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('p-5', className)} {...props} />
}

// --------------------------------------------------------------------------
// Button
// --------------------------------------------------------------------------
type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success'
type ButtonSize = 'sm' | 'md' | 'lg' | 'icon'

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant
  size?: ButtonSize
  loading?: boolean
  icon?: ReactNode
}

const BUTTON_SIZES: Record<ButtonSize, string> = {
  sm: 'h-8 px-3 text-xs gap-1.5',
  md: 'h-9 px-3.5 text-sm gap-2',
  lg: 'h-11 px-5 text-sm gap-2',
  icon: 'h-9 w-9 justify-center',
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = 'secondary', size = 'md', loading, icon, className, children, disabled, ...props },
  ref,
) {
  const styles: Record<ButtonVariant, React.CSSProperties> = {
    primary: { background: 'var(--brand)', color: 'var(--brand-ink)', borderColor: 'transparent' },
    secondary: { background: 'var(--surface-1)', color: 'var(--text-primary)' },
    ghost: { background: 'transparent', color: 'var(--text-secondary)', borderColor: 'transparent' },
    danger: { background: 'var(--status-critical)', color: '#fff', borderColor: 'transparent' },
    success: { background: 'var(--status-good)', color: '#fff', borderColor: 'transparent' },
  }

  return (
    <button
      ref={ref}
      disabled={disabled || loading}
      className={cn(
        'inline-flex items-center rounded-[var(--radius-control)] border font-medium',
        'transition-[opacity,transform] duration-150 active:scale-[0.98]',
        'disabled:pointer-events-none disabled:opacity-50',
        'hover:opacity-90',
        BUTTON_SIZES[size],
        className,
      )}
      style={{ borderColor: 'var(--border-hairline)', ...styles[variant] }}
      {...props}
    >
      {loading ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : icon}
      {children}
    </button>
  )
})

/**
 * A link that looks like a button.
 *
 * Kept separate rather than giving Button an `asChild` escape hatch, because
 * nesting an anchor inside a button is invalid HTML and breaks keyboard
 * navigation in exactly the way screen-reader users notice first.
 */
export function LinkButton({
  to,
  variant = 'secondary',
  size = 'md',
  icon,
  className,
  children,
}: {
  to: string
  variant?: ButtonVariant
  size?: ButtonSize
  icon?: ReactNode
  className?: string
  children: ReactNode
}) {
  const styles: Record<ButtonVariant, React.CSSProperties> = {
    primary: { background: 'var(--brand)', color: 'var(--brand-ink)', borderColor: 'transparent' },
    secondary: { background: 'var(--surface-1)', color: 'var(--text-primary)' },
    ghost: { background: 'transparent', color: 'var(--text-secondary)', borderColor: 'transparent' },
    danger: { background: 'var(--status-critical)', color: '#fff', borderColor: 'transparent' },
    success: { background: 'var(--status-good)', color: '#fff', borderColor: 'transparent' },
  }
  return (
    <Link
      to={to}
      className={cn(
        'inline-flex items-center rounded-[var(--radius-control)] border font-medium',
        'transition-opacity hover:opacity-90',
        BUTTON_SIZES[size],
        className,
      )}
      style={{ borderColor: 'var(--border-hairline)', ...styles[variant] }}
    >
      {icon}
      {children}
    </Link>
  )
}

// --------------------------------------------------------------------------
// Badge & status
// --------------------------------------------------------------------------
export function Badge({
  children,
  tone = 'neutral',
  className,
}: {
  children: ReactNode
  tone?: 'neutral' | 'brand' | 'good' | 'warning' | 'critical' | 'info'
  className?: string
}) {
  const tones: Record<string, React.CSSProperties> = {
    neutral: { background: 'var(--surface-2)', color: 'var(--text-secondary)' },
    brand: { background: 'var(--brand)', color: 'var(--brand-ink)' },
    good: { background: 'var(--status-good-wash)', color: 'var(--text-success)' },
    warning: { background: 'var(--status-warning-wash)', color: 'var(--text-primary)' },
    critical: { background: 'var(--status-critical-wash)', color: 'var(--status-critical)' },
    info: { background: 'var(--surface-2)', color: 'var(--series-1)' },
  }
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium',
        className,
      )}
      style={tones[tone]}
    >
      {children}
    </span>
  )
}

/**
 * RAG indicator.
 *
 * Always renders an icon *and* a text label. The amber status is deliberately
 * below 3:1 contrast on a light surface, so colour alone would be unreadable for
 * some users — the icon and label are the required relief, not decoration.
 */
export function RagBadge({ rag, showLabel = true }: { rag?: string | null; showLabel?: boolean }) {
  const value = (rag ?? 'unknown') as Rag
  const Icon =
    value === 'green'
      ? CheckCircle2
      : value === 'amber'
        ? AlertTriangle
        : value === 'red'
          ? XCircle
          : CircleHelp
  const ink =
    value === 'green'
      ? 'var(--text-success)'
      : value === 'red'
        ? 'var(--status-critical)'
        : 'var(--text-primary)'

  return (
    <span
      className="inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-medium"
      style={{ background: RAG_WASH[value], color: ink }}
    >
      <Icon className="h-3.5 w-3.5" aria-hidden />
      {showLabel ? RAG_LABEL[value] : null}
    </span>
  )
}

// --------------------------------------------------------------------------
// Form controls
// --------------------------------------------------------------------------
export function Field({
  label,
  hint,
  error,
  required,
  children,
  className,
}: {
  label: string
  hint?: string
  error?: string
  required?: boolean
  children: ReactNode
  className?: string
}) {
  return (
    <label className={cn('block', className)}>
      <span className="mb-1.5 flex items-baseline gap-1 text-xs font-medium">
        {label}
        {required ? <span style={{ color: 'var(--status-critical)' }}>*</span> : null}
      </span>
      {children}
      {error ? (
        <span className="mt-1 block text-xs" style={{ color: 'var(--status-critical)' }}>
          {error}
        </span>
      ) : hint ? (
        <span className="mt-1 block text-xs" style={{ color: 'var(--text-muted)' }}>
          {hint}
        </span>
      ) : null}
    </label>
  )
}

const CONTROL_CLASS =
  'w-full rounded-[var(--radius-control)] border px-3 py-2 text-sm outline-none ' +
  'transition-colors placeholder:opacity-60 disabled:opacity-50'

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(
  function Input({ className, ...props }, ref) {
    return (
      <input
        ref={ref}
        className={cn(CONTROL_CLASS, className)}
        style={{ background: 'var(--surface-1)', borderColor: 'var(--border-hairline)' }}
        {...props}
      />
    )
  },
)

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(
  function Textarea({ className, ...props }, ref) {
    return (
      <textarea
        ref={ref}
        rows={3}
        className={cn(CONTROL_CLASS, 'resize-y', className)}
        style={{ background: 'var(--surface-1)', borderColor: 'var(--border-hairline)' }}
        {...props}
      />
    )
  },
)

export const Select = forwardRef<HTMLSelectElement, SelectHTMLAttributes<HTMLSelectElement>>(
  function Select({ className, children, ...props }, ref) {
    return (
      <select
        ref={ref}
        className={cn(CONTROL_CLASS, 'appearance-none pr-8', className)}
        style={{
          background: 'var(--surface-1)',
          borderColor: 'var(--border-hairline)',
          color: 'var(--text-primary)',
        }}
        {...props}
      >
        {children}
      </select>
    )
  },
)

// --------------------------------------------------------------------------
// Feedback
// --------------------------------------------------------------------------
export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode
  title: string
  description?: string
  action?: ReactNode
}) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {icon ? (
        <div className="mb-3 opacity-40" aria-hidden>
          {icon}
        </div>
      ) : null}
      <p className="text-sm font-medium">{title}</p>
      {description ? (
        <p className="mt-1 max-w-sm text-xs" style={{ color: 'var(--text-muted)' }}>
          {description}
        </p>
      ) : null}
      {action ? <div className="mt-4">{action}</div> : null}
    </div>
  )
}

export function ErrorState({ error, retry }: { error: unknown; retry?: () => void }) {
  const message =
    error instanceof Error ? error.message : 'Something went wrong loading this view.'
  return (
    <div
      className="flex items-start gap-3 rounded-[var(--radius-card)] border p-4"
      style={{ background: 'var(--status-critical-wash)', borderColor: 'var(--status-critical)' }}
      role="alert"
    >
      <XCircle className="mt-0.5 h-4 w-4 shrink-0" style={{ color: 'var(--status-critical)' }} />
      <div className="min-w-0 flex-1">
        <p className="text-sm font-medium">Could not load this view</p>
        <p className="mt-0.5 text-xs" style={{ color: 'var(--text-secondary)' }}>
          {message}
        </p>
      </div>
      {retry ? (
        <Button size="sm" onClick={retry}>
          Try again
        </Button>
      ) : null}
    </div>
  )
}

export function Skeleton({ className }: { className?: string }) {
  return <div className={cn('skeleton', className)} />
}

export function LoadingRows({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-2 p-5">
      {Array.from({ length: rows }, (_, index) => (
        <Skeleton key={index} className="h-10 w-full" />
      ))}
    </div>
  )
}

// --------------------------------------------------------------------------
// Table
// --------------------------------------------------------------------------
export function TableWrap({ children }: { children: ReactNode }) {
  // Wide tables scroll inside their own container so the page body never scrolls
  // horizontally on a phone.
  return (
    <div className="w-full overflow-x-auto">
      <table className="w-full min-w-[42rem] border-collapse text-sm">{children}</table>
    </div>
  )
}

export function Th({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <th
      className={cn(
        'border-b px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wide',
        className,
      )}
      style={{ color: 'var(--text-muted)', borderColor: 'var(--border-hairline)' }}
    >
      {children}
    </th>
  )
}

export function Td({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <td
      className={cn('border-b px-4 py-2.5 align-middle', className)}
      style={{ borderColor: 'var(--border-hairline)' }}
    >
      {children}
    </td>
  )
}

// --------------------------------------------------------------------------
// Tabs
// --------------------------------------------------------------------------
export function Tabs({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: string; label: string; count?: number }[]
  active: string
  onChange: (id: string) => void
}) {
  return (
    <div
      className="flex gap-1 overflow-x-auto border-b"
      style={{ borderColor: 'var(--border-hairline)' }}
      role="tablist"
    >
      {tabs.map((tab) => {
        const selected = tab.id === active
        return (
          <button
            key={tab.id}
            role="tab"
            aria-selected={selected}
            onClick={() => onChange(tab.id)}
            className="relative whitespace-nowrap px-3.5 py-2.5 text-sm font-medium transition-colors"
            style={{ color: selected ? 'var(--text-primary)' : 'var(--text-muted)' }}
          >
            {tab.label}
            {tab.count !== undefined ? (
              <span
                className="ml-1.5 rounded-full px-1.5 py-0.5 text-[10px] tnum"
                style={{ background: 'var(--surface-2)', color: 'var(--text-secondary)' }}
              >
                {tab.count}
              </span>
            ) : null}
            {selected ? (
              <span
                className="absolute inset-x-2 -bottom-px h-0.5 rounded-full"
                style={{ background: 'var(--brand)' }}
              />
            ) : null}
          </button>
        )
      })}
    </div>
  )
}

// --------------------------------------------------------------------------
export function Progress({
  value,
  tone = 'brand',
  label,
}: {
  value: number
  tone?: 'brand' | Rag
  label?: string
}) {
  const clamped = Math.max(0, Math.min(100, value))
  const fill =
    tone === 'brand'
      ? 'var(--brand)'
      : tone === 'green'
        ? 'var(--status-good)'
        : tone === 'amber'
          ? 'var(--status-warning)'
          : tone === 'red'
            ? 'var(--status-critical)'
            : 'var(--status-unknown)'

  return (
    <div className="w-full">
      {label ? (
        <div className="mb-1 flex items-baseline justify-between text-xs">
          <span style={{ color: 'var(--text-secondary)' }}>{label}</span>
          <span className="tnum font-medium">{clamped.toFixed(0)}%</span>
        </div>
      ) : null}
      <div
        className="h-1.5 w-full overflow-hidden rounded-full"
        style={{ background: 'var(--surface-3)' }}
        role="progressbar"
        aria-valuenow={Math.round(clamped)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={label}
      >
        <div
          className="h-full rounded-full transition-[width] duration-500"
          style={{ width: `${clamped}%`, background: fill }}
        />
      </div>
    </div>
  )
}
