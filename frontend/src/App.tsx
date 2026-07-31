import { Suspense, lazy } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { Loader2 } from 'lucide-react'

import { AppShell } from './components/AppShell'
import { useAuth } from './lib/auth'
import SignIn from './pages/SignIn'
import Dashboard from './pages/Dashboard'

// Routes beyond the dashboard are code-split: a house officer on a slow
// connection should not download the accreditation module to log a case.
const Logbook = lazy(() => import('./pages/Logbook'))
const ValidationQueue = lazy(() => import('./pages/ValidationQueue'))
const Rotations = lazy(() => import('./pages/Rotations'))
const Competencies = lazy(() => import('./pages/Competencies'))
const Academic = lazy(() => import('./pages/Academic'))
const Research = lazy(() => import('./pages/Research'))
const Analytics = lazy(() => import('./pages/Analytics'))
const Cbt = lazy(() => import('./pages/Cbt'))
const Readiness = lazy(() => import('./pages/Readiness'))
const QuestionReview = lazy(() => import('./pages/QuestionReview'))
const Promotion = lazy(() => import('./pages/Promotion'))
const Accreditation = lazy(() => import('./pages/Accreditation'))
const Curriculum = lazy(() => import('./pages/Curriculum'))
const People = lazy(() => import('./pages/People'))
const Settings = lazy(() => import('./pages/Settings'))
const Branding = lazy(() => import('./pages/Branding'))

function FullPageLoader({ message = 'Loading…' }: { message?: string }) {
  return (
    <div className="grid min-h-dvh place-items-center">
      <div className="flex flex-col items-center gap-3">
        <Loader2 className="h-6 w-6 animate-spin" style={{ color: 'var(--brand)' }} aria-hidden />
        <p className="text-sm" style={{ color: 'var(--text-muted)' }}>
          {message}
        </p>
      </div>
    </div>
  )
}

function RouteLoader() {
  return (
    <div className="grid place-items-center py-20">
      <Loader2 className="h-5 w-5 animate-spin" style={{ color: 'var(--brand)' }} aria-hidden />
    </div>
  )
}

export default function App() {
  const { principal, loading } = useAuth()

  if (loading) return <FullPageLoader message="Restoring your session…" />

  if (!principal) {
    return (
      <Routes>
        <Route path="/sign-in" element={<SignIn />} />
        <Route path="*" element={<Navigate to="/sign-in" replace />} />
      </Routes>
    )
  }

  return (
    <Suspense fallback={<RouteLoader />}>
      <Routes>
        <Route path="/sign-in" element={<Navigate to="/" replace />} />
        <Route element={<AppShell />}>
          <Route index element={<Dashboard />} />
          <Route path="logbook" element={<Logbook />} />
          <Route path="validation" element={<ValidationQueue />} />
          <Route path="rotations" element={<Rotations />} />
          <Route path="competencies" element={<Competencies />} />
          <Route path="academic" element={<Academic />} />
          <Route path="research" element={<Research />} />
          <Route path="analytics" element={<Analytics />} />
          <Route path="cbt" element={<Cbt />} />
          <Route path="readiness" element={<Readiness />} />
          <Route path="question-review" element={<QuestionReview />} />
          <Route path="promotion" element={<Promotion />} />
          <Route path="accreditation" element={<Accreditation />} />
          <Route path="curriculum" element={<Curriculum />} />
          <Route path="people" element={<People />} />
          <Route path="settings" element={<Settings />} />
          <Route path="branding" element={<Branding />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </Suspense>
  )
}
