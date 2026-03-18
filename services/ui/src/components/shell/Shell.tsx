import { Outlet } from 'react-router-dom'
import { Sidebar } from './Sidebar'

export function Shell() {
  // TODO: replace with real tenant context from JWT claims
  return (
    <div className="flex h-screen overflow-hidden bg-vigil-bg font-mono">
      <Sidebar tenantName="Acme Corp" tokensToday={42180} />
      <main className="flex-1 overflow-hidden">
        <Outlet />
      </main>
    </div>
  )
}
