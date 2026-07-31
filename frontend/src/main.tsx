import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import App from './App'
import { AuthProvider } from './lib/auth'
import { BrandingProvider } from './lib/branding'
import { OfflineError } from './lib/api'
import './index.css'

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // Clinical data changes slowly within a session; a minute of staleness saves
      // a great deal of bandwidth on a hospital connection.
      staleTime: 60_000,
      gcTime: 30 * 60_000,
      refetchOnWindowFocus: false,
      retry: (failureCount, error) => {
        // Retrying while genuinely offline just burns battery — the sync engine
        // handles reconnection, and cached data is already on screen.
        if (error instanceof OfflineError) return false
        return failureCount < 2
      },
    },
    mutations: { retry: false },
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <BrandingProvider>
          <AuthProvider>
            <App />
          </AuthProvider>
        </BrandingProvider>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
)

// Register the service worker after first paint so it never delays the shell.
if ('serviceWorker' in navigator && import.meta.env.PROD) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {
      // A blocked service worker degrades the app to online-only, which is
      // survivable; it must never prevent the app from starting.
    })
  })
}
