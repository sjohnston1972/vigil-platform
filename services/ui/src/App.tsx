import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import { lazy, Suspense } from 'react'
import { Shell } from './components/shell/Shell'
import { TenantProvider } from './context/TenantContext'

const Dashboard       = lazy(() => import('./components/dashboard/Dashboard').then(m => ({ default: m.Dashboard })))
const ChatPage        = lazy(() => import('./components/chat/ChatPage').then(m => ({ default: m.ChatPage })))
const AuditPage       = lazy(() => import('./components/audit/AuditPage').then(m => ({ default: m.AuditPage })))
const AdminLayout     = lazy(() => import('./components/admin/AdminLayout').then(m => ({ default: m.AdminLayout })))
const TenantsPage     = lazy(() => import('./components/admin/TenantsPage').then(m => ({ default: m.TenantsPage })))
const BudgetsPage     = lazy(() => import('./components/admin/BudgetsPage').then(m => ({ default: m.BudgetsPage })))
const AgentHealthPage = lazy(() => import('./components/admin/AgentHealthPage').then(m => ({ default: m.AgentHealthPage })))

export default function App() {
  return (
    <TenantProvider>
    <BrowserRouter>
      <Suspense fallback={<div className="text-vigil-muted p-4 font-mono">Loading...</div>}>
        <Routes>
          <Route element={<Shell />}>
            <Route index element={<Navigate to="/dashboard" replace />} />
            <Route path="dashboard" element={<Dashboard />} />
            <Route path="chat" element={<ChatPage />} />
            <Route path="chat/:sessionId" element={<ChatPage />} />
            <Route path="audit" element={<AuditPage />} />
            <Route path="admin" element={<AdminLayout />}>
              <Route index element={<Navigate to="tenants" replace />} />
              <Route path="tenants" element={<TenantsPage />} />
              <Route path="budgets" element={<BudgetsPage />} />
              <Route path="agents"  element={<AgentHealthPage />} />
            </Route>
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
    </TenantProvider>
  )
}
